"""Validate scenario inputs before optimization; no scheduling decisions here."""
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

class ScenarioValidationError(ValueError):
    """Input is incomplete, inconsistent, or outside the supported model."""


def fail(message):
    raise ScenarioValidationError(message)


def date(value, label):
    try:
        result = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        fail(f'{label}: invalid ISO date/time: {value!r}')
    if result.tzinfo is not None:
        fail(f'{label}: timezone offsets are not supported; use the same naive time basis')
    return result


def number(value, label, *, positive=False, nonnegative=False):
    try:
        result=float(value)
    except (TypeError,ValueError):
        fail(f'{label}: expected a finite number, got {value!r}')
    if not math.isfinite(result): fail(f'{label}: expected a finite number, got {value!r}')
    if positive and result<=0: fail(f'{label}: must be positive')
    if nonnegative and result<0: fail(f'{label}: must be nonnegative')
    return result


def ident(value,label):
    if not isinstance(value,str) or not value.strip() or value!=value.strip():
        fail(f'{label}: identifier must be a nonempty string without surrounding whitespace')
    return value


def rows(path):
    path=Path(path)
    if not path.is_file(): fail(f'Missing input file: {path}')
    try:
        with path.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f)
            if not reader.fieldnames: fail(f'{path}: missing CSV header')
            if len(set(reader.fieldnames))!=len(reader.fieldnames): fail(f'{path}: duplicate CSV column')
            return list(reader)
    except (OSError,UnicodeError,csv.Error) as e:
        fail(f'Cannot read {path}: {e}')


