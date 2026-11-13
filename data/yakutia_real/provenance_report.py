"""Происхождение данных по вариантам сценария: что реальное, что посчитано, что допущение, что «что если».

    python3 data/yakutia_real/provenance_report.py     # → PROVENANCE.md
"""
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import sea_experiments as X  # noqa: E402

FIELDS = """## Откуда каждый параметр

| Параметр | Откуда | Статус |
|---|---|---|
| Длины речных рёбер | Норматив Росморречфлота № АТ-442-р (Y-01), внутри участков — русло OSM с калибровкой | REAL / DERIVED |
| Гарантированные глубины и допустимая осадка по дням | Тот же норматив; осадка = глубина − запас под килем 0,2 м; смена глубины на Яне и баре Индигирки 1 сентября (примечания 16, 17) | REAL / DERIVED |
| Окна навигации речных рёбер | Даты открытия и закрытия навигации 2026 по участкам норматива | REAL |
| Морские рёбра Тикси — Нижнеянск — Чокурдах — Зелёный Мыс | Расстояние по координатам ×1,2, глубина 4,0 м, окно 10.07 / 20.07 / 01.08 — 30.09 | ASSUMED |
| Проекты судов: класс, дедвейт, размеры, мощность | Каталог ЛОРП (Y-04) | REAL |
| Осадка в грузу, скорости, производительность обработки | Высота борта минус надводный борт; скорости и обработка — оценка | ASSUMED |
| Число судов по базам | Расстановка по местам зимовки (Y-05), пропорционально 130 единицам транспортного флота | ASSUMED (пропорция по REAL) |
| Морской допуск Р-153 и 16800 | Класс Регистра М-ПР по каталогу; допуск на арктическое побережье не подтверждён | SCENARIO_ASSUMPTION |
| Объёмы заявок, пункты отгрузки и доставки | Распоряжение № 326-р, приложения 3–9 (Y-07); видимые строки, привязка к ближайшему порту сети | REAL / DERIVED |
| Сроки готовности и доставки | В распоряжении нет; окно навигации на кратчайшем пути | ASSUMED |
| Социальные минимумы (версия `_social`) | Распоряжение № 326-р, п. 1: приложения 3, 4, 5, 7 — «для жизнеобеспечения»; минимум = объём заявок сценария за навигацию | DERIVED |

Варианты «что если» помечены полем `what_if`: `deepened` — ребро углублено под осадку Р-153, `sea_permit` — танкерной барже 16800Н1 дан морской допуск, `added_vessel` — судна нет в данных перевозчика. Это не фактическое состояние акватории.
"""


def table(name):
    s = X.variant(name)
    rows = []
    for key in ("edges", "tugs", "barges", "cargo"):
        prov = Counter(r.get("provenance", "—") for r in s[key])
        wif = Counter(r["what_if"] for r in s[key] if r.get("what_if"))
        rows.append((key, len(s[key]), prov.get("REAL", 0), prov.get("DERIVED", 0), prov.get("ASSUMED", 0),
                     ", ".join(f"{k}: {v}" for k, v in sorted(wif.items())) or "—"))
    social = s.get("social_requirements") or []
    return rows, social


def main():
    out = ["# Происхождение данных сценария Якутии 2026", "",
           "Сгенерировано `provenance_report.py`. Статусы записей — поле `provenance` "
           "(REAL — из источника, DERIVED — посчитано из реальных данных, ASSUMED — оценка).", "", FIELDS]
    for name in ("E0", "E1", "E2", "E3", "E0S"):
        rows, social = table(name)
        out += [f"## {name}: {X.title(name)}", "", "| Таблица | Записей | REAL | DERIVED | ASSUMED | «Что если» |",
                "|---|---|---|---|---|---|"]
        out += [f"| {k} | {n} | {r} | {d} | {a} | {w} |" for k, n, r, d, a, w in rows]
        out.append(f"\nСоциальные минимумы: {', '.join(q['requirement_id'] + ' ' + format(q['required_t'], ',.0f').replace(',', ' ') + ' т' for q in social) or 'не заданы'}.\n")
    (HERE / "PROVENANCE.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(HERE / "PROVENANCE.md")


if __name__ == "__main__":
    main()
