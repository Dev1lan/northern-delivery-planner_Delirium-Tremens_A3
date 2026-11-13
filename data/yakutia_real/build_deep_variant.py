"""Варианты сценария под суда река—море: допущения для проверки перегона морского флота.

Берёт scenario/yakutia_real_2026_demo.json и меняет только глубины и флот река—море.
Заявки, окна навигации и остальной флот не трогаются. Изменённые записи помечены provenance = ASSUMED.

- deepen(s, "paths"): на речных рёбрах кратчайших путей межбассейновых заявок (путь идёт через море)
  допустимая осадка поднимается до осадки самого глубокого морского судна (Р-153, 2,48 м) + запас под килем;
- deepen(s, "mouths"): то же, но только на устьевых участках, примыкающих к морю;
- add_sea_fleet(s, ...): дополнительные буксиры Р-153 и баржи 16800 / 16800Н1, морской допуск танкерным 16800Н1.

    python data/yakutia_real/build_deep_variant.py      # scenario/yakutia_real_2026_deep.json (сайт)
"""
import copy
import heapq
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent / "scenario"
SRC = HERE / "yakutia_real_2026_demo.json"
OUT = HERE / "yakutia_real_2026_deep.json"
EXTRA_TUGS = 4
ADDED = ("ВАРИАНТ «что если»: дополнительное судно проекта ЛОРП сверх расстановки по местам зимовки; "
         "в данных перевозчика его нет")
# Устьевые участки: последнее речное ребро перед морским портом на каждой реке.
MOUTHS = {("Кюсюр", "Быков Мыс"), ("Усть-Куйга", "Нижнеянск"), ("Белая Гора", "Чокурдах"),
          ("Колымское", "Зелёный Мыс")}


def load():
    return json.loads(SRC.read_text(encoding="utf-8"))


def shortest(edges, a, b):
    """Кратчайший путь по длине; возвращает список рёбер."""
    adj = {}
    for e in edges:
        adj.setdefault(e["origin_port"], []).append(e)
    best, heap, n = {a: 0.0}, [(0.0, 0, a, [])], 0
    while heap:
        d, _, p, path = heapq.heappop(heap)
        if p == b:
            return path
        if d > best.get(p, float("inf")):
            continue
        for e in adj.get(p, []):
            nd = d + e["distance_km"]
            if nd < best.get(e["destination_port"], float("inf")):
                best[e["destination_port"]] = nd
                n += 1
                heapq.heappush(heap, (nd, n, e["destination_port"], path + [e]))
    return None


def sea_edges(s):
    return {e["edge_id"] for e in s["edges"] if e.get("water_type") == "SEA"}


def sea_cargo(s):
    """Заявки, чей кратчайший путь проходит по морю."""
    sea = sea_edges(s)
    return {c["cargo_id"] for c in s["cargo"]
            if any(e["edge_id"] in sea for e in shortest(s["edges"], c["origin_port"], c["destination_port"]) or [])}


def sea_draft(s):
    vessels = [v for v in s["tugs"] + s["barges"] if v.get("navigation_permits")]
    return max(max(v.get("draft_empty_m", 0), v.get("draft_full_m", 0)) for v in vessels)


def deepen(s, mode="paths"):
    sea = sea_edges(s)
    by_id = {e["edge_id"]: e for e in s["edges"]}
    need_draft = sea_draft(s)
    need_depth = round(need_draft + s["underkeel_m"], 2)
    if mode == "mouths":
        target = {eid for eid, e in by_id.items()
                  if (e["origin_port"], e["destination_port"]) in MOUTHS
                  or (e["destination_port"], e["origin_port"]) in MOUTHS}
    else:
        target = set()
        wanted = sea_cargo(s)
        for c in s["cargo"]:
            if c["cargo_id"] not in wanted:
                continue
            for e in shortest(s["edges"], c["origin_port"], c["destination_port"]):
                if e["edge_id"] not in sea:
                    target.add(e["edge_id"])
                    back = f'{e["destination_port"]}→{e["origin_port"]}'
                    if back in by_id:
                        target.add(back)
    changed = []
    for eid in sorted(target - sea):
        e = by_id[eid]
        old = min((d["max_allowed_draft_m"] for d in e["daily_conditions"].values() if not d["is_closed"]),
                  default=None)
        if old is None or old >= need_draft:
            continue
        for d in e["daily_conditions"].values():
            if not d["is_closed"]:
                d["max_allowed_draft_m"] = max(d["max_allowed_draft_m"], need_draft)
                d["usable_depth_m"] = max(d["usable_depth_m"], need_depth)
        e["provenance"] = "ASSUMED"
        e["what_if"] = "deepened"
        e["source"] = (f'{e.get("source", "")}; ВАРИАНТ: глубина поднята до {need_depth} м '
                       f'(осадка {need_draft} м + запас {s["underkeel_m"]} м), чтобы прошли суда река—море')
        changed.append((eid, old))
    return changed, need_draft, need_depth


