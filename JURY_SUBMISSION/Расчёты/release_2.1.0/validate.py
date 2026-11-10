"""Independent schedule audit and meaningful edge cases. No external dependencies."""
import argparse
import copy
import hashlib
import json
import tempfile
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
from generate import BASE, STATUSES, dt, read_csv, generate, load_scenario, simulate, make_conditions

def require(ok, message):
    if not ok: raise AssertionError(message)

def audit(c,cargo,units,conditions,trips):
    cc={x['cargo_id']:x for x in cargo}; uu={x['unit_id']:x for x in units}
    ss={s['segment_id']:s for s in c['segments']}
    cm={(x['date'],x['segment_id']):x for x in conditions}
    require(len(cc)==len(cargo),'Duplicate cargo IDs')
    require(len(uu)==len(units),'Duplicate unit IDs')
    require(len(cm)==len(conditions),'Duplicate condition keys')
    for x in cargo:
        require(float(x['volume_t'])>=0,'Negative demand')
        require(dt(x['ready_date'])<=dt(x['due_date']),'Due date precedes readiness')
        require(dt(c['start'])<=dt(x['ready_date'])<=dt(c['end']),'Readiness outside scenario window')
    for x in conditions:
        require(x['segment_id'] in ss,'Condition FK')
        require(float(x['usable_depth_m'])>=0 and float(x['max_allowed_draft_m'])>=0,'Negative depth')
        require(int(x['is_closed']) in [0,1],'Invalid closed flag')
    moved=defaultdict(float); intervals=defaultdict(list); busy=0; ontime=0; weighted_days=0
    require(len({t['trip_id'] for t in trips})==len(trips),'Duplicate trip IDs')
    for t in trips:
        require(t['cargo_id'] in cc and t['unit_id'] in uu,'Trip FK')
        x,u=cc[t['cargo_id']],uu[t['unit_id']]; q=float(t['volume_t'])
        require(0<q<=u['capacity_t']+1e-6,'Payload capacity')
        require(t['cargo_type']==x['cargo_type'] and t['cargo_type'] in u['cargo_type_allowed'],'Cargo compatibility')
        require(t['origin']==x['origin']==c['origin'] and t['destination']==x['destination']==c['destination'],'Cargo endpoint mismatch')
        require(t['tug_id']==u['tug_id'] and t['barge_id']==u['barge_id'],'Missing/wrong tug')
        require(t['tug_id']!=t['barge_id'],'Barge cannot tug itself')
        times=[dt(t[k]) for k in ['loading_start','departure','arrival','unloading_end','released_at']]
        require(times==sorted(times),'Negative trip duration')
        require(times[0]>=dt(x['ready_date']),'Loaded before ready')
        require(times[0]>=dt(c['start']) and times[-1]<=dt(c['end'])+timedelta(days=1),'Navigation window')
        require(times[0]>=dt(u['available_from']) and times[-1]<=dt(u['available_to']),'Availability violation')
        h=c['handling_fixed_hours']+q/u['handling_tph']
        require(abs((times[1]-times[0]).total_seconds()/3600-h)<0.001,'Loading duration')
        require(abs((times[3]-times[2]).total_seconds()/3600-h)<0.001,'Unloading duration')
        draft=u['draft_empty_m']+(u['draft_full_m']-u['draft_empty_m'])*q/u.get('reference_capacity_t',u['capacity_t'])
        require(abs(float(t['loaded_draft_m'])-draft)<1e-5,'Draft-load curve')
        events=json.loads(t['events_json']); ids=list(ss)
        require([(e['direction'],e['segment_id']) for e in events]==[('upstream',i) for i in ids]+[('downstream',i) for i in reversed(ids)],'Wrong segment sequence')
        previous=times[1]
        for i,e in enumerate(events):
            if i==len(ids):
                require(previous==times[2],'Arrival timestamp'); previous=times[3]
            a,b=dt(e['start']),dt(e['end']); s=ss[e['segment_id']]
            require(a==previous and b>a,'Segment chronology'); previous=b
            speed=u['speed_upstream_kmh'] if e['direction']=='upstream' else u['speed_downstream_kmh']
            require(abs((b-a).total_seconds()/3600-s['distance_km']/speed)<0.001,'Distance/speed/time mismatch')
            used=max(u['tug_draft_m'],draft if e['direction']=='upstream' else u['draft_empty_m'])
            day=datetime.combine(a.date(),datetime.min.time())
            while day<b:
                key=(day.date().isoformat(),e['segment_id']); require(key in cm,'Missing daily condition')
                con=cm[key]; require(not int(con['is_closed']),'Closed segment passage')
                require(used<=float(con['max_allowed_draft_m'])+1e-6,'Draft restriction')
                require(used+c['underkeel_m']<=float(con['usable_depth_m'])+1e-6,'Underkeel clearance')
                day+=timedelta(days=1)
        require(previous==times[-1],'Release time not return time')
        for resource in [u['tug_id'],u['barge_id']]: intervals[resource].append((times[0],times[-1]))
        moved[x['cargo_id']]+=q; busy+=(times[-1]-times[0]).total_seconds()/3600
        ontime+=q if times[3]<=dt(x['due_date']) else 0
        weighted_days+=q*(times[3]-dt(x['ready_date'])).total_seconds()/86400
    for resource,rows in intervals.items():
        ordered=sorted(rows)
        require(all(a[1]<=b[0] for a,b in zip(ordered,ordered[1:])),f'Overlapping resource {resource}')
    require(all(q<=float(cc[k]['volume_t'])+1e-5 for k,q in moved.items()),'Cargo mass counted twice')
    total=sum(float(x['volume_t']) for x in cargo); delivered=sum(moved.values())
    require(total-delivered>=-1e-5,'Negative undelivered')
    return delivered,ontime,busy,weighted_days

