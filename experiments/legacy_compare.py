"""Run the archive's original dispatcher as a separate reference."""
import copy
import json
from pathlib import Path
from generate import load_scenario, simulate, write_csv, read_csv, TRIP_FIELDS
from validate import audit
ROOT = Path(__file__).resolve().parent.parent
config = json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8'))
rows = []
for rep in (1, 2, 3):
    for variant, folder in [('scarce_fleet','baseline_central_central'), ('excess_fleet','baseline_central_central'),
                            ('high_demand','demand_plus30_central_central'), ('short_navigation','navigation_minus10_central_central'),
                            ('shallow','baseline_shallow_central')]:
        cargo, units, conditions = load_scenario(ROOT/f'data/scenarios/r{rep}_{folder}')
        if variant == 'scarce_fleet': units = units[:1]
        if variant == 'excess_fleet':
            extra = copy.deepcopy(units)
            for u in extra:
                for key in ('unit_id','barge_id','tug_id'): u[key] += '_EXTRA'
            units += extra
        trips, m = simulate(config, cargo, units, conditions, 'optimized_heuristic')
        audit(config, cargo, units, conditions, trips)
        scenario_id = f'r{rep}_{variant}'
        row = dict(scenario_id=scenario_id, strategy='ARCHIVE_EARLIEST_DUE', delivered_on_time_t=m['delivered_on_time_t'],
                   delivered_total_t=m['total_delivered_t'], undelivered_t=m['undelivered_t'], completed_trips=m['completed_trips'],
                   vessel_idle_days=m['vessel_idle_days'], fleet_utilization=m['fleet_utilization'],
                   average_delivery_days=m['average_delivery_days'], violations=0)
        rows.append(row)
        write_csv(ROOT/'results/experiment_results'/scenario_id/'archive_earliest_due_trips.csv', trips, TRIP_FIELDS)
write_csv(ROOT/'results/experiment_results/archive_reference.csv', rows)
base = ROOT/'results/experiment_results/comparison.csv'
if base.exists():
    combined = read_csv(base) + rows
    combined.sort(key=lambda r: (r['scenario_id'], r['strategy']))
    write_csv(ROOT/'results/experiment_results/comparison_all.csv', combined)
    (ROOT/'results/experiment_results/comparison_all.json').write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'Archive reference: {len(rows)} scenarios')