def add_sea_fleet(s, tugs=EXTRA_TUGS, dry=0, tank=0, bases=("Жатай",), prefix="Д"):
    """Добавляет буксиры Р-153 и баржи 16800 / 16800Н1 по базам по кругу; танкерным 16800Н1 — морской допуск."""
    sea_tug = next(t for t in s["tugs"] if t.get("navigation_permits"))
    permit = sea_tug["navigation_permits"]
    for b in s["barges"]:
        if "16800Н1" in b["vessel_id"] and not b.get("navigation_permits"):
            b["navigation_permits"] = [dict(p, source="ВАРИАНТ: допуск как у 16800 (допущение)") for p in permit]
            b["vessel_class"] = sea_tug["vessel_class"]
            b["what_if"] = "sea_permit"
    dry_t = next(b for b in s["barges"] if b["vessel_id"].endswith("-16800"))
    tank_t = next(b for b in s["barges"] if "16800Н1" in b["vessel_id"])

    def opening(port):
        dates = [v["available_at"] for v in s["tugs"] + s["barges"] if v["current_port"] == port]
        return min(dates) if dates else sea_tug["available_at"]

    known = {v["vessel_id"] for v in s["tugs"] + s["barges"]}
    start = 1 + sum(1 for k in known if k.startswith(prefix) and k.endswith("-Р-153"))
    for i in range(tugs):
        port = bases[i % len(bases)]
        s["tugs"].append(dict(copy.deepcopy(sea_tug), vessel_id=f"{prefix}{start + i:02d}-Р-153",
                              current_port=port, available_at=opening(port), provenance="ASSUMED",
                              what_if="added_vessel", source=ADDED))
    for kind, n, tpl in (("16800", dry, dry_t), ("16800Н1", tank, tank_t)):
        for i in range(n):
            port = bases[i % len(bases)]
            s["barges"].append(dict(copy.deepcopy(tpl), vessel_id=f"{prefix}Б{i + 1:02d}-{kind}",
                                    current_port=port, available_at=opening(port), provenance="ASSUMED",
                                    what_if="added_vessel", source=ADDED))
    sea_tugs = [t["vessel_id"] for t in s["tugs"] if t.get("navigation_permits")]
    sea_barges = [b["vessel_id"] for b in s["barges"] if b.get("navigation_permits")]
    have = {(p["tug_id"], p["barge_id"]) for p in s["compatible_pairs"]}
    s["compatible_pairs"] += [dict(tug_id=t, barge_id=b) for t in sea_tugs for b in sea_barges if (t, b) not in have]
    # Морское ребро пускает только перечисленные в allowed_pairs составы — вносим туда все пары река—море.
    sea_pairs = [dict(tug_id=t, barge_id=b) for t in sea_tugs for b in sea_barges]
    for e in s["edges"]:
        if e.get("water_type") == "SEA":
            e["allowed_pairs"] = sea_pairs
    return s


def main():
    s = load()
    changed, need_draft, need_depth = deepen(s, "paths")
    add_sea_fleet(s)
    s["scenario_id"] = s.get("scenario_id", "yakutia_real") + "_deep"
    OUT.write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"осадка морского флота {need_draft} м, глубина {need_depth} м; изменено рёбер: {len(changed)}")
    for eid, old in changed:
        print(f"  {eid}: {old} -> {need_draft}")
    print(OUT)


if __name__ == "__main__":
    main()
