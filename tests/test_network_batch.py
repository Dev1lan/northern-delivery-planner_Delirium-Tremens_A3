import json
from pathlib import Path
import unittest

from network.adapter import audit_result
from network.batch import PlanningJob, run_jobs
from network.scheduler import COST, FCFS


class BatchTests(unittest.TestCase):
    def test_processes_preserve_order_and_exact_results(self):
        root = Path(__file__).resolve().parents[1]
        data = root / 'data' / 'yakutia_demo'
        scenario = json.loads((data / 'yakutia_stress.json').read_text(encoding='utf-8-sig'))
        tariffs = json.loads((data / 'tariffs_base.json').read_text(encoding='utf-8-sig'))
        jobs = [PlanningJob(scenario, COST, tariffs=tariffs),
                PlanningJob(scenario, FCFS),
                PlanningJob(scenario)]
        serial = [run.result for run in run_jobs(jobs)]
        parallel = list(run_jobs(iter(jobs), workers=2))
        self.assertEqual([run.result for run in parallel], serial)
        for run in parallel:
            self.assertGreaterEqual(run.compute_seconds, 0)
            self.assertEqual(audit_result(run.result)['schedule_audit']['status'], 'PASSED')

    def test_validation_and_empty_batch(self):
        self.assertEqual(list(run_jobs([], workers=4)), [])
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            list(run_jobs([], workers=0))

    def test_worker_failure_reaches_caller(self):
        root = Path(__file__).resolve().parents[1]
        scenario = json.loads((root / 'data' / 'network_demo' / 'a_chain.json').read_text())
        jobs = [PlanningJob(scenario, 'UNKNOWN'), PlanningJob(scenario)]
        with self.assertRaisesRegex(ValueError, 'Unknown network strategy'):
            list(run_jobs(jobs, workers=2))
