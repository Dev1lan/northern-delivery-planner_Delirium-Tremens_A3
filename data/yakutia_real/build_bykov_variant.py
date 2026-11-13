"""Вариант «что если»: реальный сценарий 2026 + условный перевалочный терминал в Быковом Мысе.

Параметры терминала — синтетическое допущение из experiments/bykov_transfer_assumption.json (один причал
100 т/ч для сухих грузов, хранение без ограничения, без отдельной ставки). Подтверждённой инфраструктурой не является.

    ml/.venv/bin/python data/yakutia_real/build_bykov_variant.py
"""
import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCEN = ROOT / "data/yakutia_real/scenario"
OVERLAY = ROOT / "experiments/bykov_transfer_assumption.json"


def with_terminal(raw, overlay):
    out = deepcopy(raw)
    for field in ("port_operations", "transshipment_ports"):
        out[field] = {**out.get(field, {}), **deepcopy(overlay[field])}
    out["scenario_id"] = raw["scenario_id"] + "_bykov"
    return out


def main():
    raw = json.loads((SCEN / "yakutia_real_2026_demo.json").read_text(encoding="utf-8"))
    overlay = json.loads(OVERLAY.read_text(encoding="utf-8-sig"))
    (SCEN / "yakutia_real_2026_bykov.json").write_text(
        json.dumps(with_terminal(raw, overlay), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
