"""Three independent seeds, identical inputs for each strategy and targeted audit tests."""
import copy
import json
from pathlib import Path
from optimizer import load, simulate, STRATEGIES, export
from validate import audit

ROOT = Path(__file__).resolve().parent
config = json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8'))
results = []
for rep in (1, 2, 3):
    for variant, folder in [('scarce_fleet','baseline_central_central'), ('excess_fleet','baseline_central_central'),
                            ('high_demand','demand_plus30_central_central'), ('short_navigation','navigation_minus10_central_central'),
                            ('shallow','baseline_shallow_central')]:
        s = load(ROOT/f'data/scenarios/r{rep}_{folder}', copy.deepcopy(config))
        s['id'] = f'r{rep}_{variant}'
        if variant == 'scarce_fleet':
            s['units'] = s['units'][:1]
        elif variant == 'excess_fleet':
            extra = copy.deepcopy(s['units'])
            for u in extra:
                for key in ('unit_id','barge_id','tug_id'): u[key] += '_EXTRA'
            s['units'] += extra
        run = [simulate(s, strategy) for strategy in STRATEGIES]
        export(run, ROOT/'experiment_results'/s['id'])
        results.extend(r['metrics'] for r in run)
        print(s['id'], [(r['metrics']['strategy'], r['metrics']['delivered_on_time_t']) for r in run], flush=True)
from generate import write_csv
write_csv(ROOT/'experiment_results/comparison.csv', results)
(ROOT/'experiment_results/comparison.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
