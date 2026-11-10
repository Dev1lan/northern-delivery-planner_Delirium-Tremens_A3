"""Reproducible paired experiment; preserves all original inputs and archives."""
import copy, hashlib, json, time
from pathlib import Path
from generate import write_csv
from optimizer import load, simulate, export

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'results/final_results'
CONFIG=ROOT/'scenario_config.json'
c=json.loads(CONFIG.read_text(encoding='utf-8'))
variants=[('baseline','baseline_central_central'),('scarce_fleet','baseline_central_central'),('excess_fleet','baseline_central_central'),('high_demand','demand_plus30_central_central'),('short_navigation','navigation_minus10_central_central'),('shallow','baseline_shallow_central')]

def scenario(rep, variant, folder):
    path=ROOT/f'data/scenarios/r{rep}_{folder}'
    s=load(path,copy.deepcopy(c)); s['id']=f'r{rep}_{variant}'
    if variant=='scarce_fleet': s['units']=s['units'][:1]
    if variant=='excess_fleet':
        extra=copy.deepcopy(s['units'])
        for u in extra:
            for k in ('unit_id','barge_id','tug_id'): u[k]+='_EXTRA'
        s['units']+=extra
    return s,path

def main():
    OUT.mkdir(exist_ok=True); rows=[]; types=[]; hashes={str(CONFIG.relative_to(ROOT)):hashlib.sha256(CONFIG.read_bytes()).hexdigest()}
    for rep in (1,2,3):
        for variant,folder in variants:
            s,path=scenario(rep,variant,folder)
            for filename in ('cargo.csv','units.csv','conditions.csv'):
                p=path/filename; hashes[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
            results=[]
            for strategy in ('FCFS','OPTIMIZED'):
                start=time.perf_counter(); result=simulate(s,strategy); seconds=round(time.perf_counter()-start,3)
                results.append(result); rows.append(dict(**result['metrics'],replicate=rep,variant=variant,candidate_limit='unlimited',elapsed_seconds=seconds))
                cargo_types=sorted({x['cargo_type'] for x in s['cargo']})
                for typ in cargo_types:
                    demand=sum(float(x['volume_t']) for x in s['cargo'] if x['cargo_type']==typ)
                    types.append(dict(scenario_id=s['id'],replicate=rep,variant=variant,strategy=strategy,cargo_type=typ,demand_t=round(demand,3),delivered_t=result['delivered_by_type_t'].get(typ,0),on_time_t=result['on_time_by_type_t'].get(typ,0)))
                print(s['id'],strategy,result['metrics']['delivered_on_time_t'],seconds,flush=True)
            export(results,OUT/s['id'])
    write_csv(ROOT/'results/comparison_final.csv',rows)
    write_csv(ROOT/'results/ml_scenario_results.csv',types)
    for filename in ('generate.py','optimizer.py','validate.py','experiments/run_final_experiments.py'):
        source=ROOT/filename; hashes[filename]=hashlib.sha256(source.read_bytes()).hexdigest()
    (OUT/'input_hashes.json').write_text(json.dumps(hashes,indent=2,ensure_ascii=False),encoding='utf-8')
    limit_rows=[]
    s,_=scenario(1,'baseline','baseline_central_central')
    for limit in (5,20,None):
        s['candidate_limit']=limit; start=time.perf_counter(); result=simulate(s,'OPTIMIZED')
        limit_rows.append(dict(**result['metrics'],candidate_limit='unlimited' if limit is None else limit,elapsed_seconds=round(time.perf_counter()-start,3)))
        print('candidate_limit',limit,result['metrics']['delivered_on_time_t'],limit_rows[-1]['elapsed_seconds'],flush=True)
    write_csv(ROOT/'results/candidate_limit_results.csv',limit_rows)

if __name__=='__main__': main()
