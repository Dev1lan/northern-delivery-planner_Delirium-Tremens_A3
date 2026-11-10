import unittest
from experiments.tiny_exact import setup, exact_objective
from optimizer import simulate

class ExactTests(unittest.TestCase):
    def test_tiny_optima(self):
        for name in ('two_units_three_cargo','draft','different_availability','deadlines'):
            with self.subTest(name=name):
                scenario=setup(name)
                optimal=exact_objective(scenario)
                result=simulate(scenario,'OPTIMIZED')
                metrics=result['metrics']
                self.assertEqual((metrics['delivered_on_time_t'],metrics['delivered_total_t'],-metrics['completed_trips']),optimal)

if __name__=='__main__': unittest.main()
