"""Запуск алгоритмов и единая финансовая оценка расписаний.

FCFS и OPTIMIZED запускаются по контракту 1.0.0.
SOCIAL_PRIORITY запускается по контракту 2.0.0 с социальными требованиями.
Для всех стратегий формируется один и тот же формат финансового отчёта.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from result_adapter import (
    ALL_STRATEGIES,
    CONTRACT_VERSION,
    DEFAULT_CONFIG,
    EXTENDED_CONTRACT_VERSION,
    SOCIAL_PRIORITY,
    run_scenario,
)
from economics.cost_model import evaluate_schedule, load_cost_config
from economics.cost_comparison import compare_reports


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_COST_CONFIG = BASE_DIR / "cost_config.json"
DEMO_STRATEGIES = ("FCFS", "OPTIMIZED", "EARLIEST_DUE", SOCIAL_PRIORITY)


def _read_requirements(path: Path | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None

    try:
        requirements = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный JSON социальных требований: {path}") from exc

    if not isinstance(requirements, list):
        raise ValueError("Файл социальных требований должен содержать JSON-массив")
    return requirements


def _run_one_strategy(
    *,
    scenario: Path,
    config_path: Path,
    cost_config: dict[str, Any],
    strategy: str,
    scenario_id: str | None = None,
    candidate_limit: int | None = None,
    requirements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Формирует расписание и возвращает финансовый отчёт одной стратегии."""
    if strategy == SOCIAL_PRIORITY:
        if requirements is None:
            raise ValueError(
                "Для SOCIAL_PRIORITY укажите --requirements с JSON-файлом социальных минимумов"
            )
        contract_version = EXTENDED_CONTRACT_VERSION
        strategy_requirements = requirements
    else:
        # Не передаём социальные минимумы базовым стратегиям:
        # они должны оставаться независимыми точками сравнения.
        contract_version = CONTRACT_VERSION
        strategy_requirements = None

    operational_result = run_scenario(
        scenario,
        config_path=config_path,
        strategy=strategy,
        scenario_id=scenario_id,
        candidate_limit=candidate_limit,
        contract_version=contract_version,
        requirements=strategy_requirements,
    )
    return evaluate_schedule(operational_result, cost_config)


def _print_report(report: dict[str, Any], output_path: Path) -> None:
    metrics = report["operational_metrics"]
    cost_per_ton = report["cost_per_delivered_ton"]

    print(f"Финансовый отчёт сохранён: {output_path}")
    print(f"Стратегия: {report['strategy']}")
    print(f"Контракт: {report.get('source_schema_version', 'не указан')}")
    print(f"Доставлено: {metrics['delivered_total_t']:.3f} т")
    print(f"Доставлено в срок: {metrics['delivered_on_time_t']:.3f} т")
    print(f"Количество рейсов: {metrics['completed_trips']}")
    print(f"Общие затраты: {report['total_cost']:,.2f} {report['currency']}")
    if cost_per_ton is None:
        print("Затраты на тонну: нет доставленного груза")
    else:
        print(f"Затраты на тонну: {cost_per_ton:,.2f} {report['currency']}/т")


def _write_report(report: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Единый запуск алгоритмов и финансовая оценка речных перевозок"
    )
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--strategy", choices=ALL_STRATEGIES, default="OPTIMIZED")
    parser.add_argument(
        "--all-strategies",
        action="store_true",
        help="Последовательно запустить FCFS, OPTIMIZED и SOCIAL_PRIORITY, затем сравнить их",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cost-config", type=Path, default=DEFAULT_COST_CONFIG)
    parser.add_argument(
        "--requirements",
        type=Path,
        help="JSON социальных минимумов; обязателен для SOCIAL_PRIORITY",
    )
    parser.add_argument("--scenario-id")
    parser.add_argument("--candidate-limit", type=int)
    parser.add_argument("--output", type=Path, default=Path("results/economics.json"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/economics_all"),
        help="Каталог финансовых отчётов при использовании --all-strategies",
    )
    args = parser.parse_args(argv)

    if args.candidate_limit is not None and args.candidate_limit < 1:
        parser.error("--candidate-limit должен быть положительным числом")

    if not args.scenario.exists():
        parser.error(f"Папка сценария не найдена: {args.scenario}")
    if not args.config.exists():
        parser.error(f"Файл конфигурации сценария не найден: {args.config}")
    if not args.cost_config.exists():
        parser.error(f"Файл финансовой конфигурации не найден: {args.cost_config}")
    if args.requirements is not None and not args.requirements.exists():
        parser.error(f"Файл социальных требований не найден: {args.requirements}")

    try:
        requirements = _read_requirements(args.requirements)
        cost_config = load_cost_config(args.cost_config)

        if args.all_strategies:
            if requirements is None:
                parser.error("Для --all-strategies укажите --requirements")

            args.output_dir.mkdir(parents=True, exist_ok=True)
            report_paths: list[Path] = []

            for strategy in DEMO_STRATEGIES:
                report = _run_one_strategy(
                    scenario=args.scenario,
                    config_path=args.config,
                    cost_config=cost_config,
                    strategy=strategy,
                    scenario_id=args.scenario_id,
                    candidate_limit=args.candidate_limit,
                    requirements=requirements,
                )
                report_path = args.output_dir / f"economics_{strategy.lower()}.json"
                _write_report(report, report_path)
                report_paths.append(report_path)
                print()
                _print_report(report, report_path)

            comparison = compare_reports(report_paths)
            comparison_path = args.output_dir / "economics_comparison.json"
            comparison_path.write_text(
                json.dumps(comparison, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )

            print("\nСравнение стратегий:")
            for row in comparison["strategies"]:
                cost_per_ton = row["cost_per_delivered_ton"]
                cost_per_ton_text = (
                    f"{cost_per_ton:,.2f} {comparison['currency']}/т"
                    if cost_per_ton is not None else "нет доставленного груза"
                )
                print(
                    f"{row['strategy']}: "
                    f"{row['total_cost']:,.2f} {comparison['currency']}; "
                    f"{row['delivered_total_t']:.3f} т; "
                    f"{cost_per_ton_text}"
                )
            print(f"Сводное сравнение сохранено: {comparison_path}")
            return

        if args.strategy == SOCIAL_PRIORITY and requirements is None:
            parser.error("Для SOCIAL_PRIORITY обязательно укажите --requirements")
        if args.strategy != SOCIAL_PRIORITY and requirements is not None:
            parser.error(
                "--requirements используется только для SOCIAL_PRIORITY; "
                "для пакетного запуска используйте --all-strategies"
            )

        report = _run_one_strategy(
            scenario=args.scenario,
            config_path=args.config,
            cost_config=cost_config,
            strategy=args.strategy,
            scenario_id=args.scenario_id,
            candidate_limit=args.candidate_limit,
            requirements=requirements,
        )
        _write_report(report, args.output)
        _print_report(report, args.output)

    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(2, f"Ошибка финансового расчёта: {exc}\n")


if __name__ == "__main__":
    main()