def edge_tests(c,cargo,units,conditions):
    # Use tiny complete demand with a long open, deep window to make expected results unambiguous.
    tiny=[copy.deepcopy(cargo[0])]; tiny[0].update(volume_t=10,ready_date=c['start'],due_date=c['end'])
    deep=copy.deepcopy(conditions)
    for x in deep: x.update(usable_depth_m=5,max_allowed_draft_m=4.85,is_closed=0)
    for strategy in ['baseline','optimized_heuristic']:
        require(not simulate(c,[],units,deep,strategy)[0],'Zero demand')
        require(not simulate(c,tiny,[],deep,strategy)[0],'Zero fleet')
        closed=copy.deepcopy(deep)
        for x in closed: x['is_closed']=1
        require(not simulate(c,tiny,units,closed,strategy)[0],'Closed route')
        tr,m=simulate(c,tiny,units,deep,strategy); audit(c,tiny,units,deep,tr)
        require(abs(m['total_delivered_t']-10)<1e-6,'Excess fleet must deliver all tiny demand')
        heavy=copy.deepcopy(tiny); heavy[0]['volume_t']=1e7
        tr,m=simulate(c,heavy,units,deep,strategy); audit(c,heavy,units,deep,tr)
        require(m['undelivered_t']>0,'Demand above throughput')
    tr,_=simulate(c,tiny,units,deep,'baseline')
    for kind in ['mass','overlap','draft','tug']:
        bad=copy.deepcopy(tr)
        if kind=='mass': bad[0]['volume_t']=1e6
        elif kind=='overlap': bad.append(dict(bad[0],trip_id='BAD_DUPLICATE'))
        elif kind=='draft': bad[0]['loaded_draft_m']=99
        else: bad[0]['tug_id']='NONEXISTENT'
        try: audit(c,tiny,units,deep,bad)
        except AssertionError: pass
        else: raise AssertionError(f'Audit failed to reject {kind}')
    return ['нулевой спрос','нет флота','закрытый маршрут','избыточный флот','спрос выше провозной способности','отклонение испорченных рейсов']

