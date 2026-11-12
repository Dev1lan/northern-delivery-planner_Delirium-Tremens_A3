import json
from copy import deepcopy
from pathlib import Path
import unittest

from network.adapter import decode_voyages, run_scenario
from network.models import parse_scenario
from network.validator import audit


def marine_scenario():
    raw = json.loads((Path(__file__).parents[1] / 'data/network_demo/c_reposition_chain.json').read_text())
    for row in [*raw['tugs'], *raw['barges']]:
        row.update(vessel_class='DEMO_COASTAL', navigation_permits=[
            {'area': 'demo_coast', 'status': 'SCENARIO_ASSUMPTION', 'source': 'Synthetic marine test'}])
    for edge in raw['edges']:
        if edge['edge_id'] in ('CB', 'BC'):
            edge.update(water_type='SEA', navigation_area='demo_coast',
                        allowed_vessel_classes=['DEMO_COASTAL'], allowed_pairs=deepcopy(raw['compatible_pairs']))
    return raw


class MarineTests(unittest.TestCase):
    def test_approved_pair_improves_delivery_and_finishes_in_destination(self):
        raw = marine_scenario()
        migrated = run_scenario(raw)
        self.assertEqual(migrated['metrics']['delivered_on_time_t'], 180)
        self.assertIn('CB', [e['edge_id'] for e in migrated['voyages'][0]['empty']])
        self.assertEqual(next(r['current_port'] for r in migrated['final_resources']
                              if r['vessel_id'] == 'T_LARGE'), 'C')
        raw['allow_marine_reposition'] = False
        self.assertEqual(run_scenario(raw)['metrics']['delivered_on_time_t'], 75)

    def test_class_only_or_unapproved_barge_fails_planner_and_auditor(self):
        raw = marine_scenario()
        plan = decode_voyages(run_scenario(raw)['voyages'])
        barge = next(r for r in raw['barges'] if r['vessel_id'] == 'B_LARGE')
        barge['navigation_permits'] = []
        self.assertNotIn('CB', [leg['edge_id'] for v in run_scenario(raw)['voyages']
                                if v['barge_id'] == 'B_LARGE' for leg in v['empty']])
        self.assertEqual(audit(parse_scenario(raw), plan)['schedule_audit']['violations'][0]['code'],
                         'MISSING_MARINE_PERMIT')
        barge['vessel_class'] = 'DEMO_RIVER'
        self.assertEqual(audit(parse_scenario(raw), plan)['schedule_audit']['violations'][0]['code'],
                         'CLASS_NOT_ADMITTED')

    def test_missing_edge_closed_window_or_slow_crossing_does_not_migrate(self):
        for change in ('missing', 'closed', 'window', 'slow'):
            with self.subTest(change=change):
                raw = marine_scenario()
                sea = next(e for e in raw['edges'] if e['edge_id'] == 'CB')
                if change == 'missing':
                    raw['edges'].remove(sea)
                elif change == 'closed':
                    sea['condition']['is_closed'] = True
                elif change == 'window':
                    sea['navigation_windows'] = [{'start': '2026-07-01T09:00:00', 'end': raw['end']}]
                else:
                    sea['speed_limit_kmh'] = 1
                self.assertNotIn('CB', [leg['edge_id'] for v in run_scenario(raw)['voyages'] for leg in v['empty']])

    def test_synthetic_permission_is_not_a_real_permit(self):
        raw = marine_scenario()
        raw['data_status'] = 'MIXED'
        for row in [*raw['tugs'], *raw['barges']]:
            row.update(data_status='REAL', source='Test input provenance')
        self.assertNotIn('CB', [leg['edge_id'] for v in run_scenario(raw)['voyages'] for leg in v['empty']])
        for row in [*raw['tugs'], *raw['barges']]:
            row['navigation_permits'][0]['status'] = 'CONFIRMED'
        self.assertTrue(run_scenario(raw)['reposition_comparison']['selected_repositioning'])
