import unittest

from network.adapter import audit_result, run_scenario
from network.models import Limits
from network.scheduler import COST, REPOSITION, SOCIAL
from network.search_policy import SearchPolicy
from tests.test_costs import tariffs
from tests.test_network import load


def scenario():
    raw = load('a_chain')
    raw['cargo'][0]['volume_t'] = 90
    raw['cargo'][1].update(origin_port='A', destination_port='C', volume_t=50, cargo_type='food')
    tug, barge = raw['tugs'][0], raw['barges'][0]
    tug['speed_upstream_kmh'] = 20
    for vessel in (tug, barge):
        vessel.update(vessel_class='COAST', navigation_permits=[
            {'area': 'DEMO', 'status': 'SCENARIO_ASSUMPTION', 'source': 'Test'}])
    raw['tugs'].append(dict(tug, vessel_id='T2', speed_upstream_kmh=8, navigation_permits=[]))
    raw['barges'].append(dict(barge, vessel_id='B2', navigation_permits=[]))
    raw['compatible_pairs'].append(dict(tug_id='T2', barge_id='B2'))
    edge = next(e for e in raw['edges'] if e['edge_id'] == 'BC')
    edge.update(water_type='SEA', navigation_area='DEMO', allowed_pairs=[raw['compatible_pairs'][0]],
                navigation_windows=[dict(start=raw['start'], end='2026-07-01T02:00:00')])
    raw['cargo'][1]['due_at'] = '2026-07-01T03:00:00'
    return raw


class SeaAllocationTests(unittest.TestCase):
    def plan(self, raw, enabled=True, strategy=REPOSITION):
        result = run_scenario(raw, strategy, limits=Limits(candidate_limit=1, beam_width=1),
                              tariffs=tariffs({'tug_operation': 20}) if strategy == COST else None,
                              search_policy=SearchPolicy(sea_fleet_allocation=enabled))
        self.assertEqual(audit_result(result)['schedule_audit']['status'], 'PASSED')
        return result

    def test_river_alternative_preserves_the_only_sea_pair(self):
        raw = scenario()
        before = self.plan(raw, False)
        after = self.plan(raw)
        self.assertEqual(before['metrics']['delivered_on_time_t'], 90)
        self.assertEqual(after['metrics']['delivered_on_time_t'], 140)
        self.assertEqual({v['cargo_id']: v['tug_id'] for v in after['voyages']}, {'AB': 'T2', 'BC': 'T1'})
        self.assertGreater(after['search_diagnostics']['sea_opportunity_adjustments'], 0)

    def test_no_sea_cargo_or_no_navigable_window_allows_river_work(self):
        for change in ('removed', 'closed', 'tanker', 'reposition'):
            with self.subTest(change=change):
                raw = scenario()
                if change == 'removed':
                    raw['cargo'].pop()
                elif change == 'closed':
                    next(e for e in raw['edges'] if e['edge_id'] == 'BC')['navigation_windows'] = []
                elif change == 'tanker':
                    raw['barges'][0]['cargo_types_allowed'] = ['coal']
                else:
                    raw['cargo'][1]['origin_port'] = 'D'
                result = self.plan(raw)
                self.assertEqual(result['voyages'][0]['tug_id'], 'T1')
                self.assertEqual(result['search_diagnostics']['sea_opportunity_adjustments'], 0)

    def test_unusable_river_alternative_is_not_counted(self):
        raw = scenario()
        raw['tugs'][1]['draft_empty_m'] = 3
        result = self.plan(raw)
        self.assertEqual(result['search_diagnostics']['sea_opportunity_adjustments'], 0)

    def test_sea_cargoes_compete_for_one_pair_with_audited_schedule(self):
        raw = scenario()
        raw['cargo'].append(dict(raw['cargo'][1], cargo_id='OTHER_SEA'))
        result = self.plan(raw)
        sea = [v for v in result['voyages'] if v['cargo_id'] in ('BC', 'OTHER_SEA')]
        self.assertEqual(len(sea), 1)
        self.assertEqual(sea[0]['tug_id'], 'T1')

    def test_cost_and_social_remain_audited(self):
        raw = scenario()
        raw['social_requirements'] = [{'requirement_id': 'FOOD', 'cargo_types': ['food'], 'required_t': 50,
                                       'deadline': raw['cargo'][1]['due_at'], 'basis': 'delivered_by_deadline',
                                       'source_status': 'SYNTHETIC'}]
        for strategy in (COST, SOCIAL):
            with self.subTest(strategy=strategy):
                result = self.plan(raw, strategy=strategy)
                self.assertEqual(result['requirements_status'], 'SATISFIED')

    def test_no_sea_graph_keeps_existing_plans(self):
        raw = load('a_chain')
        first, second = self.plan(raw, False), self.plan(raw)
        first.pop('search_diagnostics')
        second.pop('search_diagnostics')
        self.assertEqual(first, second)
