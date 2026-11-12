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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (ROOT, ROOT / "ml", ROOT / "map"):
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
            title = f"Синтетический маршрут, {km:.0f} км"
        except Exception:
            title = "Синтетический маршрут"
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
    html = (ROOT / "map/template.html").read_text(encoding="utf-8")
    return html.replace("/*__APP__*/false", "true")


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
