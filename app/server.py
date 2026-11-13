"""
Веб-приложение планировщика (Case A3): загрузка сценария -> проверка -> прогноз ML ->
расчёт матмоделью -> карта маршрута с рейсами и нагрузкой.

    pip install -r app/requirements.txt
    python app/server.py                 # http://127.0.0.1:8000  (документация API: /docs)

Принципы:
  * матмодель и контракт не меняются: расчёт — result_adapter.run_scenario, каждый
    результат проверяется по result.schema.json (это делает map/build_map.py);
  * входы проверяет сама матмодель (optimizer.load -> scenario_validation) до расчёта;
  * ML необязателен: без его зависимостей или при A3_ML_DISABLED=1 приложение работает,
    шаг «прогноз» просто пропускается;
  * расчёт идёт в отдельном процессе с ограничением по времени; сервер не блокируется;
  * загруженные файлы живут во временном каталоге задания и удаляются вместе с ним.

Сервер рассчитан на локальный запуск и демонстрацию: по умолчанию слушает 127.0.0.1,
без авторизации. Для внешнего доступа нужен обратный прокси с аутентификацией.
"""
from __future__ import annotations

import csv
import re
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (ROOT, ROOT / "ml", ROOT / "map", ROOT / "yakutia"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from optimizer import STRATEGIES, load  # noqa: E402
from scenario_validation import ScenarioValidationError  # noqa: E402
import a3_bridge as B  # noqa: E402  (только стандартная библиотека)
import geo_resolver  # noqa: E402
import build_map  # noqa: E402  (сборщик данных карты; тот же, что для статических страниц)

MAX_BODY_BYTES = int(os.environ.get("A3_MAX_UPLOAD_MB", "20")) * 1024 * 1024
JOB_TIMEOUT_S = int(os.environ.get("A3_JOB_TIMEOUT_S", "900"))
MAX_RUNNING = int(os.environ.get("A3_MAX_RUNNING", "2"))
MAX_KEPT_JOBS = 20
WORK = Path(tempfile.mkdtemp(prefix="a3_app_"))

try:                                     # ML — необязательная часть
    from predict import recommend_strategy as _recommend
    ML_IMPORT_ERROR = None
except Exception as e:                   # нет numpy/sklearn/torch
    _recommend, ML_IMPORT_ERROR = None, f"{type(e).__name__}: {e}"


def builtin_scenarios() -> Dict[str, dict]:
    out = {}
    demo = ROOT / "data/demo/scenario"
    if (demo / "cargo.csv").exists():
        out["demo"] = dict(title="Яна: небольшой пример, 6 партий", folder=demo, config=ROOT / "scenario_config.json")
    arc = ROOT / "JURY_SUBMISSION/Расчёты/data/scenarios"
    for sid, title in (("r1_baseline_central_central", "Яна: базовый сценарий"),
                       ("r1_baseline_shallow_central", "Яна: мелкий бар"),
                       ("r1_navigation_minus10_central_central", "Яна: навигация короче на 10 суток"),
                       ("r1_demand_plus30_central_central", "Яна: спрос +30%"),
                       ("r1_fleet_minus20_central_central", "Яна: флот −20%")):
        if (arc / sid / "cargo.csv").exists():
            out[sid] = dict(title=title, folder=arc / sid, config=ROOT / "scenario_config.json")
    for ex in sorted((ROOT / "map/examples").glob("*/config.json")):
        try:
            km = sum(float(g["distance_km"]) for g in json.loads(ex.read_text(encoding="utf-8"))["segments"])
            title = f"Маршрут, {km:.0f} км"
        except Exception:
            title = "Маршрут"
        out[ex.parent.name] = dict(title=title, folder=ex.parent, config=ex)
    return out


class Files(BaseModel):
    cargo: str = Field(..., description="содержимое cargo.csv")
    units: str = Field(..., description="содержимое units.csv")
    conditions: str = Field(..., description="содержимое conditions.csv")
    config: Optional[str] = Field(None, description="JSON конфигурации маршрута; без него берётся scenario_config.json")
    geo: Optional[str] = Field(None, description="JSON геометрии маршрута (как map/geo/*.json)")


class JobRequest(BaseModel):
    builtin: Optional[str] = Field(None, description="id готового сценария из /api/scenarios")
    files: Optional[Files] = None
    name: Optional[str] = Field(None, max_length=80)
    river: Optional[str] = Field(None, max_length=60, description="название реки — чтобы показать настоящее русло")
    strategies: Optional[list[str]] = Field(None, description="по умолчанию все три; первая из лучших открывается на карте")


class Job:
    def __init__(self, job_id: str, folder: Path):
        self.id, self.dir = job_id, folder
        self.status, self.error = "running", None
        self.started, self.finished = time.time(), None
        self.proc: Optional[subprocess.Popen] = None
        self.kind = "linear"


JOBS: Dict[str, Job] = {}
LOCK = threading.Lock()

app = FastAPI(title="Case A3 — планировщик рейсов", version="1.0.0",
              description="Проверка сценария, прогноз ML, расчёт матмоделью (контракт результата 1.0.0) и данные для карты.")


@app.middleware("http")
async def limit_body(request: Request, call_next):
    size = request.headers.get("content-length")
    if size and size.isdigit() and int(size) > MAX_BODY_BYTES:
        return JSONResponse({"detail": f"Слишком большой запрос: лимит {MAX_BODY_BYTES // 1024 // 1024} МБ"}, status_code=413)
    return await call_next(request)


def _write_totals(folder: Path, cargo_text: str) -> None:
    """Матмодель требует объявленные итоги спроса рядом со сценарием. Для загруженного
    сценария они выводятся из самого cargo.csv (независимой сверки при этом нет)."""
    rows = list(csv.DictReader(io.StringIO(cargo_text.lstrip("﻿"))))
    for name, key in (("expected_cargo_totals.csv", "cargo_type"), ("expected_plan_totals.csv", "plan_id")):
        tot: Dict[str, float] = {}
        for r in rows:
            k = (r.get(key) or "").strip()
            if k:
                try:
                    tot[k] = tot.get(k, 0.0) + float(r.get("volume_t") or 0)
                except ValueError:
                    return                    # нечисловой объём — пусть об этом скажет проверка матмодели
        if tot:
            with (folder / name).open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow([key, "volume_t"])
                w.writerows([k, round(v, 3)] for k, v in sorted(tot.items()))


def _prepare(req: JobRequest, job_dir: Path):
    """-> (каталог сценария, путь к конфигурации, путь к геометрии|None, название)."""
    if req.builtin:
        b = builtin_scenarios().get(req.builtin)
        if not b:
            raise HTTPException(404, f"Нет готового сценария «{req.builtin}»")
        return b["folder"], b["config"], None, b["title"]
    if not req.files:
        raise HTTPException(422, "Нужен либо builtin, либо files")
    f = req.files
    if sum(len(x or "") for x in (f.cargo, f.units, f.conditions, f.config, f.geo)) > MAX_BODY_BYTES:
        raise HTTPException(413, "Слишком большие файлы")
    folder = job_dir / "scenario"
    folder.mkdir(parents=True)
    for name, text in (("cargo.csv", f.cargo), ("units.csv", f.units), ("conditions.csv", f.conditions)):
        (folder / name).write_text(text, encoding="utf-8")
    _write_totals(folder, f.cargo)
    config = ROOT / "scenario_config.json"
    if f.config:
        try:
            json.loads(f.config.lstrip("﻿"))
        except ValueError as e:
            raise HTTPException(422, f"config.json: это не JSON ({e})")
        config = job_dir / "config.json"
        config.write_text(f.config.lstrip("﻿"), encoding="utf-8")
    geo = None
    if f.geo:
        try:
            g = json.loads(f.geo.lstrip("﻿"))
            assert isinstance(g.get("line"), list) and g.get("origin") and g.get("destination")
        except Exception:
            raise HTTPException(422, "Файл геометрии: ожидается JSON с полями origin, destination, line, line_km")
        geo = job_dir / "geo.json"
        geo.write_text(f.geo.lstrip("﻿"), encoding="utf-8")
    return folder, config, geo, (req.name or "Загруженный сценарий")


def _forecast(features: dict) -> dict:
    if _recommend is None:
        return dict(available=False, reason="ML-модуль недоступен: не установлены его зависимости")
    rec = _recommend(features)
    rec["available"] = True
    return rec


def _watch(job: Job):
    try:
        out, _ = job.proc.communicate(timeout=JOB_TIMEOUT_S)
        if job.proc.returncode == 0 and (job.dir / "payload.json").exists():
            job.status = "done"
        else:
            tail = (out or "").strip().splitlines()[-1:] or ["неизвестная ошибка"]
            job.status, job.error = "error", tail[0][:600]
    except subprocess.TimeoutExpired:
        job.proc.kill()
        job.status = "timeout"
        job.error = (f"Расчёт не уложился в {JOB_TIMEOUT_S // 60} мин и остановлен. "
                     "Сценарий слишком большой для интерактивного расчёта.")
    finally:
        job.finished = time.time()


def _evict():
    with LOCK:
        done = sorted((j for j in JOBS.values() if j.status != "running"), key=lambda j: j.finished or 0)
        while len(JOBS) > MAX_KEPT_JOBS and done:
            j = done.pop(0)
            shutil.rmtree(j.dir, ignore_errors=True)
            JOBS.pop(j.id, None)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Главная: сеть рек (сетевое ядро 3.0.0)."""
    html = (ROOT / "yakutia/network.html").read_text(encoding="utf-8")
    return html.replace("/*__APP__*/false", "true")


@app.get("/v4", response_class=HTMLResponse, include_in_schema=False)
def index_v4():
    """Проба оформления: растительность на карте и более читаемая светлая тема."""
    html = (ROOT / "yakutia/network_v4.html").read_text(encoding="utf-8")
    return html.replace("/*__APP__*/false", "true")


@app.get("/v3", include_in_schema=False)
def index_v3():
    """Старый адрес пробы v3: она теперь на главной."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/")


