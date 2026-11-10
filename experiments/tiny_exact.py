"""Exhaustive oracle for tiny instances with 1 kg indivisible loads and hourly events.
All ready times, windows and four-hour cycles are whole hours, so an earliest
feasible schedule can be shifted to this grid without reducing its objective.
"""
import copy, itertools, json
from datetime import timedelta
from pathlib import Path
from generate import dt, iso, make_conditions, trial_trip, write_csv
from optimizer import simulate

ROOT=Path(__file__).resolve().parent.parent

def setup(name):
    c=json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8'))
    c.update(start='2026-06-10',end='2026-06-11',handling_fixed_hours=0,underkeel_m=.1)
    c['segments']=[dict(segment_id='s',from_node=c['origin'],to_node=c['destination'],distance_km=1)]
    c['depth_profile']=[[c['start'],2],['2026-06-12',2]]
    close=dt(c['end'])+timedelta(days=1)
    conditions=make_conditions(c,close,0)
    units=[]
    for i in range(2):
        units.append(dict(unit_id=f'u{i}',barge_id=f'b{i}',tug_id=f't{i}',capacity_t=.001,reference_capacity_t=.001,cargo_type_allowed=['coal'],draft_empty_m=.2,draft_full_m=.5,tug_draft_m=.2,speed_upstream_kmh=1,speed_downstream_kmh=1,handling_tph=.001,available_from=c['start'],available_to=iso(close)))
    if name=='draft':
        units=units[:1]
        for row in conditions:
            if row['date']=='2026-06-10': row.update(usable_depth_m=.5,max_allowed_draft_m=.4)
    if name=='different_availability': units[0]['available_to']='2026-06-10T08:00:00'
    if name=='deadlines': units=units[:1]
    cargo=[]
    for i in range(3):
        ready=[0,0,4][i]; due=([8,12,20] if name=='deadlines' else [12,16,24])[i]
        cargo.append(dict(cargo_id=f'c{i}',cargo_type='coal',origin=c['origin'],destination=c['destination'],volume_t=.001,ready_date=iso(dt(c['start'])+timedelta(hours=ready)),due_date=iso(dt(c['start'])+timedelta(hours=due)),priority=1))
    if name=='draft':
        for x in cargo: x['due_date']='2026-06-11T20:00:00'
    return dict(id=name,config=c,cargo=cargo,units=units,conditions=conditions)

def exact_objective(s):
    c=s['config']; start=dt(c['start']); close=dt(c['end'])+timedelta(days=1); cm={(x['date'],x['segment_id']):x for x in s['conditions']}
    choices=[]
    for x in s['cargo']:
        opts=[None]
        for u in s['units']:
            for h in range(int((close-start).total_seconds()/3600)):
                when=start+timedelta(hours=h)
                if when<dt(x['ready_date']): continue
                t=trial_trip(c,u,x,.001,when,cm,close)
                if t: opts.append(t)
        choices.append(opts)
    best=(0,0,0)
    for combination in itertools.product(*choices):
        chosen=[x for x in combination if x]
        if any(a['unit_id']==b['unit_id'] and a['loading_start']<b['released_at'] and b['loading_start']<a['released_at'] for a,b in itertools.combinations(chosen,2)): continue
        due={x['cargo_id']:dt(x['due_date']) for x in s['cargo']}
        ontime=sum(dt(t['unloading_end'])<=due[t['cargo_id']] for t in chosen)
        score=(ontime,len(chosen),-len(chosen))
        if score>best: best=score
    return (round(best[0]*.001,3),round(best[1]*.001,3),best[2])

def exact(s):
    return exact_objective(s)[0]

def main():
    rows=[]
    for name in ('two_units_three_cargo','draft','different_availability','deadlines'):
        s=setup(name); objective=exact_objective(s); optimum=objective[0]; result=simulate(s,'OPTIMIZED'); val=result['metrics']['delivered_on_time_t']
        rows.append(dict(scenario=name,exact_on_time_t=optimum,heuristic_on_time_t=val,exact_total_t=objective[1],heuristic_total_t=result['metrics']['delivered_total_t'],exact_trips=-objective[2],heuristic_trips=result['metrics']['completed_trips'],gap_t=round(optimum-val,3),gap_percent=round(100*(optimum-val)/optimum,3) if optimum else 0))
        print(rows[-1],flush=True)
    write_csv(ROOT/'results/tiny_exact_results.csv',rows)
if __name__=='__main__': main()
