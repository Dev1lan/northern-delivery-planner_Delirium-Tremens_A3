"""Map the existing Yana pilot fixture to a directed network for evaluation."""

from datetime import timedelta
import json
from pathlib import Path

from generate import dt
from optimizer import load

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "data/demo/scenario"


def network_fixture():
    config = json.loads((ROOT / "scenario_config.json").read_text(encoding="utf-8-sig"))
    source = load(FIXTURE, config)
    start, end = dt(config["start"]), dt(config["end"]) + timedelta(days=1)
    ports = dict.fromkeys(port for seg in config["segments"] for port in (seg["from_node"], seg["to_node"]))
    edges = []
    for segment in config["segments"]:
        conditions = {row["date"][:10]: dict(usable_depth_m=float(row["usable_depth_m"]),
                                             max_allowed_draft_m=float(row["max_allowed_draft_m"]),
                                             is_closed=bool(int(row["is_closed"])))
                      for row in source["conditions"] if row["segment_id"] == segment["segment_id"]}
        for reverse in (False, True):
            a, b = segment["from_node"], segment["to_node"]
            edges.append(dict(edge_id=segment["segment_id"] + ("-return" if reverse else ""),
                              origin_port=b if reverse else a, destination_port=a if reverse else b,
                              direction="downstream" if reverse else "upstream",
                              distance_km=segment["distance_km"], condition=conditions[start.date().isoformat()],
                              daily_conditions=conditions))
    tugs, barges, pairs = {}, {}, []
    for unit in source["units"]:
        tug_id, barge_id = unit["tug_id"], unit["barge_id"]
        availability = dict(current_port=config["origin"], available_at=dt(unit["available_from"]).isoformat(),
                            available_to=dt(unit["available_to"]).isoformat())
        tugs.setdefault(tug_id, dict(availability, vessel_id=tug_id, draft_empty_m=float(unit["tug_draft_m"]),
                                     speed_upstream_kmh=float(unit["speed_upstream_kmh"]),
                                     speed_downstream_kmh=float(unit["speed_downstream_kmh"])))
        barges.setdefault(barge_id, dict(availability, vessel_id=barge_id, capacity_t=float(unit["capacity_t"]),
                                         draft_empty_m=float(unit["draft_empty_m"]),
                                         draft_full_m=float(unit["draft_full_m"]),
                                         handling_tph=float(unit["handling_tph"]),
                                         cargo_types_allowed=list(unit["cargo_type_allowed"])))
        pairs.append(dict(tug_id=tug_id, barge_id=barge_id))
    cargo = [dict(cargo_id=row["cargo_id"], cargo_type=row["cargo_type"], origin_port=row["origin"],
                  destination_port=row["destination"], volume_t=float(row["volume_t"]),
                  ready_at=dt(row["ready_date"]).isoformat(), due_at=dt(row["due_date"]).isoformat(),
                  priority=int(row["priority"])) for row in source["cargo"]]
    return dict(scenario_id="yana-pilot-network", data_status="SYNTHETIC", start=start.isoformat(),
                end=end.isoformat(), ports=list(ports), edges=edges, tugs=list(tugs.values()),
                barges=list(barges.values()), compatible_pairs=pairs, cargo=cargo,
                underkeel_m=config["underkeel_m"], fixed_handling_seconds=int(config["handling_fixed_hours"]*3600))
