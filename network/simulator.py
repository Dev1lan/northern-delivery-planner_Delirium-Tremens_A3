"""Generate physically plausible load/route choices for one compatible pair."""

from network.models import Voyage, handling_time, loaded_draft
from network.routing import Router
from network.ports import slots


def voyages(scenario, tug, barge, cargo, capacity, origin, ready, limits, *, router=None,
            allow_reposition=False, require_return=False, requirement=None, deficit_kg=0, bookings=None, finish_by=None):
    bookings = bookings or {}
    cutoff = min(scenario.end, tug.available_to, barge.available_to, finish_by or scenario.end)
    delivery_cutoff = min(cutoff, cargo.expires_at) if cargo.expires_at is not None else cutoff
    if ready >= delivery_cutoff or cargo.ready >= delivery_cutoff or not capacity:
        return ()
    if origin != cargo.origin and not allow_reposition:
        return ()
    empty_draft = loaded_draft(tug, barge, 0)
    empty_cutoff = min(delivery_cutoff, cargo.due) if origin != cargo.origin else delivery_cutoff
    router = router or Router(scenario, limits)
    approaches = router.paths(tug, barge, origin, cargo.origin, ready, empty_cutoff, empty_draft, reposition=True)
    results = {}
    for empty in approaches:
        at_pickup = max(cargo.ready, empty[-1].arrive if empty else ready)
        cached = {}

        def at_mass(kg, deadline):
            key = (kg, deadline)
            if key in cached:
                return cached[key]
            handling = handling_time(scenario, barge, kg)
            draft = loaded_draft(tug, barge, kg)
            options = []
            earliest_unload = deadline if cargo.destination in scenario.berths else deadline - handling
            for loader, load_start, departure in slots(scenario, cargo.origin, 'LOADING', barge, cargo.type,
                                                       kg, draft, at_pickup, earliest_unload, bookings):
                for loaded in router.paths(tug, barge, cargo.origin, cargo.destination, departure,
                                           earliest_unload, draft):
                    for unloader, unload_start, unloaded in slots(
                            scenario, cargo.destination, 'UNLOADING', barge, cargo.type,
                            kg, draft, loaded[-1].arrive, deadline, bookings):
                        back = router.paths(tug, barge, cargo.destination, cargo.origin, unloaded, cutoff,
                                            empty_draft) if require_return else ((),)
                        for return_legs in back:
                            options.append(Voyage(tug.id, barge.id, cargo.id, kg, empty, load_start, departure,
                                                  loaded, unloaded, return_legs, loader, unloader,
                                                  at_pickup if loader else None, unload_start if unloader else None))
            cached[key] = tuple(options)
            return cached[key]

        def best_mass(kg, deadline):
            if kg < 1 or deadline <= at_pickup:
                return ()
            first = at_mass(kg, deadline)
            if first:
                return first
            # A smaller load can depart sooner and needs less water. Waiting at a
            # port is allowed; path and wait budgets keep this an approximation.
            lo, hi = 0, kg - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if at_mass(mid, deadline):
                    lo = mid
                else:
                    hi = mid - 1
            return at_mass(lo, deadline) if lo else ()

        local_cutoff = min(delivery_cutoff, cargo.due) if empty else delivery_cutoff
        options = best_mass(capacity, local_cutoff)
        for option in options:
            results[option] = None
        if not empty and cargo.due < delivery_cutoff and not any(v.unloading_end <= cargo.due for v in options):
            for option in best_mass(capacity, cargo.due):
                results[option] = None
        if requirement is not None and deficit_kg:
            target = min(capacity, deficit_kg)
            deadline = min(local_cutoff, requirement.deadline or cutoff)
            for option in best_mass(target, deadline):
                results[option] = None
    return tuple(results)