@app.get("/v2", include_in_schema=False)
def index_v2():
    """Старый адрес черновика нового оформления: оно теперь на главной."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/")


@app.get("/api/geo/context", include_in_schema=False)
def geo_context():
    """Фоновая география вокруг сети (yakutia/geo_context.py) — только для отрисовки карты."""
    path = ROOT / "yakutia/geo/context_geo.json"
    if not path.exists():
        raise HTTPException(404, "Нет yakutia/geo/context_geo.json: соберите его yakutia/geo_context.py")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/geo/relief", include_in_schema=False)
def geo_relief():
    """Рамка отмывки рельефа (yakutia/geo_relief.py) в координатах карты — только для отрисовки."""
    path = ROOT / "yakutia/geo/relief.json"
    if not path.exists():
        raise HTTPException(404, "Нет yakutia/geo/relief.json: соберите его yakutia/geo_relief.py")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/geo/relief.jpg", include_in_schema=False)
def geo_relief_image():
    path = ROOT / "yakutia/geo/relief.jpg"
    if not path.exists():
        raise HTTPException(404, "Нет yakutia/geo/relief.jpg: соберите его yakutia/geo_relief.py")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/geo/vegetation", include_in_schema=False)
def geo_vegetation():
    """Рамка слоя растительности (yakutia/geo_vegetation.py) в координатах карты — только для отрисовки."""
    path = ROOT / "yakutia/geo/vegetation.json"
    if not path.exists():
        raise HTTPException(404, "Нет yakutia/geo/vegetation.json: соберите его yakutia/geo_vegetation.py")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/geo/vegetation.jpg", include_in_schema=False)
def geo_vegetation_image():
    path = ROOT / "yakutia/geo/vegetation.jpg"
    if not path.exists():
        raise HTTPException(404, "Нет yakutia/geo/vegetation.jpg: соберите его yakutia/geo_vegetation.py")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/yana", include_in_schema=False)
def yana_page():
    """Одна река теперь открывается на той же странице, что и сеть."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/?scenario=r1_baseline_central_central")


