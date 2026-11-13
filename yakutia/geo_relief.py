"""Отмывка рельефа под карту сети: Natural Earth GRAY_50M_SR, перепроецированная в проекцию карты.

Проекция та же, что в geo_build.py (Ламберт, центр 130° в. д., 65° с. ш., км). На расчёт не влияет —
только подложка для читаемости карты. Плоские места приведены к нейтральному серому (128), поэтому
картинку можно накладывать режимом overlay/soft-light: тени темнят, освещённые склоны светлят.

    python yakutia/geo_relief.py --tif <путь к GRAY_50M_SR.tif>

Результат: yakutia/geo/relief.jpg (или .png без sips) и yakutia/geo/relief.json (рамка картинки в км карты).
"""

import argparse
import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / 'geo'
R, LON0, LAT0 = 6371.0, 130.0, 65.0
KM_PX = 2.5                      # разрешение подложки
BOX = (85.0, 48.0, 185.0, 80.0)  # lon/lat, которые должны попасть в картинку


def read_tiff_gray(path):
    """Несжатый 8-битный TIFF в один канал (как GRAY_50M_SR) — без сторонних библиотек."""
    b = Path(path).read_bytes()
    en = '<' if b[:2] == b'II' else '>'
    (ifd,) = struct.unpack(en + 'I', b[4:8])
    (n,) = struct.unpack(en + 'H', b[ifd:ifd + 2])
    tags = {}
    size = {1: 1, 3: 2, 4: 4}
    for i in range(n):
        tag, typ, cnt, val = struct.unpack(en + 'HHII', b[ifd + 2 + 12 * i: ifd + 14 + 12 * i])
        fmt = {3: 'H', 4: 'I'}.get(typ, 'B')
        if cnt * size.get(typ, 1) <= 4:
            raw = b[ifd + 10 + 12 * i: ifd + 10 + 12 * i + cnt * size.get(typ, 1)]
        else:
            raw = b[val: val + cnt * size.get(typ, 1)]
        tags[tag] = struct.unpack(en + fmt * cnt, raw)
    w, h = tags[256][0], tags[257][0]
    assert tags.get(259, (1,))[0] == 1, 'нужен несжатый TIFF'
    assert tags[258][0] == 8 and tags.get(277, (1,))[0] == 1, 'нужен 8-битный одноканальный TIFF'
    data = b''.join(b[o:o + c] for o, c in zip(tags[273], tags[279]))
    return np.frombuffer(data, np.uint8)[:w * h].reshape(h, w)


def forward(lon, lat):
    l, p, p0 = np.radians(lon - LON0), np.radians(lat), np.radians(LAT0)
    k = np.sqrt(2 / (1 + np.sin(p0) * np.sin(p) + np.cos(p0) * np.cos(p) * np.cos(l)))
    return R * k * np.cos(p) * np.sin(l), R * k * (np.cos(p0) * np.sin(p) - np.sin(p0) * np.cos(p) * np.cos(l))


def inverse(x, y):
    p0 = np.radians(LAT0)
    rho = np.hypot(x, y)
    c = 2 * np.arcsin(np.clip(rho / (2 * R), -1, 1))
    with np.errstate(invalid='ignore', divide='ignore'):
        lat = np.arcsin(np.cos(c) * np.sin(p0) + np.where(rho > 0, y * np.sin(c) * np.cos(p0) / rho, 0))
        lon = LON0 + np.degrees(np.arctan2(x * np.sin(c), rho * np.cos(p0) * np.cos(c) - y * np.sin(p0) * np.sin(c)))
    return (lon + 180) % 360 - 180, np.degrees(lat)


def box_blur(a, r):
    """Среднее по квадрату (2r+1)² через накопленные суммы, два прохода — почти гаусс."""
    for _ in range(2):
        p = np.pad(a, r, mode='edge')
        c = p.cumsum(0).cumsum(1)
        c = np.pad(c, ((1, 0), (1, 0)))
        n = 2 * r + 1
        a = (c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]) / (n * n)
    return a


