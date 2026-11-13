"""Сравнение финансовых отчётов для стратегий одного сценария."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def compare_reports(paths: list[str | Path]) -> dict:
    if len(paths) < 2:
        raise ValueError("Для сравнения передайте минимум два финансовых отчёта")

    reports = [
        json.loads(Path(path).read_text(encoding="utf-8"))
        for path in paths
    ]
    scenario_ids = {report.get("scenario_id") for report in reports}
    currencies = {report.get("currency") for report in reports}
    if len(scenario_ids) != 1:
        raise ValueError("Нельзя сравнивать разные сценарии")
    if len(currencies) != 1:
        raise ValueError("Нельзя сравнивать отчёты в разных валютах")

    rows = []
    for report in reports:
        rows.append({
            "strategy": report.get("strategy"),
            "total_cost": report["total_cost"],
            "delivered_total_t": report["operational_metrics"]["delivered_total_t"],
            "delivered_on_time_t": report["operational_metrics"]["delivered_on_time_t"],
            "cost_per_delivered_ton": report["cost_per_delivered_ton"],
            "data_status": report.get("data_status", "UNKNOWN"),
        })

    return {
        "comparison_schema_version": "1.0.0",
        "scenario_id": next(iter(scenario_ids)),
        "currency": next(iter(currencies)),
        "data_status": "ASSUMED" if any(
            row["data_status"] == "ASSUMED" for row in rows
        ) else "MIXED_OR_SOURCE",
        "strategies": rows,
        "lowest_total_cost_strategy": min(rows, key=lambda x: x["total_cost"])["strategy"],
        "lowest_cost_per_ton_strategy": (
            min(
                (row for row in rows if row["cost_per_delivered_ton"] is not None),
                key=lambda x: x["cost_per_delivered_ton"],
                default=None,
            ) or {}
        ).get("strategy"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Сравнить финансовые отчёты стратегий")
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("results/economics_comparison.json"))
    args = parser.parse_args(argv)

    comparison = compare_reports(args.reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"Сравнение сохранено: {args.output}")


if __name__ == "__main__":
    main()
