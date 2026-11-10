"""Reproducible CSV scenarios. Python 3.10+, standard library only."""
import argparse
import copy
import csv
import hashlib
import json
import math
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATUSES = {'OBSERVED','NORMATIVE','HISTORICAL','SYNTHETIC','ASSUMED','UNKNOWN'}
CARGO_FIELDS=['cargo_id','cargo_type','origin','destination','volume_t','ready_date','due_date','priority','source_url','data_status','plan_id','origin_status','dates_status','plan_verification']
VESSEL_FIELDS=['vessel_id','project','vessel_type','cargo_type_allowed','capacity_t','draft_empty_m','draft_full_m','speed_upstream_kmh','speed_downstream_kmh','available_from','available_to','source_url','data_status','unit_id','compatibility_status']
UNIT_FIELDS=['unit_id','barge_id','tug_id','project','capacity_t','cargo_type_allowed','draft_empty_m','draft_full_m','tug_draft_m','speed_upstream_kmh','speed_downstream_kmh','handling_tph','reference_capacity_t','available_from','available_to','data_status']

def dt(s):
    return datetime.fromisoformat(s)

def iso(t):
    return t.isoformat(timespec='seconds')

def write_csv(path, rows, fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or list(rows[0])
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def days(a,b):
    while a <= b:
        yield a
        a += timedelta(days=1)

def depth_at(c, date, offset):
    p = [(dt(d),v) for d,v in c['depth_profile']]
    if date <= p[0][0]: return p[0][1]+offset
    for (a,x),(b,y) in zip(p,p[1:]):
        if date <= b:
            return round(x+(y-x)*(date-a).total_seconds()/(b-a).total_seconds()+offset, 6)
    return p[-1][1]+offset

def make_conditions(c, close, offset):
    rows=[]
    for day in days(dt(c['start']),dt(c['end'])+timedelta(days=1)):
        for seg in c['segments']:
            depth=depth_at(c,day,offset)
            rows.append(dict(date=day.date().isoformat(),segment_id=seg['segment_id'],
                usable_depth_m=depth,max_allowed_draft_m=round(max(0,depth-c['underkeel_m']),6),
                is_closed=int(day>=close),restriction_reason='Scenario closure' if day>=close else 'Scenario depth; not measured hydrology',
                source_url='',data_status='ASSUMED'))
    return rows

def make_cargo(c, seed, scale, timing):
    rng=random.Random(seed)
    start,end=dt(c['start']),dt(c['end'])
    span=(end-start).days
    rows=[]
    for p in c['plans']:
        remaining=round(p['volume_t']*1000)
        while remaining:
            qty=min(remaining,rng.randint(40,c['batch_max_t'])*1000)
            remaining-=qty
            frac=rng.uniform(*c['ready_day_fraction_range'])
            ready=max(start,min(end,start+timedelta(days=round(span*frac)+timing)))
            due=min(end+timedelta(days=1),ready+timedelta(days=rng.randint(*c['due_days_after_ready'])))
            rows.append(dict(cargo_id=f'C{len(rows)+1:05}',cargo_type=p['cargo_type'],origin=c['origin'],
                destination=c['destination'],volume_t=qty/1000,ready_date=iso(ready),due_date=iso(due),
                priority=3 if 'food' in p['cargo_type'] else 1,source_url=p['source_url'],data_status='SYNTHETIC',
                plan_id=p['plan_id'],origin_status='ASSUMED',dates_status='SYNTHETIC',plan_verification=c['plan_verification']))
    for p in c['plans']:
        selected=[x for x in rows if x['plan_id']==p['plan_id']]
        for x in selected: x['volume_t']=round(x['volume_t']*scale,3)
        if selected:
            selected[-1]['volume_t']=round(selected[-1]['volume_t']+p['volume_t']*scale-sum(x['volume_t'] for x in selected),3)
    return rows

def check_passage(event, draft, condition_map, underkeel):
    a,b=dt(event['start']),dt(event['end'])
    day=datetime.combine(a.date(),datetime.min.time())
    while day < b:
        row=condition_map.get((day.date().isoformat(),event['segment_id']))
        if row is None: raise ValueError(f"Missing condition for date {day.date()} / segment {event['segment_id']}; missing is not closure")
        if int(row['is_closed']): return False
        if draft > float(row['max_allowed_draft_m'])+1e-6: return False
        if draft+underkeel > float(row['usable_depth_m'])+1e-6: return False
        day += timedelta(days=1)
    return True

def trial_trip(c, unit, cargo, qty, start, condition_map, close):
    handling=c['handling_fixed_hours']+qty/unit['handling_tph']
    departure=start+timedelta(hours=handling)
    now=departure
    events=[]
    for s in c['segments']:
        end=now+timedelta(hours=s['distance_km']/unit['speed_upstream_kmh'])
        events.append(dict(direction='upstream',segment_id=s['segment_id'],start=iso(now),end=iso(end)))
        now=end
    arrival=now
    unloaded=arrival+timedelta(hours=handling)
    now=unloaded
    for s in reversed(c['segments']):
        end=now+timedelta(hours=s['distance_km']/unit['speed_downstream_kmh'])
        events.append(dict(direction='downstream',segment_id=s['segment_id'],start=iso(now),end=iso(end)))
        now=end
    if now>close or now>dt(unit['available_to']) or start<dt(unit['available_from']): return None
    draft=unit['draft_empty_m']+(unit['draft_full_m']-unit['draft_empty_m'])*qty/unit.get('reference_capacity_t',unit['capacity_t'])
    for e in events:
        used=max(unit['tug_draft_m'],draft if e['direction']=='upstream' else unit['draft_empty_m'])
        if not check_passage(e,used,condition_map,c['underkeel_m']): return None
    return dict(unit_id=unit['unit_id'],barge_id=unit['barge_id'],tug_id=unit['tug_id'],cargo_id=cargo['cargo_id'],
        cargo_type=cargo['cargo_type'],origin=cargo['origin'],destination=cargo['destination'],volume_t=round(qty,3),
        loading_start=iso(start),departure=iso(departure),arrival=iso(arrival),unloading_end=iso(unloaded),
        released_at=iso(now),loaded_draft_m=round(draft,6),events_json=json.dumps(events,ensure_ascii=False,separators=(',',':')),
        data_status='SYNTHETIC',source_url='')

def best_load(c, unit, cargo, qty, start, condition_map, close):
    """Largest feasible load on the 0.001 t grid, including non-monotone days.

    Passage timestamps are affine in load and draft is linear. Feasibility can
    change only at a calendar boundary, a draft bound, or a closing time. Test
    grid points around each boundary; check_passage remains the authority.
    """
    max_tick = math.floor(qty * 1000 + 1e-8)
    if max_tick < 1:
        return None
    first = trial_trip(c, unit, cargo, max_tick / 1000, start, condition_map, close)
    if first:
        return first
    ticks = {1, max_tick}
    def boundary(q):
        if math.isfinite(q):
            n = math.floor(q * 1000)
            for v in range(n - 2, n + 4):
                if 1 <= v <= max_tick:
                    ticks.add(v)
    speed_up = unit['speed_upstream_kmh']
    speed_down = unit['speed_downstream_kmh']
    handling = c['handling_fixed_hours']
    tph = unit['handling_tph']
    now = start + timedelta(hours=handling)
    legs = []
    for seg in c['segments']:
        end = now + timedelta(hours=seg['distance_km'] / speed_up)
        legs.append((seg, now, end, 1 / tph, True))
        now = end
    now += timedelta(hours=handling)
    for seg in reversed(c['segments']):
        end = now + timedelta(hours=seg['distance_km'] / speed_down)
        legs.append((seg, now, end, 2 / tph, False))
        now = end
    for limit in (close, dt(unit['available_to'])):
        boundary((limit - now).total_seconds() / 3600 * tph / 2)
    slope = (unit['draft_full_m'] - unit['draft_empty_m']) / unit.get('reference_capacity_t', unit['capacity_t'])
    for seg, a, b, hours_per_t, loaded in legs:
        last = b + timedelta(hours=hours_per_t * max_tick / 1000)
        day = datetime.combine(a.date(), datetime.min.time())
        while day <= last:
            for stamp in (a, b):
                boundary((day - stamp).total_seconds() / 3600 / hours_per_t)
                boundary((day + timedelta(seconds=1) - stamp).total_seconds() / 3600 / hours_per_t)
            row = condition_map.get((day.date().isoformat(), seg['segment_id']))
            if row and loaded and slope:
                allowed = min(float(row['max_allowed_draft_m']), float(row['usable_depth_m']) - c['underkeel_m'])
                boundary((allowed + 1e-6 - unit['draft_empty_m']) / slope)
            day += timedelta(days=1)
    for tick in sorted(ticks, reverse=True):
        trip = trial_trip(c, unit, cargo, tick / 1000, start, condition_map, close)
        if trip:
            return trip
    return None

def simulate(c, cargo, units, conditions, strategy):
    from scenario_validation import validate_inputs
    validate_inputs(c, cargo, units, conditions)
    start=dt(c['start']); close=dt(c['end'])+timedelta(days=1)
    # Barge and tug are independent exclusive resources, even across pairs.
    ready={r:start for u in units for r in (u['barge_id'],u['tug_id'])}
    remain={x['cargo_id']:float(x['volume_t']) for x in cargo}
    cm={(x['date'],x['segment_id']):x for x in conditions}
    trips=[]
    for day in days(start,dt(c['end'])):
        for u in units:
            resource_ready=max(ready[u['barge_id']],ready[u['tug_id']],dt(u['available_from']))
            if resource_ready>=day+timedelta(days=1): continue
            candidates=[x for x in cargo if dt(x['ready_date'])<=day and remain[x['cargo_id']]>0.0001
                and x['cargo_type'] in u['cargo_type_allowed'] and x['origin']==c['origin'] and x['destination']==c['destination']]
            if strategy=='baseline': candidates.sort(key=lambda x:(x['ready_date'],x['cargo_id']))
            else: candidates.sort(key=lambda x:(x['due_date'],-int(x['priority']),x['ready_date'],x['cargo_id']))
            for x in candidates:
                begin=max(day,resource_ready,dt(x['ready_date']))
                if begin>=day+timedelta(days=1): break
                qty=min(remain[x['cargo_id']],u['capacity_t'])
                trip=best_load(c,u,x,qty,begin,cm,close)
                if trip:
                    trip['trip_id']=f'TR{len(trips)+1:05}'
                    trips.append(trip); remain[x['cargo_id']]=round(remain[x['cargo_id']]-trip['volume_t'],3)
                    ready[u['barge_id']]=dt(trip['released_at'])
                    ready[u['tug_id']]=dt(trip['released_at'])
                    break
    total=sum(float(x['volume_t']) for x in cargo)
    delivered=sum(x['volume_t'] for x in trips)
    by_id={x['cargo_id']:x for x in cargo}
    on_time=sum(x['volume_t'] for x in trips if dt(x['unloading_end'])<=dt(by_id[x['cargo_id']]['due_date']))
    windows={}
    for u in units:
        for resource in (u['barge_id'],u['tug_id']):
            windows.setdefault(resource,[]).append((max(start,dt(u['available_from'])),min(close,dt(u['available_to']))))
    available=0
    for intervals in windows.values():
        end=None
        for a,b in sorted(intervals):
            if b<=a: continue
            if end is None or a>end: available+=(b-a).total_seconds()/3600; end=b
            elif b>end: available+=(b-end).total_seconds()/3600; end=b
    busy=2*sum((dt(t['released_at'])-dt(t['loading_start'])).total_seconds()/3600 for t in trips)
    avg=sum(t['volume_t']*(dt(t['unloading_end'])-dt(by_id[t['cargo_id']]['ready_date'])).total_seconds()/86400 for t in trips)/delivered if delivered else 0
    return trips,dict(strategy=strategy,delivered_on_time_t=round(on_time,3),total_delivered_t=round(delivered,3),
        undelivered_t=round(total-delivered,3),completed_trips=len(trips),average_delivery_days=round(avg,6),
        vessel_idle_days=round((available-busy)/24,6),fleet_utilization=round(busy/available,6) if available else 0)

TRIP_FIELDS=['unit_id','barge_id','tug_id','cargo_id','cargo_type','origin','destination','volume_t','loading_start','departure','arrival','unloading_end','released_at','loaded_draft_m','events_json','data_status','source_url','trip_id']

def aggregate_allocations(c,seed):
    """Mass reconciliation only: NO locations, ships, voyage history or route mapping."""
    if not c['nested_chain_assumption']: return []
    a=c['checkpoints']; rng=random.Random(seed)
    targets={'not_shipped':a['planned']-a['shipped'],'before_bar':a['shipped']-a['crossed_bar'],
             'after_bar':a['crossed_bar']-a['delivered'],'delivered':a['delivered']}
    rows=[]
    for stage,target in targets.items():
        remaining=round(target*1000)
        while remaining:
            q=min(remaining,rng.randint(100,700)*1000); remaining-=q
            rows.append(dict(allocation_id=f'A{len(rows)+1:05}',area_id=c['area_id'],as_of_date=c['as_of_date'],
                stage=stage,volume_t=q/1000,ready_day_offset=rng.randint(0,100),data_status='SYNTHETIC',
                note='Hypothetical nested chain allocation; not physical trips or known cargo readiness'))
    return rows

def generate(config_path=BASE/'scenario_config.json', out=BASE/'data/generated'):
    c=json.loads(Path(config_path).read_text(encoding='utf-8')); out=Path(out); out.mkdir(parents=True,exist_ok=True)
    if not c['segments'] or len({s['segment_id'] for s in c['segments']})!=len(c['segments']):
        raise ValueError('Segments must form a nonempty chain with unique IDs')
    if c['segments'][0]['from_node']!=c['origin'] or c['segments'][-1]['to_node']!=c['destination']:
        raise ValueError('Route endpoints do not match origin/destination')
    if any(a['to_node']!=b['from_node'] for a,b in zip(c['segments'],c['segments'][1:])):
        raise ValueError('This minimal simulator supports an ordered chain, not branching routes')
    if any(s['distance_km']<=0 for s in c['segments']) or dt(c['end'])<dt(c['start']):
        raise ValueError('Invalid distance or navigation window')
    routes=[]
    for s in c['segments']:
        routes.append(dict(segment_id=s['segment_id'],from_node=s['from_node'],to_node=s['to_node'],distance_km=s['distance_km'],
            navigation_start=c['start'],navigation_end=c['end'],guaranteed_depth_m=s['guaranteed_depth_m'],
            source_url=c['route_source_url'],data_status='NORMATIVE',channel_width_m=s['width_m'],turn_radius_m=s['turn_radius_m'],
            guaranteed_after_dredging_m=s['guaranteed_after_dredging_m'],note='1.7 m conditional after dredging from Sep 1; not measured daily depth',pdf_page='75;92',pdf_row=s['norm_pdf_row']))
    write_csv(out/'routes.csv',routes)
    write_csv(out/'actuals.csv',[dict(as_of_date=c['as_of_date'],area_id=c['area_id'],checkpoint=k,tonnage_t=v,
        source_url=c['aggregate_source_url'],data_status='NORMATIVE' if k=='planned' else 'OBSERVED') for k,v in c['checkpoints'].items()])
    write_csv(out/'cargo_plans.csv',[dict(**p,origin='',destination=c['destination'],data_status='NORMATIVE',verification=c['plan_verification']) for p in c['plans']])
    write_csv(out/'vessel_projects.csv',[dict(**p,source_url='https://lorp.ru/fleet/',data_status='HISTORICAL',draft_empty_m='',draft_full_m='',confirmed_2026_count='',note='Catalogue characteristic; no 2026 pilot assignment') for p in c['reference_projects']])
    for reference in sorted((BASE/'data/reference').glob('*.csv')):
        shutil.copyfile(reference,out/reference.name)
    # Freshly constructed scenario records; catalogue deadweight is never substituted for load limit.
    comparison=[]; manifests=[]
    for rep in range(c['replicates']):
      for scenario,change in c['scenarios'].items():
        for depth_name,offset in c['depth_variants'].items():
          for timing_name,timing in c['timing_variants'].items():
            name=f'r{rep+1}_{scenario}_{depth_name}_{timing_name}'; folder=out/'scenarios'/name
            close=dt(c['end'])+timedelta(days=1-int(change.get('close_early_days',0)))
            units=copy.deepcopy(c['units'])
            for u in units:
                u['reference_capacity_t']=u['capacity_t']
                u['capacity_t']*=change.get('capacity_scale',1)
                u['available_from']=c['start']; u['available_to']=iso(close)
            cargo=make_cargo(c,c['seed']+rep,change.get('demand_scale',1),timing)
            conditions=make_conditions(c,close,offset)
            vs=[]
            for u in units:
                for role,vid in [('barge',u['barge_id']),('tug',u['tug_id'])]:
                    vs.append(dict(vessel_id=vid,project=u['project'] if role=='barge' else '',vessel_type=role,
                        cargo_type_allowed='|'.join(u['cargo_type_allowed']) if role=='barge' else '',
                        capacity_t=u['capacity_t'] if role=='barge' else 0,draft_empty_m=u['draft_empty_m'] if role=='barge' else u['tug_draft_m'],
                        draft_full_m=u['draft_full_m'] if role=='barge' else u['tug_draft_m'],speed_upstream_kmh=u['speed_upstream_kmh'],
                        speed_downstream_kmh=u['speed_downstream_kmh'],available_from=u['available_from'],available_to=u['available_to'],
                        source_url='',data_status='ASSUMED',unit_id=u['unit_id'],compatibility_status='ASSUMED_NOT_CERTIFIED'))
            write_csv(folder/'cargo.csv',cargo,CARGO_FIELDS)
            write_csv(folder/'vessels.csv',vs,VESSEL_FIELDS)
            write_csv(folder/'conditions.csv',conditions)
            write_csv(folder/'units.csv',[{**u,'cargo_type_allowed':'|'.join(u['cargo_type_allowed']),'data_status':'ASSUMED'} for u in units],UNIT_FIELDS)
            for strategy in ['baseline','optimized_heuristic']:
                trips,metrics=simulate(c,cargo,units,conditions,strategy)
                write_csv(folder/f'trips_{strategy}.csv',trips,TRIP_FIELDS)
                comparison.append(dict(scenario_id=name,replicate=rep+1,external_seed=c['seed']+rep,scenario=scenario,
                    depth_variant=depth_name,timing_variant=timing_name,**metrics,data_status='SYNTHETIC'))
                if name=='r1_baseline_central_central' and strategy=='baseline':
                    write_csv(out/'trips_synthetic.csv',trips,TRIP_FIELDS)
            if name=='r1_baseline_central_central':
                write_csv(out/'cargo.csv',cargo,CARGO_FIELDS); write_csv(out/'vessels.csv',vs,VESSEL_FIELDS); write_csv(out/'conditions.csv',conditions)
            manifests.append(dict(scenario_id=name,capacity_scale=change.get('capacity_scale',1),demand_scale=change.get('demand_scale',1),close=iso(close)))
      allocations=aggregate_allocations(c,c['seed']+rep)
      write_csv(out/f'calibration_stage_allocations_r{rep+1}.csv',allocations,
        ['allocation_id','area_id','as_of_date','stage','volume_t','ready_day_offset','data_status','note'])
    write_csv(out/'comparison.csv',comparison)
    write_csv(out/'scenario_manifest.csv',manifests)
    hashes={str(p.relative_to(out)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.rglob('*.csv'))}
    (out/'manifest.json').write_text(json.dumps(dict(seed=c['seed'],config_sha256=hashlib.sha256(Path(config_path).read_bytes()).hexdigest(),csv_sha256=hashes),ensure_ascii=False,indent=2),encoding='utf-8')
    return c

def load_scenario(folder):
    folder=Path(folder)
    units=read_csv(folder/'units.csv')
    for u in units:
        u['cargo_type_allowed']=u['cargo_type_allowed'].split('|')
        for key in ['capacity_t','reference_capacity_t','draft_empty_m','draft_full_m','tug_draft_m','speed_upstream_kmh','speed_downstream_kmh','handling_tph']:
            u[key]=float(u[key])
    return read_csv(folder/'cargo.csv'),units,read_csv(folder/'conditions.csv')

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--config',type=Path,default=BASE/'scenario_config.json'); p.add_argument('--out',type=Path,default=BASE/'data/generated')
    a=p.parse_args(); generate(a.config,a.out); print(f'Generated: {a.out}')
