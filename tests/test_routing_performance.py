from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest

from network.models import Limits, parse_scenario
from network.routing import Calendar, Router, passage_open


class CalendarTests(unittest.TestCase):
    def test_index_and_skip_match_daily_reference(self):
        raw = json.loads(Path('data/network_demo/a_chain.json').read_text())
        edge = raw['edges'][0]
        base = datetime.fromisoformat(raw['start'])
        edge['daily_conditions'] = {
            (base + timedelta(days=i)).date().isoformat(): value for i, value in {
                1: {'usable_depth_m': 0.8}, 2: {'is_closed': True}, 3: {'is_closed': True},
                4: {'max_allowed_draft_m': 0.6}, 6: {'is_closed': True},
            }.items()
        }
        edge['navigation_windows'] = [
            {'start': base.isoformat(), 'end': (base + timedelta(days=5)).isoformat()},
            {'start': (base + timedelta(days=6)).isoformat(), 'end': (base + timedelta(days=12)).isoformat()},
        ]
        scenario = parse_scenario(raw)
        item = scenario.edges[edge['edge_id']]
        calendar = Calendar(item, scenario.underkeel)
        for wait in (2, 14):
            router = Router(scenario, Limits(max_wait_days=wait))
            for hour in range(0, 220, 7):
                ready = base + timedelta(hours=hour)
                for duration in (timedelta(hours=1), timedelta(days=1), timedelta(hours=49)):
                    for draft in (0.3, 0.8, 1.2, 4.0):
                        cutoff = base + timedelta(days=12)
                        expected = None
                        for start, end in item.windows:
                            day = max(start, ready)
                            while (day.date() <= ready.date() + timedelta(days=wait)
                                   and day + duration <= min(end, cutoff)):
                                clear = passage_open(scenario, item, day, day + duration, draft)
                                self.assertEqual(calendar.blocked_until(day, day + duration, draft) is None, clear)
                                if clear:
                                    expected = day
                                    break
                                day = datetime.combine(day.date() + timedelta(days=1), datetime.min.time())
                            if expected is not None:
                                break
                        actual = router._earliest_leg(item, duration, draft, ready, cutoff)
                        self.assertEqual(actual.depart if actual else None, expected)

    def test_route_cache_keeps_draft_and_scenario_separate(self):
        raw = json.loads(Path('data/network_demo/a_chain.json').read_text())
        first = parse_scenario(raw)
        edge = raw['edges'][0]
        edge['navigation_windows'] = []
        second = parse_scenario(raw)
        routers = [Router(s, Limits()) for s in (first, second)]
        def route(router, draft):
            s = router.scenario
            tug, barge = s.pairs[0]
            return router.paths(s.tugs[tug], s.barges[barge], edge['origin_port'],
                                edge['destination_port'], s.start, s.end, draft)
        self.assertTrue(route(routers[0], 0.2))
        self.assertFalse(route(routers[0], 99))
        self.assertFalse(route(routers[1], 0.2))
        self.assertTrue(route(routers[0], 0.2))
        self.assertEqual(routers[0].stats()['route_cache_hits'], 1)
