from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import unittest

from jsonschema import Draft202012Validator

from experiments.run_network import SCHEMA
from network.adapter import audit_result, decode_voyages, run_scenario
from network.models import parse_scenario
from network.scheduler import FCFS
from network.validator import audit
from tests.test_network import load


def queued_scenario():
    raw = load('a_chain')
    raw['cargo'] = [dict(raw['cargo'][0], due_at='2026-07-01T04:30:00')]
    raw['cargo'].append(dict(raw['cargo'][0], cargo_id='SECOND'))
    raw['tugs'].append(dict(raw['tugs'][0], vessel_id='T2'))
    raw['barges'].append(dict(raw['barges'][0], vessel_id='B2'))
    raw['compatible_pairs'].append({'tug_id': 'T2', 'barge_id': 'B2'})
    raw['port_operations'] = {'B': {'berths': [{
        'berth_id': 'B-1', 'operations': ['LOADING', 'UNLOADING'], 'handling_tph': 100,
        'initial_occupancy': [{'start': raw['start'], 'end': '2026-07-01T03:30:00'}],
    }]}}
    return raw


class PortTests(unittest.TestCase):
    def test_queue_changes_timeliness_and_counts_occupancy(self):
        raw = queued_scenario()
        unrestricted = deepcopy(raw)
        del unrestricted['port_operations']
        self.assertEqual(run_scenario(unrestricted, FCFS)['metrics']['delivered_on_time_t'], 200)
        limited = run_scenario(raw, FCFS)
        self.assertEqual(limited['metrics']['delivered_on_time_t'], 100)
        self.assertEqual(limited['metrics']['delivered_total_t'], 200)
        port = limited['port_operations']['B']
        self.assertEqual(port['waiting_seconds'], 4 * 3600)
        self.assertEqual(port['berths'][0]['busy_seconds'], 5.5 * 3600)
        self.assertEqual(port['unloaded_t'], 200)
        Draft202012Validator(json.loads(SCHEMA.read_text())).validate(limited)
        self.assertEqual(audit_result(limited)['schedule_audit']['status'], 'PASSED')

    def test_independent_audit_rejects_double_booking(self):
        raw = queued_scenario()
        first, second = decode_voyages(run_scenario(raw, FCFS)['voyages'])
        bad = replace(second, unloading_start=first.unload_start, unloading_end=first.unloading_end)
        result = audit(parse_scenario(raw), (first, bad))
        self.assertEqual(result['schedule_audit']['violations'][0]['code'], 'BERTH_BUSY')

    def test_shared_berth_counts_loading_and_unloading_against_one_slot(self):
        raw = queued_scenario()
        raw['cargo'][1].update(origin_port='B', destination_port='C', ready_at='2026-07-01T03:00:00')
        for row in (raw['barges'][1], raw['tugs'][1]):
            row['current_port'] = 'B'
        result = run_scenario(raw, FCFS)
        ops = [e for e in result['events'] if e.get('berth_id') == 'B-1']
        self.assertEqual([e['state'] for e in ops], ['UNLOADING', 'LOADING'])
        self.assertEqual(ops[0]['end'], ops[1]['start'])