@app.get("/api/health")
def health():
    return dict(status="ok", ml_available=_recommend is not None, ml_error=ML_IMPORT_ERROR,
                running=sum(j.status == "running" for j in JOBS.values()))


@app.get("/api/scenarios")
def scenarios():
    return dict(scenarios=[dict(id=k, title=v["title"]) for k, v in builtin_scenarios().items()],
                rivers=sorted(r["name"] for r in geo_resolver.library().values()),
                strategies=list(STRATEGIES), ml_available=_recommend is not None,
                job_timeout_s=JOB_TIMEOUT_S, max_upload_mb=MAX_BODY_BYTES // 1024 // 1024)


@app.get("/api/sample/{name}")
def sample(name: str):
    files = {"cargo.csv": ROOT / "data/demo/scenario/cargo.csv", "units.csv": ROOT / "data/demo/scenario/units.csv",
             "conditions.csv": ROOT / "data/demo/scenario/conditions.csv", "config.json": ROOT / "scenario_config.json"}
    if name not in files or not files[name].exists():
        raise HTTPException(404, "Нет такого примера")
    return FileResponse(files[name], filename=name)


@app.post("/api/forecast")
def forecast(req: JobRequest):
    """Только проверка входов и прогноз ML (десятки миллисекунд), без расчёта расписания."""
    job_dir = WORK / ("f_" + uuid.uuid4().hex[:12])
    job_dir.mkdir(parents=True)
    try:
        folder, config, _, _ = _prepare(req, job_dir)
        try:
            s = load(folder, json.loads(Path(config).read_text(encoding="utf-8-sig")))
        except (ScenarioValidationError, KeyError, ValueError) as e:
            raise HTTPException(422, f"Проверка матмодели: {e}")
        feats = B.extract_features(s)
        return dict(features=feats, forecast=_forecast(feats))
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@app.post("/api/jobs", status_code=202)
def create_job(req: JobRequest):
    """Проверить сценарий, сразу вернуть прогноз ML и запустить расчёт всех стратегий в фоне."""
    strategies = req.strategies or ["OPTIMIZED", "EARLIEST_DUE", "FCFS"]
    if any(st not in STRATEGIES for st in strategies):
        raise HTTPException(422, f"Стратегии: {list(STRATEGIES)}")
    with LOCK:
        if sum(j.status == "running" for j in JOBS.values()) >= MAX_RUNNING:
            raise HTTPException(429, "Сейчас считаются другие сценарии, попробуйте через минуту")
    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK / job_id
    job_dir.mkdir(parents=True)
    try:
        folder, config, geo, title = _prepare(req, job_dir)
        try:                                           # проверка входов матмоделью — до запуска расчёта
            s = load(folder, json.loads(Path(config).read_text(encoding="utf-8-sig")))
        except (ScenarioValidationError, KeyError, ValueError) as e:
            raise HTTPException(422, f"Проверка матмодели: {e}")
        t = time.time()
        feats = B.extract_features(s)
        fc = _forecast(feats)
        forecast_ms = round(1000 * (time.time() - t), 1)
        # предпросмотр: граф маршрута, глубины, спрос и прогноз ML — без расписания (стратегий нет)
        river = (req.river or "").strip() or None
        if river and not re.fullmatch(r"[0-9A-Za-zА-Яа-яЁё .\-]{1,60}", river):
            raise HTTPException(422, "Название реки: только буквы, цифры, пробел, точка и дефис")
        # в предпросмотре — только библиотека русел, без обращения в сеть (чтобы не ждать)
        preview = build_map.build_payload(Path(folder), Path(config), [], geo, False, title, None, river, False)
        preview["ml"] = fc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    cmd = [sys.executable, str(ROOT / "map/build_map.py"), "--scenario", str(folder), "--config", str(config),
           "--strategies", *strategies, "--title", title, "--out", str(job_dir / "page.html"),
           "--data-out", str(job_dir / "payload.json"), "--results-dir", str(job_dir / "results")]
    if geo:
        cmd += ["--geo", str(geo)]
    if river:
        cmd += ["--river", river]
    job = Job(job_id, job_dir)
    job.proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_watch, args=(job,), daemon=True).start()
    _evict()
    return dict(job_id=job_id, status="running", forecast=fc, forecast_ms=forecast_ms, preview=preview,
                scenario={k: feats[k] for k in ("cargo_count", "vessel_count", "route_length_km", "cargo_total_t",
                                                "navigation_days", "demand_to_throughput")})


