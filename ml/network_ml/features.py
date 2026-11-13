"""Features computed from validated inputs before planning."""

from collections import Counter
from heapq import heappop, heappush
from statistics import mean

from network.access import admission_error
from network.costs import CostModel


def ratio(a, b):
    return a / b if b else 0.0


def average(values):
    values = list(values)
    return mean(values) if values else 0.0


def distances(outgoing, start):
    best = {start: 0.0}
    queue = [(0.0, start)]
    while queue:
        distance, port = heappop(queue)
        if distance != best[port]:
            continue
        for edge in outgoing[port]:
            candidate = distance + edge.km
            if candidate < best.get(edge.destination, float("inf")):
                best[edge.destination] = candidate
                heappush(queue, (candidate, edge.destination))
    return best


def extract(scenario, tariffs=None):
    """Use directed reachability and demand-weighted routes, independent of port names."""
    cargo = list(scenario.cargo.values())
    edges = list(scenario.edges.values())
    tugs = list(scenario.tugs.values())
    barges = list(scenario.barges.values())
    vessels = tugs + barges
    demand = sum(c.kg for c in cargo)
    days = (scenario.end - scenario.start).total_seconds() / 86400
    pairs = [(scenario.tugs[t], scenario.barges[b]) for t, b in scenario.pairs]
    marine = [e for e in edges if e.marine]
    open_days = {
        e.id: sum(max(0.0, (min(b, scenario.end) - max(a, scenario.start)).total_seconds())
                  for a, b in e.windows) / 86400
        for e in edges
    }
    active = [e for e in edges if open_days[e.id] and
              (not e.condition["is_closed"] or any(not c["is_closed"] for c in e.overrides.values()))]
    water = {port: [] for port in scenario.ports}
    inland = {port: [] for port in scenario.ports}
    for edge in active:
        water[edge.origin].append(edge)
        if not edge.marine:
            inland[edge.origin].append(edge)
    # Weak components of the inland graph are a proxy for basins.
    components = {p: p for p in scenario.ports}

    def root(port):
        while port != components[port]:
            port = components[port]
        return port

    for edge in active:
        if not edge.marine:
            components[root(edge.origin)] = root(edge.destination)

    origins = {c.origin for c in cargo}
    routes = {p: distances(water, p) for p in origins}
    river_routes = {p: distances(inland, p) for p in origins}
    reach = {p: distances(water, p) for p in scenario.ports if p not in origins}
    reach.update(routes)
    reachable = [c for c in cargo if c.destination in routes[c.origin]]
    speed = average(min(t.speed_upstream_kmh, t.speed_downstream_kmh) for t in tugs)
    available_ports = Counter(v.initial.port for v in vessels)
    paired_ports = {t.initial.port for t, b in pairs if t.initial.port == b.initial.port}
    demand_by_port = Counter()
    for c in cargo:
        demand_by_port[c.destination] += c.kg
    depth = [max(0.0, min(c["usable_depth_m"] - scenario.underkeel, c["max_allowed_draft_m"]))
             for e in edges for c in (e.condition, *e.overrides.values()) if not c["is_closed"]]
    capacity = sum(b.capacity_kg for b in barges)
    route_hours = [ratio(routes[c.origin][c.destination], speed) for c in reachable]
    features = {
        "ports": len(scenario.ports), "edges": len(edges), "basins": len({root(p) for p in scenario.ports}),
        "reachability": ratio(sum(len(d) - 1 for d in reach.values()), len(reach) * (len(reach) - 1)),
        "marine_share": ratio(len(marine), len(edges)), "canal_share": ratio(sum(e.water_type == "CANAL" for e in edges), len(edges)),
        "estuary_share": ratio(sum(e.water_type == "ESTUARY" for e in edges), len(edges)),
        "marine_approval_share": average(any(admission_error(scenario, e, t, b) is None for t, b in pairs) for e in marine),
        "marine_reposition": int(scenario.allow_marine_reposition), "days": days,
        "mean_edge_window": average(open_days.values()), "mean_edge_km": average(e.km for e in edges),
        "min_draft_limit": min(depth, default=0), "mean_draft_limit": average(depth),
        "closed_override_share": ratio(sum(c["is_closed"] for e in edges for c in e.overrides.values()),
                                       sum(len(e.overrides) for e in edges)),
        "tugs": len(tugs), "barges": len(barges), "pairs": len(pairs),
        "colocated_pair_share": average(t.initial.port == b.initial.port for t, b in pairs),
        "fleet_port_share": ratio(len(available_ports), len(scenario.ports)), "capacity_t": capacity / 1000,
        "mean_capacity_t": average(b.capacity_kg / 1000 for b in barges),
        "mean_speed": speed, "mean_draft": average([t.draft for t in tugs] + [b.draft_full for b in barges]),
        "fleet_classes": len({v.access.vessel_class for v in vessels if v.access.vessel_class}),
        "mean_fleet_days": average(max(0, (min(scenario.end, v.available_to) - v.initial.available_at).total_seconds())
                                   / 86400 for v in vessels),
        "demand_t": demand / 1000, "lots": len(cargo), "cargo_types": len({c.type for c in cargo}),
        "demand_to_capacity": ratio(demand, capacity),
        "reachable_share": ratio(sum(c.kg for c in reachable), demand),
        "marine_demand_share": ratio(sum(c.kg for c in reachable if c.destination not in river_routes[c.origin]), demand),
        "demand_route_km": ratio(sum(c.kg * routes[c.origin][c.destination] for c in reachable), demand),
        "demand_origin_with_pair_share": ratio(sum(c.kg for c in cargo if c.origin in paired_ports), demand),
        "destination_concentration": ratio(max(demand_by_port.values(), default=0), demand),
        "mean_ready_days": average((c.ready - scenario.start).total_seconds() / 86400 for c in cargo),
        "mean_due_hours": average((c.due - c.ready).total_seconds() / 3600 for c in cargo),
        "mean_route_hours": average(route_hours), "priority_share": ratio(sum(c.kg for c in cargo if c.priority), demand),
        "critical_share": ratio(sum(c.kg for c in cargo if c.critical_category), demand),
        "social_minimum_t": sum(r.required_kg for r in scenario.requirements) / 1000,
        "social_requirements": len(scenario.requirements), "berth_ports": len(scenario.berths),
        "tariffs_present": int(tariffs is not None),
    }
    for name in ("loaded_fuel", "empty_fuel", "tug_operation", "barge_operation", "loading",
                 "unloading", "waiting", "port_call", "repositioning"):
        features["rate_" + name] = 0.0
    features.update(fuel_by_hour=0, tariff_override_count=0, tariff_override_mean=0.0,
                    alternative_priced_share=0.0, alternative_rate_per_demand_t=0.0)
    if tariffs is not None:
        if tariffs.get("tariff_version") != 2:
            raise ValueError("Network ML uses version 2 tariffs")
        cost = CostModel(scenario, tariffs)
        features.update({"rate_" + k: float(v) for k, v in cost.rates.items()})
        overrides = [float(v) for rates in (*cost.vessel_rates.values(), *cost.port_rates.values())
                     for v in rates.values()]
        features.update(fuel_by_hour=int(cost.fuel_basis == "hour"),
                        tariff_override_count=len(overrides), tariff_override_mean=average(overrides),
                        alternative_priced_share=ratio(sum(c.kg for c in cargo if c.type in cost.alternative), demand),
                        alternative_rate_per_demand_t=ratio(
                            sum(c.kg * float(cost.alternative.get(c.type, 0)) for c in cargo), demand))
    return features
