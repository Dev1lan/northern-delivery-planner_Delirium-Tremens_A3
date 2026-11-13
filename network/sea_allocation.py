"""Estimate competing uses of scarce sea-capable resources from feasible voyages."""

from collections import defaultdict
from datetime import timedelta
from heapq import nsmallest


class SeaAllocation:
    def __init__(self, scenario, router):
        self.scenario = scenario
        self.router = router
        self.sea_edges = {edge.id for edge in scenario.edges.values() if edge.marine}
        self.required = set()
        for cargo in scenario.cargo.values():
            visited = {cargo.origin}
            pending = [cargo.origin]
            while pending:
                port = pending.pop()
                for edge in scenario.outgoing[port]:
                    if not edge.marine and edge.destination not in visited:
                        visited.add(edge.destination)
                        pending.append(edge.destination)
            if cargo.destination not in visited:
                self.required.add(cargo.id)
        self.specialized = set()
        for tug, barge in scenario.pairs:
            if any(router.edge_order[index].id in self.sea_edges
                   for index, _ in router.signature_values[router.pair_signature(tug, barge)[0]]):
                self.specialized.update((tug, barge))

    def occupies_sea(self, move):
        return any(leg.edge_id in self.sea_edges for leg in move.loaded)

    def slack(self, move, pair):
        cargo = self.scenario.cargo[move.cargo_id]
        tug, barge = self.scenario.tugs[pair[0]], self.scenario.barges[pair[1]]
        latest = min(cargo.due, cargo.expires_at or cargo.due, self.scenario.end,
                     tug.available_to, barge.available_to) - move.unloading_end
        for leg in (*move.empty, *move.loaded):
            edge = self.scenario.edges[leg.edge_id]
            if edge.marine:
                latest = min(latest, next(end - leg.arrive for start, end in edge.windows
                                          if start <= leg.depart < leg.arrive <= end))
        return max(timedelta(), latest)

    def shortlist(self, items, limit, remaining):
        # All stored witnesses come from the physical simulator. Keep the best
        # ordinary alternative and marine voyage per pair and cargo upstream.
        marine = defaultdict(list)
        ordinary = defaultdict(list)
        for item in items:
            rank, move, pair = item
            if rank[2] != -1:
                continue
            if self.occupies_sea(move):
                marine[move.cargo_id].append(item)
            elif self.specialized.isdisjoint(pair):
                ordinary[move.cargo_id].append(item)

        if not marine:
            return nsmallest(limit, items, key=lambda item: item[0]), 0

        threats = defaultdict(list)
        slack_by_item = {}
        for options in marine.values():
            self.router.check_budget()
            # A dozen pairs sharing one tanker are still scarce.
            pairs = {item[2] for item in options}
            for sea in options:
                slack = slack_by_item[id(sea)] = self.slack(sea[1], sea[2])
                for vessel in sea[2]:
                    threats[vessel].append((sea, pairs, slack))

        def risk(item, sea, slack):
            _, river, pair = item
            _, voyage, sea_pair = sea
            if river.start >= voyage.end:
                return False
            origin = self.scenario.cargo[river.cargo_id].destination
            target = self.scenario.cargo[voyage.cargo_id].origin
            if pair != sea_pair:
                partner_port = (self.scenario.edges[voyage.empty[0].edge_id].origin if voyage.empty else target)
                return origin != partner_port or river.end > voyage.start + slack
            approach = self.router.lower_bound(self.scenario.tugs[sea_pair[0]], self.scenario.barges[sea_pair[1]],
                                               origin, target, reposition=True)
            return approach is None or river.end + approach > voyage.load_start + slack

        adjustments = {}
        for item in items:
            rank, move, pair = item
            if rank[2] != -1 or self.specialized.isdisjoint(pair) or self.occupies_sea(move):
                continue
            shared = set(pair)
            replacement = max((other[1].kg for other in ordinary[move.cargo_id]
                               if shared.isdisjoint(other[2])), default=0)
            uncovered = max(0, move.kg - replacement)
            for vessel in pair:
                for sea, alternatives, slack in threats[vessel]:
                    if sea[1].kg <= uncovered or not risk(item, sea, slack):
                        continue
                    # Losing a pair matters less if another disjoint pair can
                    # do the same job. River capacity foregone offsets benefit.
                    independent = any(shared.isdisjoint(alt) for alt in alternatives)
                    loss = (sea[1].kg - uncovered) // (2 if independent else 1)
                    adjustments[id(item)] = max(adjustments.get(id(item), 0), loss)
                    adjustments[id(sea)] = min(adjustments.get(id(sea), 0), -loss)

        def rank(item):
            original, move, _ = item
            opportunity = adjustments.get(id(item), 0)
            if self.occupies_sea(move) and move.cargo_id in self.required and original[2] == -1:
                # A feasible voyage can open a chain on a mandatory sea corridor.
                # Give larger residuals more weight when the remaining window
                # is too short to assume the fleet can be reassigned later.
                slack = slack_by_item.get(id(item))
                slack_days = (slack if slack is not None else self.slack(move, item[2])).days
                opportunity = min(opportunity, -min(remaining[move.cargo_id], 2 * move.kg) / (1 + slack_days))
            return (*original[:3], opportunity, *original[3:])

        chosen = nsmallest(limit, items, key=rank)
        if limit > 1 and marine and not any(self.occupies_sea(item[1]) for item in chosen):
            sea = min((item for options in marine.values() for item in options), key=rank)
            if rank(sea)[:3] <= rank(chosen[-1])[:3]:
                chosen[-1] = sea
        return chosen, len(adjustments)
