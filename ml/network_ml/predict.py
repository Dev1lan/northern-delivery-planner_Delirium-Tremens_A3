"""Быстрый сетевой прогноз до расчёта: все итоговые метрики ядра по стратегиям и предварительный выбор.

Прогноз не заменяет ядро: расписание и аудит считает только network. Если сценарий выходит за диапазон
обучающих признаков, модель обучена на другой версии ядра или модели нет — прогноз не выдаётся.

    ml/.venv/bin/python ml/network_ml/predict.py data/yakutia_real/scenario/yakutia_real_2026_demo.json
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from features import features  # noqa: E402

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        import joblib
        _MODEL = joblib.load(HERE / "model.joblib")
    return _MODEL


MONEY = ("total_cost", "operating_cost", "cost_per_t_on_time", "undelivered_penalty", "empty_transition_cost")
TRAIN_TARIFFS = HERE.parents[1] / "data/yakutia/tariffs_v2.json"


def forecast(raw, tariffs=None):
    """tariffs — тарифы расчёта; если они отличаются от обучающих, денежные метрики не выдаются."""
    from network import VERSION
    import numpy as np
    import pandas as pd
    path = HERE / "model.joblib"
    if not path.exists():
        return {"status": "NO_MODEL"}
    m = _model()
    if m["core_version"] != VERSION:
        return {"status": "CORE_VERSION_MISMATCH", "model_core": m["core_version"], "core": VERSION}
    f = features(raw)
    outside = [k for k in m["features"] if not m["ood_lo"][k] - 1e-9 <= f[k.removeprefix("f_")] <= m["ood_hi"][k] + 1e-9]
    demand = f["demand_t"]
    rows = []
    for s in m["strategies"]:
        x = {k: f[k.removeprefix("f_")] for k in m["features"]} | {f"s_{t}": float(t == s) for t in m["strategies"]}
        rows.append(x)
    X = pd.DataFrame(rows)
    p = np.stack([mdl.predict(X) for mdl in m["models"]]).clip(0, 1)
    mean, std = p.mean(0), p.std(0)
    by = {s: {"on_time_share": round(float(mu), 4), "on_time_t": round(float(mu * demand), 1),
              "spread_share": round(float(sd), 4)} for s, mu, sd in zip(m["strategies"], mean, std)}
    _all_metrics(m, X, f, by, mean)
    money = tariffs is None or tariffs == json.loads(TRAIN_TARIFFS.read_text(encoding="utf-8-sig"))
    if not money:
        for v in by.values():
            for k in MONEY:
                v.get("metrics", {}).pop(k, None)
    best = max(by, key=lambda s: by[s]["on_time_share"])
    return {"status": "OUT_OF_DISTRIBUTION" if outside else "OK", "outside_features": outside,
            "core_version": VERSION, "by_strategy": by, "recommended": None if outside else best,
            "money": "OK" if money else "SKIPPED_OTHER_TARIFFS",
            "interval_note": "lo–hi: ≈80% отложенных сценариев той же сети попадают в интервал; не гарантия",
            "note": "Оценка до расчёта; итог и аудит — только сетевое ядро."}


def _all_metrics(m, X, f, by, on_time_share):
    """Все итоговые метрики ядра по каждой стратегии: {имя: {value, lo, hi}}, согласованные между собой."""
    import numpy as np
    extra = m.get("extra")
    if not extra:          # старая модель без дополнительных целей
        return
    demand = f["demand_t"]
    critical_demand = demand * f.get("share_critical", 1.0)

    from train import scales
    size = scales(f)

    def band(pred, scale, q):
        top = 1.0 if scale == "demand_share" else None      # тонны не больше спроса
        nat = lambda v: np.clip(v, 0, top) * size["demand" if top else scale]
        return nat(pred), nat(pred - q), nat(pred + q)

    raw = {"delivered_on_time_t": band(on_time_share, "demand_share", m.get("on_time_q80", 0.0))}
    tonnes = {"delivered_total_t", "critical_on_time_t"}
    for name, e in extra.items():
        raw[name] = band(e["model"].predict(X), "demand_share" if name in tonnes else e["scale"], e["q80"])
    for i, s in enumerate(m["strategies"]):
        v = {name: [float(a[i]) for a in triple] for name, triple in raw.items()}
        on_time = v["delivered_on_time_t"]
        # согласованность: всего ≥ в срок; критичные в срок ≤ критичного спроса и ≤ в срок
        v["delivered_total_t"] = [min(demand, max(t, o)) for t, o in zip(v["delivered_total_t"], on_time)]
        v["critical_on_time_t"] = [min(c, critical_demand, o) for c, o in zip(v["critical_on_time_t"], on_time)]
        total = v["delivered_total_t"]
        v["undelivered_t"] = [demand - total[0], demand - total[2], demand - total[1]]
        cost = v["total_cost"] = [a + b for a, b in zip(v["operating_cost"], v["undelivered_penalty"])]
        v["cost_per_t_on_time"] = [cost[0] / on_time[0] if on_time[0] > 0 else None,
                                   cost[1] / on_time[2] if on_time[2] > 0 else None,
                                   cost[2] / on_time[1] if on_time[1] > 0 else None]
        whole = {"completed_voyages", "repositioning_voyages"}
        by[s]["metrics"] = {
            name: dict(zip(("value", "lo", "hi"),
                           (None if x is None else round(x) if name in whole else round(x, 1) for x in triple)))
            for name, triple in v.items()}


if __name__ == "__main__":
    print(json.dumps(forecast(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))), ensure_ascii=False, indent=1))