def write_png_gray(path, img):
    h, w = img.shape
    raw = b''.join(b'\x00' + img[i].tobytes() for i in range(h))
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def build(tif):
    src = read_tiff_gray(tif)
    H, W = src.shape
    # море в источнике залито одним значением; равнины суши — другим. Приводим оба к нейтрали суши,
    # чтобы на карте осталась только светотень склонов, без общего осветления суши
    sea = int(np.bincount(src[::4, ::4].ravel()).argmax())
    land = src[::4, ::4][src[::4, ::4] != sea]
    neutral = int(np.bincount(land).argmax())
    src = np.where(src == sea, neutral, src).astype(np.uint8)
    # рамка в км: охватить BOX целиком
    lo, la = np.meshgrid(np.linspace(BOX[0], BOX[2], 200), np.linspace(BOX[1], BOX[3], 100))
    fx, fy = forward(lo, la)
    x0, x1, y0, y1 = fx.min(), fx.max(), fy.min(), fy.max()
    w, h = int((x1 - x0) / KM_PX), int((y1 - y0) / KM_PX)
    xs = x0 + (np.arange(w) + .5) * KM_PX
    ys = y1 - (np.arange(h) + .5) * KM_PX          # картинка сверху вниз — y карты убывает
    gx, gy = np.meshgrid(xs, ys)
    lon, lat = inverse(gx, gy)
    # билинейная выборка из равнопромежуточной сетки: столбец — долгота −180…180, строка — широта 90…−90
    u = (lon + 180) / 360 * W - .5
    v = (90 - lat) / 180 * H - .5
    u0, v0 = np.floor(u).astype(int), np.floor(v).astype(int)
    du, dv = u - u0, v - v0
    def px(a, b):
        return src[np.clip(b, 0, H - 1), a % W].astype(np.float32)
    val = (px(u0, v0) * (1 - du) * (1 - dv) + px(u0 + 1, v0) * du * (1 - dv)
           + px(u0, v0 + 1) * (1 - du) * dv + px(u0 + 1, v0 + 1) * du * dv)
    # нейтраль — самое частое значение (равнины и море); отклонения усилить, нейтраль в 128
    # в отмывке Natural Earth высокое ещё и светлее: убираем этот общий фон (размытие ~60 км),
    # остаётся местная светотень склонов вокруг нейтрали 128
    out = np.clip(128 + (val - box_blur(val, 12)) * 1.9, 0, 255).astype(np.uint8)
    OUT.mkdir(exist_ok=True)
    png = OUT / 'relief.png'
    write_png_gray(png, out)
    # в репозиторий — JPEG (в ~4 раза меньше); sips есть на macOS, иначе остаётся PNG
    if shutil.which('sips'):
        subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '70', str(png), '--out', str(OUT / 'relief.jpg')],
                       check=True, capture_output=True)
        png.unlink()
    # рамка в координатах SVG карты (y вниз, как pathOf: -y)
    meta = {'schema': 'yakutia-relief/1', 'source': 'Natural Earth GRAY_50M_SR (общественное достояние)',
            'projection': {'type': 'lambert_azimuthal_equal_area', 'lon0': LON0, 'lat0': LAT0, 'units': 'km'},
            'neutral': 128, 'image': 'relief.jpg' if shutil.which('sips') else 'relief.png', 'box_svg': [round(float(x0), 1), round(float(-y1), 1), round(float(x1 - x0), 1), round(float(y1 - y0), 1)], 'px': [w, h]}
    (OUT / 'relief.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    return meta, int(neutral)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tif', required=True, help='GRAY_50M_SR.tif из Natural Earth')
    a = ap.parse_args()
    meta, neutral = build(a.tif)
    print('рамка', meta['box_svg'], 'пиксели', meta['px'], 'нейтраль источника', neutral,
          'КБ', max(f.stat().st_size for f in OUT.glob('relief.*g')) // 1024)


if __name__ == '__main__':
    main()
