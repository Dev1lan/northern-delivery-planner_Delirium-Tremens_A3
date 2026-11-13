"""Версия сценария с социальными минимумами по распоряжению № 326-р.

Основание (текст распоряжения, п. 1): приложения 3, 4, 5 и 7 утверждают объёмы завоза «для жизнеобеспечения»
Республики Саха (Якутия): уголь, сырая нефть, газовый конденсат, социально значимые продовольственные товары.
Сроков доставки распоряжение не устанавливает (только «в навигацию 2026 года»), поэтому минимум задан как
доставка всего объёма за навигацию (basis = delivered_total, без даты). Нефтепродукты (прил. 6) и МТР (прил. 8)
в распоряжении не отнесены к жизнеобеспечению — минимумов по ним нет. Депонационный запас (прил. 1, 2, 9)
в сценарии не отделён от нефтепродуктов по типу груза, поэтому отдельным минимумом не задан.

Минимум = сумма заявок сценария этого типа: это видимые на зеркале ГАРАНТ строки приложений, привязанные к портам
сети (см. README, «Насколько полно»), а не полный объём приложения.

    python data/yakutia_real/build_social_variant.py [исходный.json] [итоговый.json]
"""
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent / "scenario"
SOURCE = "Распоряжение Правительства РС(Я) № 326-р от 15.05.2026, п. 1 и приложение № {n}: объёмы завоза {what} «для жизнеобеспечения»"
RULES = (("LIFE_COAL", "coal", 3, "угля"), ("LIFE_CRUDE_OIL", "crude_oil", 4, "сырой нефти"),
         ("LIFE_GAS_CONDENSATE", "gas_condensate", 5, "газового конденсата"),
         ("LIFE_FOOD", "food", 7, "социально значимых продовольственных товаров"))


def with_social(s):
    volume = Counter()
    for c in s["cargo"]:
        volume[c["cargo_type"]] += c["volume_t"]
    s["social_requirements"] = [
        {"requirement_id": rid, "cargo_types": [kind], "required_t": round(volume[kind], 3), "deadline": None,
         "basis": "delivered_total", "source_status": "DERIVED",
         "source": SOURCE.format(n=n, what=what) + "; объём — сумма заявок сценария; срок — конец навигации"}
        for rid, kind, n, what in RULES if volume[kind] > 0]
    return s


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "yakutia_real_2026_demo.json"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "yakutia_real_2026_social.json"
    s = with_social(json.loads(src.read_text(encoding="utf-8")))
    s["scenario_id"] = s["scenario_id"] + "_social"
    out.write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for r in s["social_requirements"]:
        print(r["requirement_id"], r["required_t"])
    print(out)


if __name__ == "__main__":
    main()
