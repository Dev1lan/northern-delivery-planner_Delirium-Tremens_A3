"""Расчёт затрат по сформированному расписанию (модель 1.2.0).

Модуль не выбирает рейсы и не меняет логистический оптимизатор.
Поддерживает контракты result_adapter 1.0.0 и 2.0.0.

Неподтверждённые дополнительные статьи можно оставить null: они не попадут
в total_cost, но будут явно перечислены в excluded_cost_items отчёта.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

MODEL_VERSION = "1.2.0"

# Дополнительные расходы, которые можно включать после подтверждения ставок.
# Для всех них важно исключить двойной учёт с daily_operating_cost_rub.
ADDITIONAL_COST_SPECS = {
    "crew": {
        "basis": "occupied_day",
        "description": "Оплата экипажа и начисления работодателя",
        "rate_key": "rate_rub_per_occupied_day_by_unit",
    },
    "maintenance": {
        "basis": "propulsion_hour",
        "description": "ТО, расходные материалы и ремонтный резерв",
        "rate_key": "rate_rub_per_propulsion_hour_by_unit",
    },
    "port_navigation": {
        "basis": "per_trip",
        "description": "Портовые, навигационные и иные сборы за рейс",
        "rate_key": "rate_rub_per_trip_by_unit",
    },
}
SEASONAL_COST_SPECS = {
    "depreciation": "Амортизация флота за навигационный сезон",
    "insurance": "Страхование судов и ответственности за навигационный сезон",
    "wintering": "Подготовка к зимнему отстою и зимнее хранение",
    "shore_overhead": "Распределённые береговые и общехозяйственные расходы",
}


def _non_negative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} должен быть числом")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} должен быть числом") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} должно быть конечным числом")
    if number < 0:
        raise ValueError(f"{name} не может быть отрицательным")
    return number


def _validate_rate_map(value: Any, name: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{name} должен быть JSON-объектом")
    for unit_id, rate in value.items():
        if rate is not None:
            _non_negative(rate, f"{name}.{unit_id}")


def load_cost_config(path: str | Path) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = (
        "currency",
        "fuel_price_per_l",
        "fuel_consumption_lph_by_unit",
        "daily_operating_cost_rub",
        "handling_cost_per_ton_rub",
    )
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"В cost_config отсутствуют поля: {', '.join(missing)}")

    _non_negative(config["fuel_price_per_l"], "fuel_price_per_l")
    if not isinstance(config["fuel_consumption_lph_by_unit"], dict):
        raise ValueError("fuel_consumption_lph_by_unit должен быть JSON-объектом")
    for unit_id, rates in config["fuel_consumption_lph_by_unit"].items():
        for direction in ("upstream", "downstream"):
            if direction not in rates:
                raise ValueError(f"Для {unit_id} не задан расход топлива {direction}")
            _non_negative(rates[direction], f"{unit_id}.{direction}")

    for key in ("tug", "barge"):
        if key not in config["daily_operating_cost_rub"]:
            raise ValueError(f"Не задана суточная ставка {key}")
        _non_negative(config["daily_operating_cost_rub"][key], key)
    for key in ("loading", "unloading"):
        if key not in config["handling_cost_per_ton_rub"]:
            raise ValueError(f"Не задан тариф {key}")
        _non_negative(config["handling_cost_per_ton_rub"][key], key)
    _non_negative(config.get("other_cost_per_trip_rub", 0), "other_cost_per_trip_rub")

    additional = config.get("additional_costs", {})
    if not isinstance(additional, dict):
        raise ValueError("additional_costs должен быть JSON-объектом")
    for cost_name, spec in ADDITIONAL_COST_SPECS.items():
        item = additional.get(cost_name, {})
        if not isinstance(item, dict):
            raise ValueError(f"additional_costs.{cost_name} должен быть JSON-объектом")
        rates = item.get(spec["rate_key"], {})
        _validate_rate_map(rates, f"additional_costs.{cost_name}.{spec['rate_key']}")
        # Защита от двойного учёта: числовая дополнительная ставка допустима
        # только после явного подтверждения, что она не сидит в суточной ставке.
        has_numeric_rate = any(rate is not None for rate in rates.values())
        included = item.get("included_in_daily_operating_cost", "UNKNOWN")
        if included not in ("YES", "NO", "UNKNOWN"):
            raise ValueError(
                f"additional_costs.{cost_name}.included_in_daily_operating_cost: "
                "допустимы YES, NO или UNKNOWN"
            )
        if has_numeric_rate and included != "NO":
            raise ValueError(
                f"Для {cost_name} задана сумма, но included_in_daily_operating_cost "
                f"равен {included}. Установите NO только после проверки состава "
                "суточной ставки, иначе возможен двойной учёт."
            )

    seasonal = config.get("seasonal_costs_rub_by_unit", {})
    if not isinstance(seasonal, dict):
        raise ValueError("seasonal_costs_rub_by_unit должен быть JSON-объектом")
    seasonal_inclusions = config.get("seasonal_cost_inclusions", {})
    if not isinstance(seasonal_inclusions, dict):
        raise ValueError("seasonal_cost_inclusions должен быть JSON-объектом")
    for unit_id, costs in seasonal.items():
        if not isinstance(costs, dict):
            raise ValueError(f"seasonal_costs_rub_by_unit.{unit_id} должен быть объектом")
        for cost_name, amount in costs.items():
            if cost_name not in SEASONAL_COST_SPECS:
                raise ValueError(f"Неизвестная сезонная статья: {cost_name}")
            if amount is not None:
                _non_negative(amount, f"seasonal_costs_rub_by_unit.{unit_id}.{cost_name}")
                included = seasonal_inclusions.get(cost_name, "UNKNOWN")
                if included not in ("YES", "NO", "UNKNOWN"):
                    raise ValueError(
                        f"seasonal_cost_inclusions.{cost_name}: допустимы YES, NO или UNKNOWN"
                    )
                if included != "NO":
                    raise ValueError(
                        f"Для сезонной статьи {cost_name} задана сумма, но её включение "
                        f"в daily_operating_cost_rub равно {included}. Установите NO "
                        "только после проверки состава суточной ставки, иначе возможен двойной учёт."
                    )

    return config


def _hours(start: str, end: str, field: str) -> float:
    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Некорректные даты в {field}") from exc
    if seconds < 0:
        raise ValueError(f"Отрицательная длительность в {field}")
    return seconds / 3600.0


def _get_events(trip: dict[str, Any]) -> list[dict[str, Any]]:
    events = trip.get("events")
    if events is None and trip.get("events_json"):
        events = json.loads(trip["events_json"])
    if not isinstance(events, list) or not events:
        raise ValueError(f"В рейсе {trip.get('trip_id', '<без ID>')} нет событий движения")
    return events


def _rate_for_unit(config: dict[str, Any], cost_name: str, unit_id: str) -> float | None:
    spec = ADDITIONAL_COST_SPECS[cost_name]
    item = config.get("additional_costs", {}).get(cost_name, {})
    value = item.get(spec["rate_key"], {}).get(unit_id)
    return None if value is None else float(value)


def calculate_trip_cost(
    trip: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Возвращает стоимость одного рейсового цикла с детализацией."""
    unit_id = trip["unit_id"]
    if unit_id not in config["fuel_consumption_lph_by_unit"]:
        raise ValueError(f"Для состава {unit_id} не заданы топливные параметры")

    volume_t = _non_negative(trip["volume_t"], "volume_t")
    fuel_rates = config["fuel_consumption_lph_by_unit"][unit_id]

    fuel_l = 0.0
    propulsion_hours = {"upstream": 0.0, "downstream": 0.0}
    for event in _get_events(trip):
        direction = event.get("direction")
        if direction not in propulsion_hours:
            raise ValueError(f"Неизвестное направление движения: {direction!r}")
        duration = _hours(event["start"], event["end"], "events")
        propulsion_hours[direction] += duration
        fuel_l += duration * float(fuel_rates[direction])

    total_propulsion_hours = sum(propulsion_hours.values())
    fuel_cost = fuel_l * float(config["fuel_price_per_l"])

    occupied_hours = _hours(
        trip["loading_start"], trip["released_at"], "loading_start/released_at"
    )
    occupied_days = occupied_hours / 24.0
    tug_cost = occupied_days * float(config["daily_operating_cost_rub"]["tug"])
    barge_cost = occupied_days * float(config["daily_operating_cost_rub"]["barge"])

    loading_cost = volume_t * float(config["handling_cost_per_ton_rub"]["loading"])
    unloading_cost = volume_t * float(config["handling_cost_per_ton_rub"]["unloading"])
    other_cost = float(config.get("other_cost_per_trip_rub", 0.0))

    additional = {
        "crew": 0.0,
        "maintenance": 0.0,
        "port_navigation": 0.0,
    }
    for cost_name in ADDITIONAL_COST_SPECS:
        rate = _rate_for_unit(config, cost_name, unit_id)
        if rate is None:
            continue
        basis = ADDITIONAL_COST_SPECS[cost_name]["basis"]
        if basis == "occupied_day":
            additional[cost_name] = occupied_days * rate
        elif basis == "propulsion_hour":
            additional[cost_name] = total_propulsion_hours * rate
        elif basis == "per_trip":
            additional[cost_name] = rate

    components = {
        "fuel": fuel_cost,
        "tug_operation": tug_cost,
        "barge_operation": barge_cost,
        "loading": loading_cost,
        "unloading": unloading_cost,
        "other": other_cost,
        **{f"additional_{key}": value for key, value in additional.items()},
    }
    total = sum(components.values())

    return {
        "trip_id": trip.get("trip_id"),
        "unit_id": unit_id,
        "cargo_type": trip.get("cargo_type"),
        "volume_t": round(volume_t, 3),
        "propulsion_hours": {key: round(value, 4) for key, value in propulsion_hours.items()},
        "fuel_l": round(fuel_l, 3),
        "occupied_hours": round(occupied_hours, 3),
        "cost_components": {key: round(value, 2) for key, value in components.items()},
        "total_cost": round(total, 2),
        "cost_per_ton": round(total / volume_t, 2) if volume_t else 0.0,
    }