def _job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Нет такого задания (возможно, оно уже удалено)")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    j = _job(job_id)
    return dict(job_id=j.id, status=j.status, error=j.error,
                elapsed_s=round((j.finished or time.time()) - j.started, 1))


@app.get("/api/jobs/{job_id}/data")
def job_data(job_id: str):
    """Данные для карты: граф маршрута, глубины, рейсы всех стратегий, лучшая стратегия, прогноз ML."""
    j = _job(job_id)
    if j.status != "done":
        raise HTTPException(409, f"Задание в состоянии {j.status}")
    return Response((j.dir / "payload.json").read_text(encoding="utf-8"), media_type="application/json")


@app.get("/api/jobs/{job_id}/result/{strategy}")
def job_result(job_id: str, strategy: str):
    """Полный результат стратегии по контракту 1.0.0 (result.schema.json), без изменений."""
    j = _job(job_id)
    if strategy not in STRATEGIES:
        raise HTTPException(404, "Нет такой стратегии")
    path = j.dir / "results" / f"result_{strategy}.json"
    if j.status != "done" or not path.exists():
        raise HTTPException(409, "Результат ещё не готов")
    return FileResponse(path, media_type="application/json", filename=f"result_{strategy}.json")


# ---------- сетевое ядро 3.0.0 (network/) ----------
NET_DATA = ROOT / "data/yakutia"
NET_DEMO = ROOT / "data/yakutia_demo"
NET_TARIFFS = NET_DATA / "tariffs_v2.json"      # тарифы v2: цена по статьям, поиск NETWORK_COST, денежные разницы
NET_STRATEGIES = ("NETWORK_REPOSITION", "NETWORK_SOCIAL", "NETWORK_GREEDY", "NETWORK_FCFS", "NETWORK_COST")


def network_builtin() -> Dict[str, dict]:
    out = {}
    if (NET_DATA / "yakutia_network.json").exists():
        out["yakutia"] = dict(title="Якутия: Лена, Алдан, Вилюй, Яна, Индигирка, Колыма",
                              scenario=NET_DATA / "yakutia_network.json", limits=NET_DATA / "limits.json",
                              tariffs=NET_TARIFFS, cards=NET_DATA / "fleet_cards.json")
    real = ROOT / "data/yakutia_real/scenario"
    if (real / "yakutia_real_2026_demo.json").exists():
        out["yakutia_real"] = dict(title="Якутия: реальные реки и план завоза 2026 (флот — допущение)",
                                   scenario=real / "yakutia_real_2026_demo.json", limits=real / "limits.json",
                                   tariffs=NET_TARIFFS, cards=real / "fleet_cards.json", geo=real / "geo.json",
                                   policy=real / "search_policy.json")
    if (real / "yakutia_real_2026_deep.json").exists():
        out["yakutia_real_deep"] = dict(title="Якутия 2026: реки углублены под суда река—море (допущение)",
                                        scenario=real / "yakutia_real_2026_deep.json", limits=real / "limits.json",
                                        tariffs=NET_TARIFFS, cards=real / "fleet_cards.json", geo=real / "geo.json",
                                        policy=real / "search_policy.json")
    if (real / "yakutia_real_2026_bykov.json").exists():
        out["yakutia_real_bykov"] = dict(title="Якутия 2026: + перевалка в Быковом Мысе (условный терминал)",
                                         scenario=real / "yakutia_real_2026_bykov.json", limits=real / "limits.json",
                                         tariffs=NET_TARIFFS, cards=real / "fleet_cards.json", geo=real / "geo.json",
                                         policy=real / "search_policy.json")
    if (NET_DEMO / "yakutia_stress.json").exists():
        out["yakutia_stress"] = dict(title="Якутия, стресс-тест причала: Нижнеянск, один причал",
                                     scenario=NET_DEMO / "yakutia_stress.json", tariffs=NET_DEMO / "tariffs_base.json",
                                     presets=NET_DEMO / "improvements.json")
    for sid, title in (("c_reposition_chain", "Пример ядра: перегон с продолжением"),
                       ("b_backhaul", "Пример ядра: обратная загрузка"),
                       ("e_social_tradeoff", "Пример ядра: соцминимум против тоннажа"),
                       ("g_shared_tug", "Пример ядра: общий буксир")):
        f = ROOT / "data/network_demo" / f"{sid}.json"
        if f.exists():
            out[sid] = dict(title=title, scenario=f, tariffs=NET_TARIFFS)
    return out


