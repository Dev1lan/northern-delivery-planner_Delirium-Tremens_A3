"""Compare protected cargo categories without changing optimization or result schema."""
import argparse
import json
from pathlib import Path
from result_adapter import DEFAULT_CONFIG, run_scenario

DEFAULT_CRITICAL_TYPES = ('food', 'food_extended')


def compare_critical_cargo(fcfs, optimized, critical_types=DEFAULT_CRITICAL_TYPES):
    critical_types=tuple(critical_types)
    if any(not isinstance(x,str) or not x.strip() for x in critical_types):
        raise ValueError('critical cargo types must be nonempty strings')
    if len(set(critical_types)) != len(critical_types):
        raise ValueError('critical cargo types must be unique')
    if fcfs['strategy'] != 'FCFS' or optimized['strategy'] != 'OPTIMIZED':
        raise ValueError('expected FCFS and OPTIMIZED results in that order')
    if fcfs['scenario_id'] != optimized['scenario_id']:
        raise ValueError('cannot compare different scenarios')
    comparisons=[]; warnings=[]
    for category in critical_types:
        on_a=fcfs['metrics']['on_time_by_type_t']; on_b=optimized['metrics']['on_time_by_type_t']
        total_a=fcfs['metrics']['delivered_by_type_t']; total_b=optimized['metrics']['delivered_by_type_t']
        present_a=category in total_a; present_b=category in total_b
        if present_a!=present_b: raise ValueError(f'inconsistent cargo categories: {category}')
        if not present_a:
            comparisons.append({'cargo_type':category,'status':'not_present'})
            continue
        a=float(total_a[category]); b=float(total_b[category]); oa=float(on_a[category]); ob=float(on_b[category])
        row={'cargo_type':category,'status':'compared','fcfs_delivered_total_t':a,
             'optimized_delivered_total_t':b,'delta_delivered_total_t':round(b-a,3),
             'fcfs_delivered_on_time_t':oa,'optimized_delivered_on_time_t':ob,
             'delta_delivered_on_time_t':round(ob-oa,3)}
        comparisons.append(row)
        for metric,baseline,actual in [('delivered_total_t',a,b),('delivered_on_time_t',oa,ob)]:
            if actual < baseline-0.0005:
                warnings.append({'code':'CRITICAL_CARGO_DECREASE','cargo_type':category,'metric':metric,
                                 'fcfs_t':baseline,'optimized_t':actual,'delta_t':round(actual-baseline,3),
                                 'message':f'{category}: {metric} is lower for OPTIMIZED than FCFS'})
    return {'schema_version':'1.0.0','scenario_id':fcfs['scenario_id'],
            'critical_types':list(critical_types),'comparisons':comparisons,'warnings':warnings}


def main():
    p=argparse.ArgumentParser(description='Compare critical cargo in two audited schedules')
    p.add_argument('--scenario',type=Path,required=True); p.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    p.add_argument('--critical-type',action='append',dest='categories',help='May be supplied multiple times')
    p.add_argument('--output',type=Path)
    a=p.parse_args(); fcfs=run_scenario(a.scenario,config_path=a.config,strategy='FCFS')
    opt=run_scenario(a.scenario,config_path=a.config,strategy='OPTIMIZED')
    output=compare_critical_cargo(fcfs,opt,a.categories or DEFAULT_CRITICAL_TYPES)
    payload=json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    if a.output: a.output.write_text(payload,encoding='utf-8')
    else:
        import sys
        sys.stdout.buffer.write(payload.encode('utf-8'))
if __name__=='__main__': main()
