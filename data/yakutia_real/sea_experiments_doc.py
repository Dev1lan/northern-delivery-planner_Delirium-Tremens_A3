"""SEA_EXPERIMENTS.md из scenario/sea_experiments_report.json и sea_experiments_social.json.

    python3 data/yakutia_real/sea_experiments_doc.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SC = HERE / "scenario"


def f(x, nd=0):
    if x is None:
        return "—"
    if isinstance(x, (int, float)):
        return f"{x:,.{nd}f}".replace(",", " ")
    return str(x)


def mln(x):
    return "неизвестно" if x is None else f(x / 1e6) + " млн"


ROWS = [
    ("Довезено всего = в срок, т", lambda r: f(r["delivered_on_time_t"])),
    ("Морем, т", lambda r: f(r["sea_delivered_t"])),
    ("По реке, т", lambda r: f(r["delivered_total_t"] - r["sea_delivered_t"])),
    ("Недовоз, т", lambda r: f(r["undelivered_t"])),
    ("— морской / речной", lambda r: f"{f(r['undelivered_sea_t'])} / {f(r['undelivered_river_t'])}"),
    ("Рейсов", lambda r: f(r["voyages"])),
    ("Р-153: рейсов", lambda r: f(r["r153_voyages"])),
    ("Р-153: занято из всего", lambda r: f"{r['r153_tugs_used']} из {r['r153_tugs_total']}"),
    ("Р-153: река / море, т", lambda r: f"{f(r['r153_river_t'])} / {f(r['r153_sea_t'])}"),
    ("Добавленных судов в работе", lambda r: f"{r['added_vessels_used']} из {r['added_vessels']}"),
    ("Эксплуатация, ₽", lambda r: mln(r["finance"]["operating_rub"])),
    ("Альтернативная доставка недовоза, ₽", lambda r: mln(r["finance"]["alternative_delivery_rub"])),
    ("Дополнительный флот, ₽", lambda r: mln(r["finance"]["extra_fleet"]["cost_rub"])),
    ("Итого, ₽", lambda r: mln(r["finance"]["total_rub"]) if r["finance"]["total_rub"] is not None
     else f"≥ {mln(r['finance']['total_known_rub'])} + флот"),
    ("Соцминимумы", lambda r: r["social"] if r["social"] == "NOT_CONFIGURED" else
     f"{sum(q['satisfied'] for q in r['requirements'])} из {len(r['requirements'])}, дефицит {f(r['social_deficit_t'])} т"),
    ("Время, с", lambda r: f(r["seconds"], 1)),
    ("Завершение", lambda r: r["termination"]),
    ("Аудит", lambda r: f"{r['audit']} ({r['audit_violations']} нарушений)"),
]


def table(rows):
    out = ["| | " + " | ".join(r["name"] for r in rows) + " |", "|---|" + "---|" * len(rows)]
    out += [f"| {name} | " + " | ".join(fn(r) for r in rows) + " |" for name, fn in ROWS]
    return out


def main():
    base = json.loads((SC / "sea_experiments_report.json").read_text(encoding="utf-8"))
    social = json.loads((SC / "sea_experiments_social.json").read_text(encoding="utf-8"))
    st, rows = base["settings"], base["experiments"]
    L = ["# Эксперименты с морским флотом: E0 / E1 / E1P / E2 / E3", "",
         "Контрольные прогоны для доработки распределения флота река—море в сетевом ядре. Это варианты «что если», "
         "а не фактическое состояние акватории: углубление рек (E1, E2, E3) и дополнительные суда (E1, E2) — допущения, "
         "помеченные в сценарии полем `what_if` (см. [PROVENANCE.md](PROVENANCE.md)).", "",
         "## Условия (одинаковые у всех)", "",
         f"- Ядро {st['core_version']}, коммит репозитория при расчёте `{(st['repo_commit'] or '')[:7]}`.",
         "- Заявки: 65 заявок (одинаковые «откуда — куда — груз» объединены), 729 895 т из "
         "`scenario/yakutia_real_2026_demo.json`, одинаковые во всех вариантах (у R_* — только речные).",
         f"- Лимиты `{st['limits']}`, тарифы `{st['tariffs']}` (условные демо-ставки).",
         f"- Поиск: `SearchPolicy(mode={st['policy']['mode']}, time_seconds={st['budget_seconds']}, "
         f"recover_undelivered=True)`, стратегия `{st['strategy']}`.",
         f"- E1P/E2P — два прохода вне ядра ({st['two_pass_split']}): сначала только морские заявки на всём флоте, "
         "затем остаток на освободившемся флоте; суда, занятые морем, во втором проходе доступны с порта и момента "
         "окончания морской работы. Планы склеиваются `build_result` и проверяются `audit_result`.",
         "- «Морская» заявка — кратчайший путь проходит по морскому ребру (`build_deep_variant.sea_cargo`).",
         "- Поиск ограничен по времени: варианты с `TIME_LIMIT` при повторе могут давать разницу в несколько тысяч тонн "
         "(E3 в двух прогонах — 542 682 и 536 953 т).", "",
         "Воспроизведение: `python3 data/yakutia_real/sea_experiments.py` (все) и "
         "`python3 data/yakutia_real/sea_experiments.py E0S E1S E1PS E2S` (с соцминимумами). Полные результаты v3 "
         "сохраняются в `scenario/sea_results/<код>.json.gz` и проходят повторный аудит.", "",
         "## Варианты", "", "| Код | Что меняется | Отпечаток сценария |", "|---|---|---|"]
    L += [f"| {r['name']} | {r['title']} | `{r['scenario_fingerprint'][:16]}…` |" for r in rows + social["experiments"]]
    L += ["", "## Результаты", ""] + table(rows)
    L += ["", "## С социальными минимумами № 326-р", "",
          "Минимумы — весь объём заявок угля, сырой нефти, газового конденсата и продовольствия за навигацию "
          "(приложения 3, 4, 5, 7 распоряжения «для жизнеобеспечения»), см. `build_social_variant.py`.", ""]
    L += table(social["experiments"])
    L += ["", "Стоимость дополнительного флота неизвестна: подтверждённых ставок аренды или содержания судов река—море нет, "
          "в тарифах v2 только часы работы, топливо и сборы. Поэтому для вариантов с добавленными судами итог не выводится, "
          "только известная часть.", "",
          "## Выводы", "",
          "- Без углубления морем не идёт ничего (E0, E3): морские суда не доходят до портов погрузки внутри рек.",
          "- Распределение: E1 → E1P на том же флоте +45 тыс. т (морем 15,5 → 70,7 тыс. т), речной недовоз +10 тыс. т.",
          "- С соцминимумами ядро само частично перераспределяет флот: E1S 596 913 т против E1 563 087 т; E1PS — 616 135 т.",
          "- Флот: E1P → E2 ещё +61 тыс. т; при 22 Р-153 приоритет уже не влияет (E2 = E2P).",
          "- Речную работу Р-153 другие составы заменяют без потери тоннажа (R_ALL 547 606 т против R_NOSEA 547 457 т), "
          "но дороже по эксплуатации на 185 млн ₽.",
          "- Остаётся 43 тыс. т речного недовоза (Вилюй 0,8–0,9 м, верхний Алдан 1,3 м) и 17 тыс. т морского на самых "
          "длинных путях."]
    (HERE / "SEA_EXPERIMENTS.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(HERE / "SEA_EXPERIMENTS.md")


if __name__ == "__main__":
    main()