class NetworkRequest(BaseModel):
    builtin: Optional[str] = Field(None, description="id из /api/network/scenarios (сеть или одна река)")
    files: Optional[Files] = Field(None, description="одна река: cargo/units/conditions CSV (+ config)")
    river: Optional[str] = Field(None, max_length=60)
    scenario: Optional[dict] = Field(None, description="сетевой сценарий в формате data/network_demo/*.json")
    limits: Optional[dict] = Field(None, description="пределы поиска network.models.Limits")
    tariffs: Optional[dict] = Field(None, description="тарифы v2 (или v1) network.financial; без них — демо-ставки v2")
    presets: Optional[list] = Field(None, description="готовые изменения для «Что, если?» (формат change)")


def _ru_error(msg: str) -> str:
    hints = {"must be labeled SYNTHETIC": "ядро 3.0.0 принимает только сценарии с data_status = \"SYNTHETIC\"",
             "Unknown port": "участок или заявка ссылается на порт, которого нет в списке ports",
             "Missing condition": "для какого-то дня нет условий участка",
             "Invalid or duplicate compatible pair": "в compatible_pairs неизвестный или повторный буксир/баржа"}
    for k, v in hints.items():
        if k in msg:
            return f"{v} ({msg})"
    return msg


# Что показывать в выпадающем списке сайта. Остальные готовые сценарии остаются доступны через API
# (/api/network/jobs с builtin) — на них опираются тесты и примеры.
SHOWN_NETWORK = ("yakutia", "yakutia_real", "yakutia_real_deep", "yakutia_real_bykov", "yakutia_stress")
SHOWN_LINEAR = ("r1_baseline_central_central",)


