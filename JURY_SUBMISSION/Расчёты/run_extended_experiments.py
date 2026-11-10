"""Run all archived scenarios after run_final_experiments.py, without changing inputs."""
import concurrent.futures,hashlib,json,os,time
from pathlib import Path
from generate import read_csv,write_csv
from optimizer import load,simulate,export

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'final_results'
ALIASES={'baseline_central_central','baseline_shallow_central','demand_plus30_central_central','navigation_minus10_central_central'}

def run_one(folder):
    path=ROOT/'data/scenarios'/folder
    config=json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8'))
    s=load(path,config); s['id']=folder
    result=[]
    for strategy in ('FCFS','OPTIMIZED'):
        start=time.perf_counter(); value=simulate(s,strategy)
        result.append((value,round(time.perf_counter()-start,3)))
    demands={typ:round(sum(float(x['volume_t']) for x in s['cargo'] if x['cargo_type']==typ),3) for typ in sorted({x['cargo_type'] for x in s['cargo']})}
    return folder,result,demands

def main():
    rows=read_csv(ROOT/'comparison_final.csv'); types=read_csv(ROOT/'ml_scenario_results.csv')
    # Starting from the central/fleet run makes repeated execution idempotent.
    base_ids={f'r{rep}_{v}' for rep in (1,2,3) for v in ('baseline','scarce_fleet','excess_fleet','high_demand','short_navigation','shallow')}
    rows=[x for x in rows if x['scenario_id'] in base_ids]; types=[x for x in types if x['scenario_id'] in base_ids]
    hashes=json.loads((OUT/'input_hashes.json').read_text(encoding='utf-8'))
    paths=[p for p in sorted((ROOT/'data/scenarios').iterdir()) if p.is_dir() and p.name.split('_',1)[1] not in ALIASES]
    workers=min(4,os.cpu_count() or 1)
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(run_one,p.name) for p in paths]
        for index,f in enumerate(concurrent.futures.as_completed(futures),1):
            name,results,demands=f.result(); rep=int(name.split('_',1)[0][1:]); variant=name.split('_',1)[1]
            for value,seconds in results:
                strategy=value['metrics']['strategy']
                rows.append(dict(**value['metrics'],replicate=rep,variant=variant,candidate_limit='unlimited',elapsed_seconds=seconds))
                for typ,demand in demands.items():
                    types.append(dict(scenario_id=name,replicate=rep,variant=variant,strategy=strategy,cargo_type=typ,demand_t=demand,delivered_t=value['delivered_by_type_t'].get(typ,0),on_time_t=value['on_time_by_type_t'].get(typ,0)))
            export([x[0] for x in results],OUT/name)
            print(f'{index}/{len(paths)} {name}',flush=True)
    rows.sort(key=lambda x:(x['scenario_id'],x['strategy']))
    types.sort(key=lambda x:(x['scenario_id'],x['strategy'],x['cargo_type']))
    write_csv(ROOT/'comparison_final.csv',rows); write_csv(ROOT/'ml_scenario_results.csv',types)
    for path in paths:
        for filename in ('cargo.csv','units.csv','conditions.csv'):
            p=path/filename; hashes[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    for filename in ('generate.py','optimizer.py','validate.py','run_final_experiments.py','run_extended_experiments.py'):
        p=ROOT/filename; hashes[filename]=hashlib.sha256(p.read_bytes()).hexdigest()
    (OUT/'input_hashes.json').write_text(json.dumps(hashes,indent=2,ensure_ascii=False),encoding='utf-8')
    print('Completed paired scenarios:',len(rows)//2,flush=True)
if __name__=='__main__': main()