def evaluate_schedule(
    result: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Оценивает расписание; неподтверждённые статьи явно показывает как исключённые."""
    if not isinstance(result, dict) or "trips" not in result or "metrics" not in result:
        raise ValueError("Ожидается результат result_adapter с metrics и trips")
    trips = result["trips"]
    if not isinstance(trips, list):
        raise ValueError("Поле trips должно быть списком")

    trip_costs = [calculate_trip_cost(trip, config) for trip in trips]
    all_keys = (
        "fuel", "tug_operation", "barge_operation", "loading", "unloading", "other",
        "additional_crew", "additional_maintenance", "additional_port_navigation",
    )
    component_totals = {
        key: sum(row["cost_components"].get(key, 0.0) for row in trip_costs)
        for key in all_keys
    }

    # Сезонные расходы добавляются один раз на судно за весь сценарий,
    # а не повторяются в каждом рейсе. Нужна фактическая сумма по сезону.
    seasonal_totals = {name: 0.0 for name in SEASONAL_COST_SPECS}
    active_units = sorted({str(trip["unit_id"]) for trip in trips})
    seasonal_config = config.get("seasonal_costs_rub_by_unit", {})
    for unit_id in active_units:
        for cost_name in SEASONAL_COST_SPECS:
            amount = seasonal_config.get(unit_id, {}).get(cost_name)
            if amount is not None:
                seasonal_totals[cost_name] += float(amount)

    for cost_name, amount in seasonal_totals.items():
        component_totals[f"seasonal_{cost_name}"] = amount

    total_cost = sum(component_totals.values())
    delivered_t = float(result["metrics"].get("delivered_total_t", 0.0))
    delivered_on_time_t = float(result["metrics"].get("delivered_on_time_t", 0.0))
    if delivered_t < 0 or delivered_on_time_t < 0:
        raise ValueError("Показатели доставленного тоннажа не могут быть отрицательными")
    if delivered_on_time_t > delivered_t + 1e-6:
        raise ValueError("Доставлено в срок больше, чем доставлено всего")

    shares = {
        key: round(value / total_cost * 100.0, 2) if total_cost else 0.0
        for key, value in component_totals.items()
    }

    excluded_cost_items: list[dict[str, str]] = []
    additional_config = config.get("additional_costs", {})
    for cost_name, spec in ADDITIONAL_COST_SPECS.items():
        item = additional_config.get(cost_name, {})
        rates = item.get(spec["rate_key"], {})
        missing_units = [unit_id for unit_id in active_units if rates.get(unit_id) is None]
        if missing_units:
            excluded_cost_items.append({
                "item": cost_name,
                "basis": spec["basis"],
                "missing_for_units": ", ".join(missing_units),
                "reason": "Нет подтверждённой ставки; в total_cost не включено.",
            })

    for unit_id in active_units:
        for cost_name, description in SEASONAL_COST_SPECS.items():
            amount = seasonal_config.get(unit_id, {}).get(cost_name)
            if amount is None:
                excluded_cost_items.append({
                    "item": cost_name,
                    "basis": "season_per_unit",
                    "missing_for_units": unit_id,
                    "reason": f"{description}: нет суммы за сезон; в total_cost не включено.",
                })

    metrics = result["metrics"]
    return {
        "economics_schema_version": "1.2.0",
        "cost_model_version": MODEL_VERSION,
        "scenario_id": result.get("scenario_id"),
        "strategy": result.get("strategy"),
        "source_schema_version": result.get("schema_version"),
        "currency": config["currency"],
        "data_status": config.get("data_status", "ASSUMED"),
        "cost_completeness": "PARTIAL" if excluded_cost_items else "CONFIGURED_ITEMS_PRICED",
        "total_cost_scope": "Только включённые статьи; исключённые расходы перечислены отдельно.",
        "assumptions": config.get("notes", []),
        "excluded_cost_items": excluded_cost_items,
        "active_units": active_units,
        "operational_metrics": {
            "delivered_total_t": delivered_t,
            "delivered_on_time_t": delivered_on_time_t,
            "completed_trips": int(metrics.get("completed_trips", len(trips))),
        },
        "cost_components": {key: round(value, 2) for key, value in component_totals.items()},
        "cost_shares_percent": shares,
        "total_cost": round(total_cost, 2),
        "cost_per_delivered_ton": round(total_cost / delivered_t, 2) if delivered_t else None,
        "cost_per_on_time_delivered_ton": (
            round(total_cost / delivered_on_time_t, 2) if delivered_on_time_t else None
        ),
        "trip_costs": trip_costs,
    }