def validate_inputs(c,cargo,units,conditions,expected_by_type=None,expected_by_plan=None):
    try:
        start=date(c['start'],'navigation start'); end=date(c['end'],'navigation end')
        if start.time()!=datetime.min.time() or end.time()!=datetime.min.time(): fail('navigation start/end must be calendar dates at 00:00')
        if end<start: fail('navigation end precedes navigation start')
        origin=ident(c['origin'],'origin'); destination=ident(c['destination'],'destination')
        number(c['underkeel_m'],'underkeel_m',nonnegative=True)
        number(c['handling_fixed_hours'],'handling_fixed_hours',nonnegative=True)
        segs=c['segments']
        if not isinstance(segs,list) or not segs: fail('segments: expected a nonempty route')
        segment_ids=[]
        for i,seg in enumerate(segs):
            label=f'segments[{i}]'; segment_ids.append(ident(seg['segment_id'],label+'.segment_id'))
            ident(seg['from_node'],label+'.from_node'); ident(seg['to_node'],label+'.to_node')
            number(seg['distance_km'],label+'.distance_km',positive=True)
            if i and segs[i-1]['to_node']!=seg['from_node']: fail(f'{label}: disconnected route')
        if len(set(segment_ids))!=len(segment_ids): fail('segments: duplicate segment_id')
        if segs[0]['from_node']!=origin or segs[-1]['to_node']!=destination: fail('route endpoints do not match origin/destination')
        plans={}
        for p in c.get('plans',[]):
            pid=ident(p['plan_id'],'plans.plan_id'); typ=ident(p['cargo_type'],f'plan {pid}.cargo_type')
            number(p['volume_t'],f'plan {pid}.volume_t',nonnegative=True)
            if pid in plans: fail(f'duplicate plan_id: {pid}')
            plans[pid]=typ
        seen=set(); bytype=defaultdict(float); byplan=defaultdict(float)
        for i,x in enumerate(cargo):
            label=f'cargo[{i}]'; cid=ident(x['cargo_id'],label+'.cargo_id')
            if cid in seen: fail(f'duplicate cargo_id: {cid}')
            seen.add(cid); typ=ident(x['cargo_type'],f'cargo {cid}.cargo_type')
            amount=number(x['volume_t'],f'cargo {cid}.volume_t',nonnegative=True)
            if x['origin']!=origin or x['destination']!=destination: fail(f'cargo {cid}: endpoint mismatch')
            ready=date(x['ready_date'],f'cargo {cid}.ready_date'); due=date(x['due_date'],f'cargo {cid}.due_date')
            if ready<start or ready>end: fail(f'cargo {cid}: readiness outside navigation window')
            if due<ready: fail(f'cargo {cid}: due date precedes readiness')
            priority=number(x['priority'],f'cargo {cid}.priority',nonnegative=True)
            if not priority.is_integer(): fail(f'cargo {cid}.priority: must be an integer')
            pid=x.get('plan_id','')
            if pid:
                ident(pid,f'cargo {cid}.plan_id')
                if plans and pid not in plans: fail(f'cargo {cid}: unknown plan_id {pid}')
                if pid in plans and plans[pid]!=typ: fail(f'cargo {cid}: plan/category mismatch for {pid}')
                byplan[pid]+=amount
            bytype[typ]+=amount
        uids=set(); barges=set(); tugs=set()
        for i,u in enumerate(units):
            label=f'units[{i}]'; uid=ident(u['unit_id'],label+'.unit_id')
            if uid in uids: fail(f'duplicate unit_id: {uid}')
            uids.add(uid); barge=ident(u['barge_id'],f'unit {uid}.barge_id'); tug=ident(u['tug_id'],f'unit {uid}.tug_id')
            if barge==tug: fail(f'unit {uid}: barge and tug must differ')
            barges.add(barge); tugs.add(tug)
            for key in ('capacity_t','reference_capacity_t','speed_upstream_kmh','speed_downstream_kmh','handling_tph'):
                number(u[key],f'unit {uid}.{key}',positive=True)
            for key in ('draft_empty_m','draft_full_m','tug_draft_m'):
                number(u[key],f'unit {uid}.{key}',nonnegative=True)
            if float(u['draft_full_m'])<float(u['draft_empty_m']): fail(f'unit {uid}: full draft below empty draft')
            allowed=u['cargo_type_allowed']
            if isinstance(allowed,str): allowed=allowed.split('|')
            if not isinstance(allowed,list) or not allowed or any(not isinstance(v,str) or not v.strip() for v in allowed): fail(f'unit {uid}: cargo_type_allowed is empty/invalid')
            a=date(u['available_from'],f'unit {uid}.available_from'); b=date(u['available_to'],f'unit {uid}.available_to')
            if b<=a: fail(f'unit {uid}: available_to must be after available_from')
        if barges & tugs: fail('a physical identifier is used as both barge and tug')
        calendar=set()
        for i,x in enumerate(conditions):
            label=f'conditions[{i}]'; day=date(x['date'],label+'.date')
            if day.time()!=datetime.min.time() or x['date']!=day.date().isoformat(): fail(f'{label}: date must be YYYY-MM-DD')
            sid=ident(x['segment_id'],label+'.segment_id')
            if sid not in segment_ids: fail(f'{label}: unknown segment_id {sid}')
            key=(day.date(),sid)
            if key in calendar: fail(f'duplicate condition for {day.date()} / {sid}')
            calendar.add(key)
            number(x['usable_depth_m'],label+'.usable_depth_m',nonnegative=True)
            number(x['max_allowed_draft_m'],label+'.max_allowed_draft_m',nonnegative=True)
            if str(x['is_closed']) not in ('0','1'): fail(f'{label}.is_closed must be 0 or 1')
        day=start
        while day<=end:
            for sid in segment_ids:
                if (day.date(),sid) not in calendar: fail(f'Missing condition for date {day.date()} / segment {sid}; missing is not closure')
            day+=timedelta(days=1)
        def check_totals(expected,actual,label):
            if expected is None: return False
            if set(expected)!=set(actual): fail(f'{label}: identifiers differ: expected {sorted(expected)}, actual {sorted(actual)}')
            for key,value in expected.items():
                target=number(value,f'{label} {key}',nonnegative=True)
                if abs(target-actual[key])>1e-6: fail(f'{label} {key}: expected {target}, batches sum to {actual[key]:.3f}')
            return True
        return {'cargo_by_type_t':dict(bytype),'cargo_by_plan_t':dict(byplan),
                'category_totals_checked':check_totals(expected_by_type,bytype,'category totals'),
                'plan_totals_checked':check_totals(expected_by_plan,byplan,'plan totals')}
    except KeyError as e:
        fail(f'Missing required input field: {e.args[0]}')