@app.get("/api/network/scenarios")
def network_scenarios():
    net, lin = network_builtin(), builtin_scenarios()
    linear = [dict(name=k, title=lin[k]["title"], kind="linear") for k in SHOWN_LINEAR if k in lin]
    return dict(builtin=[dict(name=k, title=net[k]["title"], kind="network") for k in SHOWN_NETWORK if k in net] + linear,
                strategies=list(NET_STRATEGIES), job_timeout_s=JOB_TIMEOUT_S,
                max_upload_mb=MAX_BODY_BYTES // 1024 // 1024)


@app.get("/api/network/sample/{name}")
def network_sample(name: str):
    files = {"yakutia": NET_DATA / "yakutia_network.json", "demo": ROOT / "data/network_demo/c_reposition_chain.json",
             "limits": NET_DATA / "limits.json", "tariffs": NET_TARIFFS,
             "stress": NET_DEMO / "yakutia_stress.json", "improvements": NET_DEMO / "improvements.json"}
    if name not in files or not files[name].exists():
        raise HTTPException(404, "Нет такого примера")
    return FileResponse(files[name], filename=files[name].name)


def _network_forecast(raw, tariffs=None):
    """Быстрый прогноз сетевой ML-модели (ml/network_ml) до расчёта ядра; ошибки модели не мешают расчёту."""
    started = time.perf_counter()
    try:
        import importlib.util
        if "network_ml_predict" not in sys.modules:
            spec = importlib.util.spec_from_file_location("network_ml_predict", ROOT / "ml/network_ml/predict.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["network_ml_predict"] = mod
            spec.loader.exec_module(mod)
        out = sys.modules["network_ml_predict"].forecast(raw, tariffs)
    except Exception as e:  # noqa: BLE001 — нет sklearn/модели: сайт работает без прогноза
        out = {"status": "UNAVAILABLE", "error": f"{type(e).__name__}: {e}"}
    return out, round((time.perf_counter() - started) * 1000, 1)


@app.post("/api/network/jobs", status_code=202)
def network_job(req: NetworkRequest):
    """Проверить сетевой сценарий ядром и посчитать 4 стратегии в отдельном процессе."""
    from network.models import Limits, parse_scenario
    if req.files or (req.builtin and req.builtin in builtin_scenarios()):
        # одна река: прежний конвейер (проверка матмоделью, прогноз ML, расчёт трёх стратегий)
        out = create_job(JobRequest(builtin=None if req.files else req.builtin, files=req.files, river=req.river))
        JOBS[out["job_id"]].kind = "linear"
        return dict(job_id=out["job_id"], status="running", kind="linear", forecast=out.get("forecast"),
                    forecast_ms=out.get("forecast_ms"))
    with LOCK:
        if sum(j.status == "running" for j in JOBS.values()) >= MAX_RUNNING:
            raise HTTPException(429, "Сейчас считаются другие сценарии, попробуйте через минуту")
    job_id = "n" + uuid.uuid4().hex[:11]
    job_dir = WORK / job_id
    job_dir.mkdir(parents=True)
    try:
        if req.builtin:
            b = network_builtin().get(req.builtin)
            if not b:
                raise HTTPException(404, "Нет такого готового сценария")
            raw = json.loads(Path(b["scenario"]).read_text(encoding="utf-8-sig"))
            limits = json.loads(Path(b["limits"]).read_text(encoding="utf-8")) if b.get("limits") else None
            tariffs = json.loads(Path(b["tariffs"]).read_text(encoding="utf-8-sig")) if b.get("tariffs") else None
            presets = json.loads(Path(b["presets"]).read_text(encoding="utf-8-sig")) if b.get("presets") else None
            cards, title = b.get("cards"), b["title"].split(":")[0] if req.builtin == "yakutia" else b["title"]
            geo, policy = b.get("geo"), b.get("policy")
            if req.builtin == "yakutia":
                title = "Северный завоз · Якутия"
        elif req.scenario:
            raw, limits, cards, presets, geo, policy = req.scenario, req.limits, None, req.presets, None, None
            tariffs = req.tariffs or json.loads(NET_TARIFFS.read_text(encoding="utf-8"))
            title = str(raw.get("scenario_id", "Свой сценарий"))[:80]
        else:
            raise HTTPException(422, "Нужен builtin или scenario")
        try:                                   # проверка входа самим ядром — до запуска
            parse_scenario(raw)
            if limits:
                Limits(**limits)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, "Проверка сетевого ядра: " + _ru_error(f"{type(e).__name__}: {e}"))
        (job_dir / "scenario.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        if cards:
            shutil.copyfile(cards, job_dir / "cards.json")
        if geo:
            shutil.copyfile(geo, job_dir / "geo.json")
        cmd = [sys.executable, str(ROOT / "yakutia/payload.py"), "--scenario", str(job_dir / "scenario.json"),
               "--title", title, "--out", str(job_dir / "payload.json"), "--results-dir", str(job_dir / "results"),
               "--limits", "none", "--tariffs", "none", "--cards", str(cards) if cards else "none",
               "--geo", str(geo) if geo else "none", "--policy", str(policy) if policy else "none"]
        if presets:
            (job_dir / "presets.json").write_text(json.dumps(presets, ensure_ascii=False), encoding="utf-8")
        for name, data in (("limits", limits), ("tariffs", tariffs)):
            if data:
                (job_dir / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                cmd[cmd.index(f"--{name}") + 1] = str(job_dir / f"{name}.json")
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    job = Job(job_id, job_dir)
    job.kind = "network"
    job.proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_watch, args=(job,), daemon=True).start()
    _evict()
    forecast, forecast_ms = _network_forecast(raw, tariffs)
    (job_dir / "forecast.json").write_text(json.dumps(forecast, ensure_ascii=False), encoding="utf-8")
    return dict(job_id=job_id, status="running", kind="network", forecast=forecast, forecast_ms=forecast_ms)


@app.get("/api/network/jobs/{job_id}")
def network_job_status(job_id: str):
    return job_status(job_id)


@app.get("/api/network/jobs/{job_id}/data")
def network_job_data(job_id: str):
    """Данные страницы в формате network-view/1; одна река переводится из данных map/build_map."""
    j = _job(job_id)
    if j.kind != "linear":
        return job_data(job_id)
    if j.status != "done":
        raise HTTPException(409, f"Задание в состоянии {j.status}")
    view = j.dir / "view.json"
    if not view.exists():
        import linear_view
        data = linear_view.convert(json.loads((j.dir / "payload.json").read_text(encoding="utf-8")))
        view.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return Response(view.read_text(encoding="utf-8"), media_type="application/json")


@app.get("/api/network/jobs/{job_id}/result/{strategy}")
def network_job_result(job_id: str, strategy: str):
    """Полный результат стратегии по контракту v3 (result.v3.schema.json), без изменений."""
    j = _job(job_id)
    if j.kind == "linear":
        return job_result(job_id, strategy)
    if strategy not in NET_STRATEGIES and not re.fullmatch(r"W\d+|X_[A-Z0-9]+", strategy):
        raise HTTPException(404, "Нет такой стратегии")
    path = j.dir / "results" / f"result_{strategy}.json"
    if j.status != "done" or not path.exists():
        raise HTTPException(409, "Результат ещё не готов")
    return FileResponse(path, media_type="application/json", filename=f"result_v3_{strategy}.json")


# ---------- ручки контракта ядра v3 (docs/network/API_CONTRACT.md) поверх заданий ----------
# Формы ответов — как в контракте. Отличие — адресация: вместо встроенного пакета A/B/C/D
# план выбирается внутри задания: variant = имя стратегии (NETWORK_*) или W1, W2… — сохранённый
# результат «Что, если?». По умолчанию — лучшая стратегия задания. Ошибки входа — 400 {"error": ...}.
WHATIF_MAX_BYTES = 65536
WHATIF_SLOTS = threading.BoundedSemaphore(MAX_RUNNING)


class ContractError(Exception):
    pass


@app.exception_handler(ContractError)
def _contract_error(_request: Request, exc: ContractError):
    return JSONResponse({"error": str(exc)}, status_code=400)


def _net_job(job_id: str) -> Job:
    j = _job(job_id)
    if j.kind != "network":
        raise HTTPException(409, "Ручка доступна только для сетевых заданий ядра v3")
    if j.status != "done":
        raise HTTPException(409, f"Задание в состоянии {j.status}")
    if not hasattr(j, "results"):
        j.results, j.whatif = {}, []
        j.best = json.loads((j.dir / "payload.json").read_text(encoding="utf-8"))["best"]
    return j


def _read_opt(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _plan(j: Job, variant: Optional[str]) -> dict:
    """Результат v3 задания по ключу варианта (стратегия или W<n>), с кэшем в памяти."""
    key = variant or j.best
    if not isinstance(key, str):
        raise ContractError("variant must be a string")
    if key not in j.results:
        path = j.dir / "results" / f"result_{key}.json"
        if not re.fullmatch(r"NETWORK_[A-Z_]+|W\d+|X_[A-Z0-9]+", key) or not path.exists():
            raise ContractError(f"Unknown variant: {key}")
        j.results[key] = json.loads(path.read_text(encoding="utf-8"))
    return j.results[key]


def _core_call(fn, *args):
    try:
        return fn(*args)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, StopIteration) as e:
        raise ContractError(str(e) or type(e).__name__)


@app.get("/api/network/jobs/{job_id}/plans")
def network_plans(job_id: str):
    """Ключи вариантов задания для параметров variant/from/to: стратегии и сохранённые «Что, если?»."""
    j = _net_job(job_id)
    strategies = sorted(p.name[len("result_"):-len(".json")] for p in (j.dir / "results").glob("result_NETWORK_*.json"))
    return dict(best=j.best, strategies=strategies, whatif=j.whatif)


@app.get("/api/network/jobs/{job_id}/state")
def network_state(job_id: str, moment: str, variant: Optional[str] = None):
    """Положения всех судов и состояние всех портов на момент moment (контракт: GET /api/state)."""
    from network.analysis import port_at, positions_at
    plan = _plan(_net_job(job_id), variant)
    vessels = _core_call(positions_at, plan, moment)
    return dict(vessels=vessels, ports={port: _core_call(port_at, plan, port, moment) for port in plan["scenario"]["ports"]})


@app.get("/api/network/jobs/{job_id}/port-load")
def network_port_load(job_id: str, port: str, start: str, end: str, variant: Optional[str] = None):
    """Статистика порта за период или null, если причалы порта не моделируются (контракт: GET /api/port-load)."""
    from network.analysis import port_load
    return _core_call(port_load, _plan(_net_job(job_id), variant), port, start, end)


@app.get("/api/network/jobs/{job_id}/explain")
def network_explain(job_id: str, voyage: str, variant: Optional[str] = None):
    """Маршрут, срок, аудит и цена рейса; номер рейса с 1 внутри варианта (контракт: GET /api/explain)."""
    from network.analysis import explain_voyage
    plan = _plan(_net_job(job_id), variant)
    out = _core_call(explain_voyage, plan, voyage)
    v = plan["voyages"][int(voyage) - 1]
    if v.get("batch_id"):          # перевалка: этап партии, терминал и второй рейс той же партии
        out["transfer"] = {"batch_id": v["batch_id"], "stage": v.get("stage"), "port": v.get("transfer_port"),
                           "voyages": [i + 1 for i, w in enumerate(plan["voyages"]) if w.get("batch_id") == v["batch_id"]]}
    return out


@app.get("/api/network/jobs/{job_id}/diagnose")
def network_diagnose(job_id: str, variant: Optional[str] = None):
    """Причины недовоза по заявкам и наблюдаемые очереди {cargo, queues} (контракт: diagnoses[вариант])."""
    from network.analysis import diagnose
    return _core_call(diagnose, _plan(_net_job(job_id), variant))


@app.get("/api/network/jobs/{job_id}/compare")
def network_compare(job_id: str, to: str, from_: str = Query(..., alias="from")):
    """Разницы метрик to − from (контракт: GET /api/compare)."""
    from network.analysis import compare_results
    j = _net_job(job_id)
    return _core_call(compare_results, _plan(j, from_), _plan(j, to))


@app.get("/api/network/jobs/{job_id}/scenario")
def network_scenario(job_id: str):
    """Входной сценарий задания (порты, рёбра, флот, заявки, причалы) — для конструктора изменений."""
    return Response((_net_job(job_id).dir / "scenario.json").read_text(encoding="utf-8"), media_type="application/json")


@app.get("/api/network/jobs/{job_id}/presets")
def network_presets(job_id: str):
    """Готовые изменения для «Что, если?»: индекс в массиве — значение preset."""
    return _read_opt(_net_job(job_id).dir / "presets.json") or []


@app.post("/api/network/jobs/{job_id}/what-if")
def network_what_if(job_id: str, request: Request, body: dict = Body(...)):
    """Пересчитать одно изменение относительно базового варианта (контракт: POST /api/what-if).

    Тело: {"preset": 0} или {"change": {...}}; опционально "base" — вариант-основа (по умолчанию
    лучшая стратегия задания). Ответ — {baseline_fingerprint, improvement_found, alternatives}
    по контракту и дополнительно "variant": ключ, под которым изменённый план сохранён в задании —
    его можно передать в state/port-load/explain/diagnose/compare, и "view" — тот же план в формате
    страницы (network-view/1, как strategies[*] в /data), чтобы показать его на карте.
    """
    from network.analysis import find_improvements
    from network.models import Limits
    size = request.headers.get("content-length")
    if not size or not size.isdigit() or not 0 < int(size) <= WHATIF_MAX_BYTES:
        raise ContractError("Provide one change as JSON, up to 64 KiB")
    j = _net_job(job_id)
    base_key = body.get("base") or j.best
    base = _plan(j, base_key)
    if "preset" in body:
        presets = _read_opt(j.dir / "presets.json") or []
        index = body["preset"]
        if type(index) is not int or not 0 <= index < len(presets):
            raise ContractError("Unknown preset")
        option = presets[index]
    else:
        option = body.get("change")
    if not isinstance(option, dict):
        raise ContractError("Provide one change object")
    limits = _read_opt(j.dir / "limits.json")
    tariffs = _read_opt(j.dir / "tariffs.json")
    raw = json.loads((j.dir / "scenario.json").read_text(encoding="utf-8"))
    if not WHATIF_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Сейчас считаются другие изменения, попробуйте через минуту")
    try:
        report = _core_call(lambda: find_improvements(raw, base, [option], strategy=base["strategy"],
                                                      limits=Limits(**limits) if limits else None, tariffs=tariffs))
    finally:
        WHATIF_SLOTS.release()
    result = report["alternatives"][0]["result"]
    with LOCK:
        key = f"W{len(j.whatif) + 1}"
        j.whatif.append(dict(variant=key, base=base_key, change=option))
        j.results[key] = result
    (j.dir / "results" / f"result_{key}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    import payload as net_view
    view = net_view.view_for(result["scenario"], result, cards=_read_opt(j.dir / "cards.json"),
                             geo=_read_opt(j.dir / "geo.json"),
                             label=f"Что, если {key[1:]}", hint=f"Изменение относительно «{base_key}»")
    known = {v["vessel_id"] for fleet in ("tugs", "barges") for v in raw[fleet]}
    view["extra_fleet"] = [{**{k: v for k, v in r.items() if k not in ("available_at", "available_to")},
                            "kind": kind, "card": None}
                           for kind, fleet in (("tug", "tugs"), ("barge", "barges"))
                           for r in result["scenario"][fleet] if r["vessel_id"] not in known]
    return dict(variant=key, base=base_key, view=view, **report)


# ---------- заранее посчитанные эксперименты с морским флотом (data/yakutia_real/sea_experiments.py) ----------
SEA_EXP = ROOT / "data/yakutia_real/scenario"
SEA_SHOWN = ("E1", "E1P", "E2")   # что показывать в «Что, если?»: обычное планирование, морские первыми, +флот


def _sea_report():
    out, rows = {}, {}
    for kind, name in (("base", "sea_experiments_report.json"), ("social", "sea_experiments_social.json")):
        data = _read_opt(SEA_EXP / name)
        if isinstance(data, list):            # старый формат отчёта: только список экспериментов
            data = {"settings": None, "experiments": data}
        if isinstance(data, dict):
            out[kind] = data.get("settings")
            for r in data.get("experiments", []):
                rows[r["name"]] = r
    return out, rows


@app.get("/api/network/experiments")
def network_experiments():
    """Проверенные эксперименты E0/E1/E1P/E2/...: показатели, структура стоимости, настройки и версия ядра."""
    settings, rows = _sea_report()
    saved = {p.name.split(".")[0] for p in (SEA_EXP / "sea_results").glob("*.json.gz")}
    return dict(settings=settings, shown=[k for k in SEA_SHOWN if k in rows],
                experiments=[dict(r, saved=r["name"] in saved) for r in rows.values()])


@app.post("/api/network/jobs/{job_id}/experiments/{name}")
def network_job_experiment(job_id: str, name: str):
    """Подключить сохранённый план эксперимента к заданию как вариант X_<name>: карта, compare, diagnose, explain."""
    import gzip
    if not re.fullmatch(r"[A-Z0-9]{2,6}", name):
        raise ContractError("Unknown experiment")
    path = SEA_EXP / "sea_results" / f"{name}.json.gz"
    _, rows = _sea_report()
    if not path.exists() or name not in rows:
        raise ContractError(f"Experiment {name} is not saved: run data/yakutia_real/sea_experiments.py")
    j = _net_job(job_id)
    key = f"X_{name}"
    with LOCK:
        result = j.results.get(key)
    if result is None:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            result = json.load(f)
        with LOCK:
            j.results[key] = result
        (j.dir / "results" / f"result_{key}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    row = rows[name]
    raw = json.loads((j.dir / "scenario.json").read_text(encoding="utf-8"))
    import payload as net_view
    view = net_view.view_for(result["scenario"], result, cards=_read_opt(j.dir / "cards.json"),
                             geo=_read_opt(j.dir / "geo.json"), label=f"{name}: {row['title']}",
                             hint=f"Эксперимент {name}, заранее посчитан и проверен аудитом")
    view["seconds"] = row.get("seconds")
    known = {v["vessel_id"] for fleet in ("tugs", "barges") for v in raw[fleet]}
    view["extra_fleet"] = [{**{k: v for k, v in r.items() if k not in ("available_at", "available_to")},
                            "kind": kind, "card": None}
                           for kind, fleet in (("tug", "tugs"), ("barge", "barges"))
                           for r in result["scenario"][fleet] if r["vessel_id"] not in known]
    return dict(variant=key, view=view, experiment=row)


@app.on_event("startup")
def _warmup():
    """Загрузить веса заранее, чтобы первый прогноз не ждал пару секунд."""
    if _recommend is not None:
        def run():
            try:
                from predict import _load, HERE as model_dir
                _load(str(model_dir))
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()
    sample = NET_DATA / "yakutia_network.json"
    if sample.exists():                       # сетевая модель: прогреть загрузку весов
        threading.Thread(target=lambda: _network_forecast(json.loads(sample.read_text(encoding="utf-8"))),
                         daemon=True).start()


@app.on_event("shutdown")
def _cleanup():
    for j in JOBS.values():
        if j.proc and j.proc.poll() is None:
            j.proc.kill()
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(description="Веб-приложение планировщика Case A3")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
