"""Растительность под карту сети: цвета Natural Earth II (NE2_50M_SR), перепроецированные в проекцию карты.

Natural Earth II раскрашивает сушу по природным зонам: тайга — зелёная, тундра и горы — бурые и серые,
полярные пустыни — светлые. Здесь из неё берутся только цвета: изображение размыто (~10 км), чтобы
убрать встроенную отмывку — светотень рисует отдельный слой рельефа (geo_relief.py). Море и крупные
озёра залиты белым: на карте картинка кладётся режимом multiply, и белое ничего не меняет.

На расчёт не влияет — только подложка для читаемости карты.

    python yakutia/geo_vegetation.py --tif <путь к NE2_50M_SR.tif>

Нужен sips (macOS) — им TIFF переводится в BMP, а результат в JPEG.
Результат: yakutia/geo/vegetation.jpg и yakutia/geo/vegetation.json (рамка — та же, что у рельефа).
"""

import argparse
import json
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

import numpy as np

from geo_relief import BOX, KM_PX, box_blur, forward, inverse

HERE = Path(__file__).resolve().parent
OUT = HERE / 'geo'


def read_rgb(tif):
    """TIFF любого сжатия → BMP через sips → массив h×w×3."""
    with tempfile.TemporaryDirectory() as d:
        bmp = Path(d) / 'src.bmp'
        subprocess.run(['sips', '-s', 'format', 'bmp', str(tif), '--out', str(bmp)], check=True, capture_output=True)
        b = bmp.read_bytes()
    (off,) = struct.unpack('<I', b[10:14])
    w, h = struct.unpack('<ii', b[18:26])
    (bpp,) = struct.unpack('<H', b[28:30])
    assert bpp in (24, 32), f'нужен 24/32-битный BMP, а не {bpp}'
    n = bpp // 8
    row = (w * n + 3) // 4 * 4
    a = np.frombuffer(b, np.uint8, count=row * abs(h), offset=off).reshape(abs(h), row)[:, :w * n].reshape(abs(h), w, n)
    a = a[:, :, 2::-1] if n == 3 else a[:, :, [2, 1, 0]]  # BGR → RGB
    return a[::-1] if h > 0 else a                          # BMP хранит строки снизу вверх


def write_png_rgb(path, img):
    h, w, _ = img.shape
    raw = b''.join(b'\x00' + img[i].tobytes() for i in range(h))
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def build(tif):
    src = read_rgb(tif)
    H, W, _ = src.shape
    # море: самый частый цвет растра (NE2 заливает воду одним тоном) и всё, что к нему близко
    q = (src[::6, ::6] // 4).reshape(-1, 3).astype(np.int32)
    key = q[:, 0] * 4096 + q[:, 1] * 64 + q[:, 2]
    m = np.bincount(key).argmax()
    sea = np.array([m // 4096, m // 64 % 64, m % 64]) * 4 + 2
    print('цвет моря в источнике', sea.tolist())

    lo, la = np.meshgrid(np.linspace(BOX[0], BOX[2], 200), np.linspace(BOX[1], BOX[3], 100))
    fx, fy = forward(lo, la)
    x0, x1, y0, y1 = fx.min(), fx.max(), fy.min(), fy.max()
    w, h = int((x1 - x0) / KM_PX), int((y1 - y0) / KM_PX)
    gx, gy = np.meshgrid(x0 + (np.arange(w) + .5) * KM_PX, y1 - (np.arange(h) + .5) * KM_PX)
    lon, lat = inverse(gx, gy)
    u = np.clip(((lon + 180) / 360 * W).astype(int), 0, W - 1)
    v = np.clip(((90 - lat) / 180 * H).astype(int), 0, H - 1)
    rgb = src[v, u].astype(np.float32)
    water = np.abs(rgb - sea).sum(-1) < 30
    # размыть только сушу: вода не подмешивается в береговые цвета
    land = (~water).astype(np.float32)
    wsum = box_blur(land, 4)
    col = np.stack([box_blur(rgb[..., c] * land, 4) for c in range(3)], -1) / np.maximum(wsum, 1e-3)[..., None]
    # чуть насыщеннее и светлее: под multiply цвета должны тонировать, а не темнить карту
    grey = col.mean(-1, keepdims=True)
    col = grey + (col - grey) * 1.35
    col = 255 - (255 - col) * 0.8
    col[water] = 255
    img = np.clip(col, 0, 255).astype(np.uint8)
    OUT.mkdir(exist_ok=True)
    png = OUT / 'vegetation.png'
    write_png_rgb(png, img)
    subprocess.run(['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '72', str(png), '--out', str(OUT / 'vegetation.jpg')],
                   check=True, capture_output=True)
    png.unlink()
    meta = {'schema': 'yakutia-vegetation/1', 'source': 'Natural Earth II NE2_50M_SR (общественное достояние)',
            'projection': {'type': 'lambert_azimuthal_equal_area', 'lon0': 130.0, 'lat0': 65.0, 'units': 'km'},
            'blend': 'multiply, вода белая', 'image': 'vegetation.jpg',
            'box_svg': [round(float(x0), 1), round(float(-y1), 1), round(float(x1 - x0), 1), round(float(y1 - y0), 1)], 'px': [w, h]}
    (OUT / 'vegetation.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tif', required=True, help='NE2_50M_SR.tif из Natural Earth')
    a = ap.parse_args()
    meta = build(a.tif)
    print('рамка', meta['box_svg'], 'пиксели', meta['px'], 'КБ', (OUT / 'vegetation.jpg').stat().st_size // 1024)


if __name__ == '__main__':
    main()
