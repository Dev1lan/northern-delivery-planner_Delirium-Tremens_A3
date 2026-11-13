"""Быстрый сетевой прогноз до расчёта: доля тоннажа в срок по стратегиям и предварительный выбор.

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


def forecast(raw):
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
    best = max(by, key=lambda s: by[s]["on_time_share"])
    return {"status": "OUT_OF_DISTRIBUTION" if outside else "OK", "outside_features": outside,
            "core_version": VERSION, "by_strategy": by, "recommended": None if outside else best,
            "note": "Оценка до расчёта; итог и аудит — только сетевое ядро."}


if __name__ == "__main__":
    print(json.dumps(forecast(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))), ensure_ascii=False, indent=1))