def validate(config,out,reproduce=True):
    c=json.loads(Path(config).read_text(encoding='utf-8')); out=Path(out); counts=0
    actual=read_csv(out/'actuals.csv')
    require({r['checkpoint']:float(r['tonnage_t']) for r in actual}==c['checkpoints'],'Actual anchors changed')
    require(all(r['area_id']==c['area_id'] and r['as_of_date']==c['as_of_date'] for r in actual),'Anchor scope/date')
    a=c['checkpoints']; require(a['planned']>=a['shipped']>=a['crossed_bar']>=a['delivered'],'Nested-chain assumption infeasible')
    for rep in range(c['replicates']):
        rows=read_csv(out/f'calibration_stage_allocations_r{rep+1}.csv'); sums=defaultdict(float)
        for r in rows: sums[r['stage']]+=float(r['volume_t'])
        expected={'not_shipped':a['planned']-a['shipped'],'before_bar':a['shipped']-a['crossed_bar'],'after_bar':a['crossed_bar']-a['delivered'],'delivered':a['delivered']}
        require(all(abs(sums[k]-v)<1e-6 for k,v in expected.items()),'Aggregate allocation balance')
    metrics={(r['scenario_id'],r['strategy']):r for r in read_csv(out/'comparison.csv')}
    for folder in sorted((out/'scenarios').iterdir()):
        cargo,units,conditions=load_scenario(folder)
        vessels=read_csv(folder/'vessels.csv'); vessel_ids={v['vessel_id'] for v in vessels}
        require(len(vessel_ids)==len(vessels),'Duplicate vessel IDs')
        require(all(u['barge_id'] in vessel_ids and u['tug_id'] in vessel_ids for u in units),'Vessel/unit FK')
        for group in [cargo,units,conditions,vessels]:
            require(all(x['data_status'] in STATUSES for x in group),'Unknown data status')
        scale=1.3 if '_demand_plus30_' in folder.name else 1
        for p in c['plans']:
            require(abs(sum(float(x['volume_t']) for x in cargo if x['plan_id']==p['plan_id'])-p['volume_t']*scale)<0.001,'Plan batch split')
        for strategy in ['baseline','optimized_heuristic']:
            trips=read_csv(folder/f'trips_{strategy}.csv')
            delivered,ontime,busy,weighted=audit(c,cargo,units,conditions,trips)
            m=metrics[(folder.name,strategy)]
            require(abs(delivered-float(m['total_delivered_t']))<0.001,'Delivered metric')
            require(abs(ontime-float(m['delivered_on_time_t']))<0.001,'On-time metric')
            require(0<=float(m['fleet_utilization'])<=1,'Utilization bounds')
            counts+=len(trips)
    example=out/'scenarios'/'r1_baseline_central_central'; cargo,units,conditions=load_scenario(example)
    edges=edge_tests(c,cargo,units,conditions)
    manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    for name,h in manifest['csv_sha256'].items(): require(hashlib.sha256((out/name).read_bytes()).hexdigest()==h,f'File hash changed: {name}')
    if reproduce:
        with tempfile.TemporaryDirectory(prefix='.repro_',dir=BASE) as temp:
            require(Path(temp).resolve().parent==BASE.resolve(),'Unsafe temporary directory')
            generate(config,Path(temp))
            other=json.loads((Path(temp)/'manifest.json').read_text(encoding='utf-8'))
            require(other==manifest,'Not reproducible at fixed seed')
    report=f'''# Проверка данных\n\nPASS. Проверено расписаний: {len(metrics)}. Рейсов: {counts}.\n\n- Массы, даты, ссылки между CSV и порядок событий проверены.\n- Осадка буксира и баржи проверены на каждой затронутой календарной дате каждого участка в обоих направлениях.\n- Загрузка, обработка, возвращение до закрытия и исключительность ресурсов проверены.\n- Массы синтетических партий совпадают с выбранными сценарными планами.\n- Три условные декомпозиции общеречных агрегатов совпадают точно; это НЕ проверка реального расписания.\n- Проверена воспроизводимость всех CSV: {reproduce}.\n- Крайние случаи: {', '.join(edges)}.\n\n## Ограничение калибровки\n\nТранспортная модель пилотного плеча НЕ откалибрована по 89 030 т всей Яны. Нет сопоставимых фактических рейсов, заявок и численности флота пилота. Общеречная декомпозиция только арифметическая и условна относительно гипотезы вложенной цепочки.\n\n## Предел проверки допустимости\n\nPASS означает допустимость внутри объявленной демонстрационной модели, а не разрешение реальных перевозок. Кривые осадки, скорости, совместимость буксиров, характеристики портов и глубины сценарные. Класс судна, буксировочная тяга, ледовые и погодные условия, узкие местные проходы и разрешения не сертифицированы.\n'''
    (BASE/'validation_report.md').write_text(report,encoding='utf-8')
    print(report.split('\n')[2])

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--config',type=Path,default=BASE/'scenario_config.json'); p.add_argument('--out',type=Path,default=BASE/'data'); p.add_argument('--skip-reproducibility',action='store_true')
    args=p.parse_args(); validate(args.config,args.out,not args.skip_reproducibility)
