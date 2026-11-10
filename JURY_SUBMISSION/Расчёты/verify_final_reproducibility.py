"""Rerun every final strategy and compare full schedule and metrics to saved JSON."""
import concurrent.futures,copy,json,os,hashlib
from pathlib import Path
from optimizer import load,simulate
ROOT=Path(__file__).resolve().parent
ALIAS={'baseline':'baseline_central_central','scarce_fleet':'baseline_central_central','excess_fleet':'baseline_central_central','high_demand':'demand_plus30_central_central','short_navigation':'navigation_minus10_central_central','shallow':'baseline_shallow_central'}

def check(sid):
    rep,variant=sid.split('_',1); folder=f'{rep}_{ALIAS.get(variant,variant)}'
    config=json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8'))
    s=load(ROOT/'data/scenarios'/folder,config); s['id']=sid
    if variant=='scarce_fleet': s['units']=s['units'][:1]
    if variant=='excess_fleet':
        extra=copy.deepcopy(s['units'])
        for u in extra:
            for k in ('unit_id','barge_id','tug_id'): u[k]+='_EXTRA'
        s['units']+=extra
    for strategy in ('FCFS','OPTIMIZED'):
        saved=json.loads((ROOT/'final_results'/sid/(strategy.lower()+'.json')).read_text(encoding='utf-8'))
        actual=simulate(s,strategy)
        if actual!=saved: raise AssertionError(f'Different schedule or metrics: {sid} {strategy}')
    return sid

def main():
    from generate import read_csv
    rows=read_csv(ROOT/'comparison_final.csv'); ids=sorted({x['scenario_id'] for x in rows})
    if len(ids)!=114 or len(rows)!=228: raise AssertionError('Expected 114 pairs')
    hashes=json.loads((ROOT/'final_results/input_hashes.json').read_text(encoding='utf-8'))
    old=json.loads((ROOT/'previous_input_hashes.json').read_text(encoding='utf-8'))
    inputs={k:v for k,v in old.items() if k.replace('\\','/').startswith('data/') or k=='scenario_config.json'}
    for k,v in inputs.items():
        if hashes.get(k)!=v or hashlib.sha256((ROOT/k).read_bytes()).hexdigest()!=v: raise AssertionError('Input changed: '+k)
    with concurrent.futures.ProcessPoolExecutor(max_workers=min(4,os.cpu_count() or 1)) as pool:
        for i,done in enumerate(pool.map(check,ids),1):
            if i%10==0 or i==len(ids): print(f'reproduced {i}/{len(ids)}',flush=True)
    (ROOT/'reproducibility_check.json').write_text(json.dumps({'scenario_pairs':len(ids),'schedules':2*len(ids),'unchanged_input_hashes':len(inputs),'result':'PASS'},indent=2),encoding='utf-8')
    print('PASS: every schedule and metric exactly reproduced; inputs unchanged',flush=True)
if __name__=='__main__': main()
