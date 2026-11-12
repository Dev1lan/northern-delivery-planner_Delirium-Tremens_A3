import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator, ValidationError

from experiments.run_comparison import run_experiment
from experiments.social_examples import examples
from result_adapter import run_scenario


ROOT = Path(__file__).resolve().parents[1]


class ExtendedResultTests(unittest.TestCase):
    def test_contract_v1_and_v2(self):
        path = ROOT / 'data/demo/scenario'
        requirements = json.loads((ROOT / 'data/demo/social_requirements.json').read_text(encoding='utf-8'))
        legacy = run_scenario(path, strategy='FCFS')
        schema = json.loads((ROOT / 'result.v2.schema.json').read_text(encoding='utf-8'))
        validator = Draft202012Validator(schema)
        a = run_scenario(path, strategy='FCFS', contract_version='2.0.0', requirements=requirements)
        b = run_scenario(path, strategy='SOCIAL_PRIORITY', contract_version='2.0.0', requirements=requirements)
        validator.validate(a)
        validator.validate(b)
        self.assertEqual(a['input_fingerprint'], b['input_fingerprint'])
        self.assertEqual(a['requirements_fingerprint'], b['requirements_fingerprint'])
        self.assertEqual(a['trips'], legacy['trips'])
        self.assertEqual(a['metrics'], legacy['metrics'])
        with self.assertRaises(ValidationError):
            validator.validate(legacy)
        with self.assertRaises(ValidationError):
            validator.validate(dict(b, feasibility_status='NOT_CHECKED'))
        with self.assertRaises(ValueError):
            run_scenario(path, strategy='SOCIAL_PRIORITY')

    def test_runner_never_overwrites_existing_results(self):
        s, requirements = next(examples())
        with TemporaryDirectory(dir=ROOT / 'results') as directory:
            output = Path(directory) / 'run'
            strategies = ('FCFS', 'SOCIAL_PRIORITY')
            result = run_experiment([(s, requirements, None)], strategies, output)
            self.assertEqual(result['schedules'], 2)
            self.assertEqual(result['physical_violations'], 0)
            original = (output / 'manifest.json').read_bytes()
            with self.assertRaises(FileExistsError):
                run_experiment([(s, requirements, None)], strategies, output)
            self.assertEqual((output / 'manifest.json').read_bytes(), original)
