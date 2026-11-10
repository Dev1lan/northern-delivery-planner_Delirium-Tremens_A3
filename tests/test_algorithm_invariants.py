import ast, hashlib, json, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
class AlgorithmInvariantTests(unittest.TestCase):
    def test_heuristic_2_1_0_unchanged(self):
        expected=json.loads((ROOT/'algorithm_invariants.json').read_text(encoding='utf-8'))
        tree=ast.parse((ROOT/'optimizer.py').read_text(encoding='utf-8'))
        for name,digest in expected['functions_ast_sha256'].items():
            with self.subTest(name=name):
                node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name==name)
                if name=='simulate':
                    node.body=[x for x in node.body if not (isinstance(x,ast.Expr) and isinstance(x.value,ast.Call) and isinstance(x.value.func,ast.Name) and x.value.func.id=='validate_inputs')]
                self.assertEqual(hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest(),digest)
if __name__=='__main__': unittest.main()
