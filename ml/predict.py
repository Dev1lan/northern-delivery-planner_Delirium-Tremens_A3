"""
ML-рекомендация стратегии + расчёт по матмодели Case A3 с ответом в формате
контракта результата 1.0.0 (result.schema.json).

Python:
    from predict import run_recommended, recommend_for_scenario, recommend_strategy
    result, rec = run_recommended('data/demo/scenario', scenario_id='yana_001')
    # result — объект контракта 1.0.0 (schema_version, scenario_id, strategy, metrics, trips)
    # rec    — рекомендация ML (стратегия, confidence, прогноз, признаки)

CLI (из каталога ml/):
    python predict.py --scenario ../data/demo/scenario --output result.json --recommendation-output rec.json
    python predict.py --scenario ../data/demo/scenario --recommend-only

Схема контракта запрещает дополнительные поля (additionalProperties: false),
поэтому сведения ML (confidence, прогноз) НЕ вкладываются в объект
результата, а возвращаются отдельным объектом / файлом.

Порядок работы:
  1) признаки считаются из входов сценария (до расчёта);
  2) нейросеть выбирает стратегию; вне обучающего распределения, при
     нехватке данных или отключённом ML (A3_ML_DISABLED=1) берётся
     OPTIMIZED — штатный оптимизатор матмодели;
  3) расписание строит и проверяет матмодель (result_adapter.run_scenario →
     optimizer.simulate → validate.audit). Нейросеть расписаний не строит и
     ограничений нарушить не может: при ошибке аудита расчёт прерывается.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

import a3_bridge as B
from model_common import Preprocessor, KnnOOD, ood_flags

HERE = Path(__file__).resolve().parent
FALLBACK_STRATEGY = "OPTIMIZED"
DISCLAIMER = ("Оценка модели, обученной на синтетических сценариях матмодели; не гарантия и не "
              "математически оптимальное решение. Расписание строит и проверяет матмодель.")


def _fallback(reason: str, **extra) -> Dict:
    out = dict(recommended_strategy=None, confidence=None, needs_verification=True,
               action="FALLBACK_TO_OPTIMIZER", strategy_to_run=FALLBACK_STRATEGY,
               reason=reason, disclaimer=DISCLAIMER)
    out.update(extra)
    return out


@lru_cache(maxsize=2)
def _load(model_dir: str):
    d = Path(model_dir)
    meta = json.loads((d / "model_meta.json").read_text(encoding="utf-8"))
    pre = Preprocessor.from_dict(meta["preprocessor"])
    knn = KnnOOD(np.load(d / "ood_train_matrix.npy"))
    if meta["backend"] == "torch":
        import torch
        from model_common import build_torch_mlp
        ck = torch.load(d / "model.pt", map_location="cpu")
        net = build_torch_mlp(ck["n_in"], ck["hidden"], len(meta["strategies"]))
        net.load_state_dict(ck["state_dict"])
        net.eval()

        def proba(X):
            with torch.no_grad():
                return torch.softmax(net(torch.tensor(X)), dim=1).numpy()
    else:
        import joblib
        proba = joblib.load(d / "model_sklearn_mlp.joblib").predict_proba
    share = None
    if (d / "share_mlp.joblib").exists():
        import joblib
        share = joblib.load(d / "share_mlp.joblib")
    return meta, pre, knn, proba, share


def recommend_strategy(scenario_features: dict, model_dir=HERE) -> dict:
    """Признаки сценария -> рекомендация. Ничего не рассчитывает по матмодели."""
    if os.environ.get("A3_ML_DISABLED") == "1":
        return _fallback("ML отключён (A3_ML_DISABLED=1)")
    try:
        meta, pre, knn, proba, share = _load(str(model_dir))
    except FileNotFoundError as e:
        return _fallback(f"модель не найдена: {Path(str(e.filename)).name}")
    missing = [f for f in pre.features if scenario_features.get(f) is None]
    if missing:
        return _fallback("не хватает признаков", missing_features=missing)
    try:
        X = pre.transform(scenario_features)
    except (TypeError, ValueError):
        return _fallback("признаки не приводятся к числам")
    if not np.all(np.isfinite(X)):
        return _fallback("некорректные значения признаков")
    flags, reasons = ood_flags(pre, knn, [scenario_features], X)
    if flags[0]:
        return _fallback("параметры вне обучающего распределения", out_of_distribution=True,
                         ood_reasons=reasons[0])
    strategies = meta["strategies"]
    p = proba(X)[0]
    k = int(np.argmax(p))
    # правило с учётом цены ошибки: отклоняться от штатного оптимизатора только при высокой уверенности
    thr = meta.get("deviate_confidence_threshold")
    safe = meta.get("safe_strategy", FALLBACK_STRATEGY)
    run = strategies[k]
    if thr is not None and run != safe and float(p[k]) < thr:
        run = safe
    out = dict(recommended_strategy=strategies[k], confidence=round(float(p[k]), 4),
               needs_verification=True, action="RUN_AND_VERIFY", strategy_to_run=run,
               probabilities={s: round(float(x), 4) for s, x in zip(strategies, p)},
               out_of_distribution=False, model_backend=meta["backend"], disclaimer=DISCLAIMER)
    if share is not None:
        sh = np.clip(share.predict(X)[0], 0, 1)
        total = float(scenario_features["cargo_total_t"])
        out["expected_on_time_share"] = {s: round(float(x), 3) for s, x in zip(strategies, sh)}
        out["expected_on_time_t"] = {s: round(float(x) * total, 1) for s, x in zip(strategies, sh)}
        if hasattr(share, "predict_interval") and getattr(share, "q", None) is not None:
            _, lo, hi = share.predict_interval(X)
            out["on_time_share_interval90"] = {s: [round(float(a), 3), round(float(b), 3)]
                                               for s, a, b in zip(strategies, lo[0], hi[0])}
            out["interval_note"] = ("≈90% накрытия на синтетических val-сценариях; "
                                    "эмпирическая калибровка, не гарантия")
    return out


def recommend_for_scenario(scenario_path, config_path=B.DEFAULT_CONFIG, model_dir=HERE) -> dict:
    """Каталог сценария (cargo.csv, units.csv, conditions.csv) -> рекомендация + признаки."""
    s = B.load_scenario_folder(scenario_path, config_path)   # включает проверку входов матмодели
    feats = B.extract_features(s)
    rec = recommend_strategy(feats, model_dir)
    rec["features"] = feats
    return rec


def validate_contract(result: dict, schema_path: Optional[Path] = None) -> None:
    """Проверка по result.schema.json (нужен пакет jsonschema из requirements матмодели)."""
    import jsonschema
    schema = json.loads(Path(schema_path or B.A3_ROOT / "result.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(result)


def run_recommended(scenario_path, *, config_path=B.DEFAULT_CONFIG, scenario_id=None,
                    candidate_limit=None, model_dir=HERE, validate=True) -> Tuple[dict, dict]:
    """Рекомендация ML -> расчёт матмоделью -> (результат по контракту 1.0.0, рекомендация)."""
    from result_adapter import run_scenario
    rec = recommend_for_scenario(scenario_path, config_path, model_dir)
    result = run_scenario(scenario_path, config_path=config_path, strategy=rec["strategy_to_run"],
                          scenario_id=scenario_id, candidate_limit=candidate_limit)
    if validate:
        validate_contract(result)
    m = result["metrics"]
    rec["verification"] = dict(strategy_run=result["strategy"], audit_passed=m["violations"] == 0,
                               delivered_on_time_t=m["delivered_on_time_t"],
                               delivered_total_t=m["delivered_total_t"],
                               completed_trips=m["completed_trips"],
                               contract_schema_version=result["schema_version"],
                               contract_schema_validated=bool(validate))
    return result, rec


def run_best_of_all(scenario_path, *, config_path=B.DEFAULT_CONFIG, scenario_id=None, candidate_limit=None,
                    model_dir=HERE, validate=True, with_ml=True) -> Tuple[dict, dict]:
    """Посчитать все стратегии матмоделью и вернуть лучшую.

    Возвращает (результат лучшей стратегии по контракту 1.0.0, сводка). Выбор — по целевой
    функции матмодели (a3_bridge.pick_best), а не по мнению нейросети, поэтому потерь
    относительно лучшей из трёх нет по построению. ML здесь не обязателен: сводка содержит
    его прогноз только для сравнения «прогноз / расчёт» (with_ml=False или A3_ML_DISABLED=1
    — без него). Цена режима — время расчёта трёх стратегий вместо одной."""
    import time
    from result_adapter import run_scenario
    results, seconds = {}, {}
    for st in B.STRATEGIES:
        t = time.time()
        results[st] = run_scenario(scenario_path, config_path=config_path, strategy=st,
                                   scenario_id=scenario_id, candidate_limit=candidate_limit)
        seconds[st] = round(time.time() - t, 3)
        if validate:
            validate_contract(results[st])
    metrics = {st: r["metrics"] for st, r in results.items()}
    pick = B.pick_best(metrics)
    best = pick["best_strategy"]
    summary = dict(
        mode="BEST_OF_ALL", best_strategy=best, tied_strategies=pick["tied_strategies"],
        strategies={st: dict(delivered_on_time_t=m["delivered_on_time_t"], delivered_total_t=m["delivered_total_t"],
                             undelivered_t=m["undelivered_t"], completed_trips=m["completed_trips"],
                             violations=m["violations"], seconds=seconds[st]) for st, m in metrics.items()},
        gain_vs_optimized_on_time_t=round(metrics[best]["delivered_on_time_t"]
                                          - metrics["OPTIMIZED"]["delivered_on_time_t"], 3),
        contract_schema_version=results[best]["schema_version"], contract_schema_validated=bool(validate),
        note="Лучшая из рассчитанных стратегий матмодели (эвристика); не математически оптимальное решение.")
    if with_ml:
        rec = recommend_for_scenario(scenario_path, config_path, model_dir)
        rec.pop("features", None)
        summary["ml_forecast"] = rec
    return results[best], summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="ML-рекомендация + результат по контракту Case A3 1.0.0")
    ap.add_argument("--scenario", required=True, type=Path, help="каталог с cargo.csv, units.csv, conditions.csv")
    ap.add_argument("--config", type=Path, default=B.DEFAULT_CONFIG)
    ap.add_argument("--scenario-id")
    ap.add_argument("--candidate-limit", type=int)
    ap.add_argument("--output", type=Path, help="файл результата по контракту; без него — stdout")
    ap.add_argument("--recommendation-output", type=Path, help="файл с рекомендацией ML")
    ap.add_argument("--recommend-only", action="store_true", help="только рекомендация, без расчёта")
    ap.add_argument("--best-of-all", action="store_true",
                    help="посчитать все стратегии и вернуть лучшую (сводка — в --recommendation-output или stderr)")
    a = ap.parse_args(argv)
    dump = lambda o: json.dumps(o, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if a.best_of_all:
        result, summary = run_best_of_all(a.scenario, config_path=a.config, scenario_id=a.scenario_id,
                                          candidate_limit=a.candidate_limit)
        if a.recommendation_output:
            a.recommendation_output.write_text(dump(summary), encoding="utf-8")
        else:
            sys.stderr.write(dump({k: v for k, v in summary.items() if k != "ml_forecast"}))
        if a.output:
            a.output.write_text(dump(result), encoding="utf-8")
        else:
            sys.stdout.buffer.write(dump(result).encode("utf-8"))
        return
    if a.recommend_only:
        sys.stdout.buffer.write(dump(recommend_for_scenario(a.scenario, a.config)).encode("utf-8"))
        return
    result, rec = run_recommended(a.scenario, config_path=a.config, scenario_id=a.scenario_id,
                                  candidate_limit=a.candidate_limit)
    if a.recommendation_output:
        a.recommendation_output.write_text(dump(rec), encoding="utf-8")
    else:
        sys.stderr.write(dump({k: v for k, v in rec.items() if k != "features"}))
    if a.output:
        a.output.write_text(dump(result), encoding="utf-8")
    else:
        sys.stdout.buffer.write(dump(result).encode("utf-8"))


if __name__ == "__main__":
    main()
