"""
Карта маршрута с графом, рейсами и нагрузкой — из результата по контракту 1.0.0.

  python map/build_map.py --scenario data/demo/scenario --out map/demo_map.html
  python map/build_map.py --scenario <каталог> --strategies OPTIMIZED FCFS --no-ml

Что делает:
  1) читает сценарий (cargo.csv, units.csv, conditions.csv) и конфигурацию;
  2) для каждой стратегии вызывает result_adapter.run_scenario — расписание строит
     и проверяет только матмодель; результат проверяется по result.schema.json;
  2а) определяет лучшую из рассчитанных стратегий по целевой функции матмодели
      (тоннаж вовремя, общий тоннаж, меньше рейсов) — она открывается по умолчанию;
  3) при наличии ML-модуля (ml/) добавляет прогноз «до расчёта» отдельным объектом;
  4) собирает граф маршрута: узлы и участки — из config["segments"]; если есть
     файл геометрии (map/geo/*.json) с теми же пунктами отправления и назначения,
     узлы кладутся на реальную линию русла, иначе рисуется схема;
  5) вставляет всё в map/template.html и пишет один самодостаточный HTML
     (без внешних библиотек и сети).

Файлы матмодели и контракт не меняются. Страница только показывает данные:
нагрузка участков и положение судов выводятся из trips[].events результата.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (ROOT, ROOT / "ml", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from optimizer import STRATEGIES, load  # noqa: E402
from a3_bridge import pick_best  # noqa: E402  (только стандартная библиотека; зависимости ML не нужны)
from result_adapter import run_scenario  # noqa: E402

TRIP_KEEP = ["trip_id", "unit_id", "cargo_id", "cargo_type", "volume_t", "loading_start", "departure",
             "arrival", "unloading_end", "released_at", "loaded_draft_m", "due_date", "on_time", "events"]


TIME_KEYS = ("loading_start", "departure", "arrival", "unloading_end", "released_at", "due_date")


def _epoch(iso_text: str) -> int:
    """Время контракта (ISO без зоны) -> целые секунды; на странице трактуется так же, как UTC."""
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(iso_text).replace(tzinfo=timezone.utc).timestamp())


def compact_trip(t: dict) -> dict:
    """Рейс из результата по контракту в компактном виде для страницы (значения те же)."""
    out = {k: t[k] for k in TRIP_KEEP if k not in TIME_KEYS and k != "events"}
    out.update({k: _epoch(t[k]) for k in TIME_KEYS})
    out["events"] = [[e["segment_id"], 1 if e["direction"] == "upstream" else -1, _epoch(e["start"]), _epoch(e["end"])]
                     for e in t["events"]]
    return out


def read_config(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def validate(result: dict) -> bool:
    """Проверка по result.schema.json. Без пакета jsonschema — пропускается (False)."""
    try:
        import jsonschema
    except ImportError:
        return False
    schema = json.loads((ROOT / "result.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(result)
    return True


def find_geo(config: dict, geo_path: Path | None, river: str | None = None, online: bool | None = None):
    """Геометрия маршрута: явный файл -> библиотека русел (и OpenStreetMap, если реки там нет) -> готовые файлы map/geo."""
    if geo_path:
        return json.loads(Path(geo_path).read_text(encoding="utf-8"))
    try:
        import geo_resolver
        geo = geo_resolver.resolve(config, river, online)
        if geo:
            return geo
    except Exception:
        pass                                                 # без геометрии маршрут рисуется схемой
    for f in sorted((HERE / "geo").glob("*.json")):
        g = json.loads(f.read_text(encoding="utf-8"))
        if g.get("origin") == config["origin"] and g.get("destination") == config["destination"]:
            return g
    return None


def ml_recommendation(scenario: Path, config_path: Path):
    """Прогноз ML до расчёта. Модуль необязателен: при любой ошибке импорта карта строится без него."""
    try:
        from predict import recommend_for_scenario
    except Exception as e:                                   # нет numpy/sklearn/torch или папки ml/
        return dict(available=False, reason=f"ML-модуль недоступен: {type(e).__name__}")
    rec = recommend_for_scenario(scenario, config_path)
    rec.pop("features", None)
    rec["available"] = True
    return rec


def build_payload(scenario: Path, config_path: Path, strategies, geo_path, use_ml: bool, title: str | None,
                  results_dir: Path | None = None, river: str | None = None, geo_online: bool | None = None) -> dict:
    config = read_config(config_path)
    s = load(scenario, config)                               # включает проверку входов матмодели
    cargo, units, cond = s["cargo"], s["units"], s["conditions"]

    seg_ids = [g["segment_id"] for g in config["segments"]]
    nodes, km = [dict(id=config["segments"][0]["from_node"], km=0.0)], 0.0
    segments = []
    for g in config["segments"]:
        segments.append(dict(id=g["segment_id"], from_node=g["from_node"], to_node=g["to_node"],
                             km=float(g["distance_km"]), km_from=km))
        km += float(g["distance_km"])
        nodes.append(dict(id=g["to_node"], km=round(km, 3)))

    days = sorted({r["date"] for r in cond})
    by = {(r["date"], r["segment_id"]): r for r in cond}
    conditions = {}
    for sid in seg_ids:
        allowed, depth, closed = [], [], []
        for d in days:
            r = by.get((d, sid))
            if r is None:
                allowed.append(None); depth.append(None); closed.append(1); continue
            depth.append(round(float(r["usable_depth_m"]), 3))
            allowed.append(round(min(float(r["max_allowed_draft_m"]),
                                     float(r["usable_depth_m"]) - config["underkeel_m"]), 3))
            closed.append(int(r["is_closed"]))
        conditions[sid] = dict(allowed_draft_m=allowed, depth_m=depth, closed=closed)

    results = {}
    for st in strategies:
        res = run_scenario(scenario, config_path=config_path, strategy=st)
        checked = validate(res)
        if results_dir:                                      # полный результат по контракту — как есть
            Path(results_dir).mkdir(parents=True, exist_ok=True)
            (Path(results_dir) / f"result_{st}.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        results[st] = dict(metrics=res["metrics"], schema_version=res["schema_version"], schema_validated=checked,
                           trips=[compact_trip(t) for t in res["trips"]])

    # лучшая из рассчитанных стратегий — по целевой функции матмодели, не по мнению нейросети
    if results:
        best = pick_best({st: r["metrics"] for st, r in results.items()})
        default = strategies[0] if strategies[0] in best["tied_strategies"] else best["best_strategy"]
    else:                                                    # только входы: предпросмотр до расчёта
        best, default = None, None

    allowed_types = lambda u: u["cargo_type_allowed"] if isinstance(u["cargo_type_allowed"], list) \
        else str(u["cargo_type_allowed"]).split("|")
    return dict(
        title=title or f"{config['origin']} — {config['destination']}",
        scenario_id=s["id"],
        disclaimer=("Исследовательский прототип. Заявки, флот, осадки и глубины в сценарии синтетические или "
                    "предположительные; это не данные о фактическом завозе и не система управления судоходством."),
        route=dict(origin=config["origin"], destination=config["destination"], nodes=nodes, segments=segments,
                   length_km=round(km, 3), geo=find_geo(config, geo_path, river, geo_online)),
        navigation=dict(start=config["start"], end=config["end"], underkeel_m=config["underkeel_m"]),
        days=days, conditions=conditions,
        units=[dict(unit_id=u["unit_id"], capacity_t=float(u["capacity_t"]), draft_empty_m=float(u["draft_empty_m"]),
                    draft_full_m=float(u["draft_full_m"]), tug_draft_m=float(u["tug_draft_m"]),
                    cargo_types=allowed_types(u)) for u in units],
        cargo=dict(total_t=round(sum(float(x["volume_t"]) for x in cargo), 3),
                   lots=[dict(id=x["cargo_id"], type=x["cargo_type"], volume_t=float(x["volume_t"]),
                              ready=x["ready_date"], due=x["due_date"]) for x in cargo]),
        strategies=results, default_strategy=default, best=best,
        ml=ml_recommendation(scenario, config_path) if use_ml else dict(available=False, reason="ML отключён (--no-ml)"),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="Карта маршрута с графом и нагрузкой (Case A3)")
    ap.add_argument("--scenario", type=Path, default=ROOT / "data/demo/scenario",
                    help="каталог с cargo.csv, units.csv, conditions.csv")
    ap.add_argument("--config", type=Path, default=ROOT / "scenario_config.json")
    ap.add_argument("--strategies", nargs="+", default=["OPTIMIZED", "EARLIEST_DUE", "FCFS"], choices=list(STRATEGIES))
    ap.add_argument("--geo", type=Path, help="файл геометрии маршрута; по умолчанию ищется в map/geo/")
    ap.add_argument("--river", help="название реки: русло берётся из библиотеки map/geo/rivers или из OpenStreetMap")
    ap.add_argument("--no-ml", action="store_true", help="не добавлять прогноз ML")
    ap.add_argument("--title")
    ap.add_argument("--out", type=Path, default=HERE / "demo_map.html")
    ap.add_argument("--data-out", type=Path, help="дополнительно сохранить данные карты в JSON")
    ap.add_argument("--results-dir", type=Path, help="сохранить полные результаты по контракту 1.0.0 (result_<стратегия>.json)")
    a = ap.parse_args(argv)

    payload = build_payload(a.scenario, a.config, a.strategies, a.geo, not a.no_ml, a.title, a.results_dir, a.river)
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    template = (HERE / "template.html").read_text(encoding="utf-8")
    marker = "/*__MAP_DATA__*/null"
    assert marker in template, "в template.html нет метки для данных"
    a.out.write_text(template.replace(marker, data.replace("</", "<\\/")), encoding="utf-8")
    if a.data_out:
        a.data_out.write_text(data, encoding="utf-8")
    m = {st: r["metrics"]["delivered_on_time_t"] for st, r in payload["strategies"].items()}
    print(f"лучшая стратегия: {payload['best']['tied_strategies']}")
    print(f"{a.out}: сценарий {payload['scenario_id']}, тонн вовремя по стратегиям: {m}, "
          f"геометрия: {'русло' if payload['route']['geo'] else 'схема'}, ML: {payload['ml'].get('available')}")


if __name__ == "__main__":
    main()
