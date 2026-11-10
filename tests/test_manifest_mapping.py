import hashlib
import unittest
from pathlib import Path
from experiments.verify_manifest import ROOT, check_table, normalize, resolve_manifest_path

class ManifestMappingTests(unittest.TestCase):
    def test_windows_separator_and_matching_digest(self):
        p=ROOT/'data/demo/scenario/cargo.csv'
        digest=hashlib.sha256(p.read_bytes()).hexdigest()
        self.assertEqual(normalize(r'data\demo\scenario\cargo.csv'),Path('data/demo/scenario/cargo.csv'))
        self.assertEqual(check_table({r'data\demo\scenario\cargo.csv':digest},ROOT),[])
    def test_historical_manifest_path_resolves(self):
        self.assertEqual(resolve_manifest_path('final_results/input_hashes.json'),Path('results/final_results/input_hashes.json'))

    def test_wrong_or_missing_digest_fails(self):
        self.assertTrue(check_table({'scenario_config.json':'0'*64},ROOT))
        self.assertTrue(check_table({'missing.csv':'0'*64},ROOT))

if __name__=='__main__': unittest.main()