def validate_folder(folder,c):
    folder=Path(folder); cargo=rows(folder/'cargo.csv'); units=rows(folder/'units.csv'); conditions=rows(folder/'conditions.csv')
    expected={}
    for name,key in [('expected_cargo_totals.csv','cargo_type'),('expected_plan_totals.csv','plan_id')]:
        path=folder/name
        if path.exists():
            values={}
            for row in rows(path):
                if key not in row or 'volume_t' not in row: fail(f'{path}: expected columns {key}, volume_t')
                k=ident(row[key],f'{path}:{key}')
                if k in values: fail(f'{path}: duplicate {key}: {k}')
                values[k]=row['volume_t']
            expected[key]=values
    # The archive manifest declares scenario-wide demand scaling. Do not infer
    # a total for user scenarios that do not declare one.
    manifest=folder.parent.parent/'scenario_manifest.csv'
    if manifest.exists() and 'cargo_type' not in expected and 'plan_id' not in expected:
        matches=[x for x in rows(manifest) if x.get('scenario_id')==folder.name]
        if len(matches)>1: fail(f'{manifest}: duplicate scenario_id {folder.name}')
        if matches:
            scale=number(matches[0].get('demand_scale'),f'{manifest}:demand_scale',nonnegative=True)
            expected['plan_id']={p['plan_id']:number(p['volume_t'],f"plan {p['plan_id']}.volume_t",nonnegative=True)*scale for p in c.get('plans',[])}
            totals=defaultdict(float)
            for p in c.get('plans',[]): totals[p['cargo_type']]+=float(p['volume_t'])*scale
            expected['cargo_type']=dict(totals)
    if cargo and 'cargo_type' not in expected:
        fail(f'{folder}: missing expected_cargo_totals.csv (or matching scenario manifest)')
    if any(x.get('plan_id') for x in cargo) and 'plan_id' not in expected:
        fail(f'{folder}: missing expected_plan_totals.csv (or matching scenario manifest)')
    summary=validate_inputs(c,cargo,units,conditions,expected.get('cargo_type'),expected.get('plan_id'))
    vessels_path=folder/'vessels.csv'
    if vessels_path.exists():
        vessels={}
        for row in rows(vessels_path):
            try:
                vid=ident(row['vessel_id'],f'{vessels_path}:vessel_id')
                role=ident(row['vessel_type'],f'{vessels_path}:vessel_type')
            except KeyError as e: fail(f'{vessels_path}: missing column {e.args[0]}')
            if vid in vessels: fail(f'{vessels_path}: duplicate vessel_id {vid}')
            vessels[vid]=role
        for unit in units:
            for key,role in (('barge_id','barge'),('tug_id','tug')):
                vid=unit[key]
                if vessels.get(vid)!=role: fail(f'unit {unit["unit_id"]}: {key} {vid} missing or wrong role in vessels.csv')
    summary['vessel_links_checked']=vessels_path.exists()
    return summary


def main():
    p=argparse.ArgumentParser(description='Validate inputs without running an optimizer')
    p.add_argument('--scenario',required=True,type=Path); p.add_argument('--config',required=True,type=Path)
    a=p.parse_args(); c=json.loads(a.config.read_text(encoding='utf-8-sig'))
    print(json.dumps(validate_folder(a.scenario,c),ensure_ascii=False,indent=2))
if __name__=='__main__': main()
