"""Independent demo parameter perturbations; no fitting to optimizer outcome."""
import copy
import json
from statistics import mean
from generate import BASE,load_scenario,simulate,make_conditions,dt,write_csv
from validate import audit

c=json.loads((BASE/'scenario_config.json').read_text(encoding='utf-8'))
variants={'central':{},'speed_minus20':{'speed':0.8},'speed_plus20':{'speed':1.2},
          'handling_half':{'handling':0.5},'handling_double':{'handling':2},
          'draft_minus010':{'draft':-0.1},'draft_plus010':{'draft':0.1},
          'underkeel_010':{'underkeel':0.10},'underkeel_020':{'underkeel':0.20}}
rows=[]
for rep in range(1,c['replicates']+1):
    cargo,base_units,base_conditions=load_scenario(BASE/f'data/scenarios/r{rep}_baseline_central_central')
    for name,change in variants.items():
        cfg=copy.deepcopy(c); units=copy.deepcopy(base_units)
        if 'underkeel' in change: cfg['underkeel_m']=change['underkeel']
        conditions=make_conditions(cfg,dt(units[0]['available_to']),0)
        for u in units:
            for key in ['speed_upstream_kmh','speed_downstream_kmh']: u[key]*=change.get('speed',1)
            u['handling_tph']*=change.get('handling',1)
            for key in ['draft_empty_m','draft_full_m','tug_draft_m']: u[key]+=change.get('draft',0)
        for strategy in ['baseline','optimized_heuristic']:
            trips,metrics=simulate(cfg,cargo,units,conditions,strategy)
            audit(cfg,cargo,units,conditions,trips)
            rows.append(dict(variant=name,replicate=rep,**metrics,data_status='SYNTHETIC'))
write_csv(BASE/'sensitivity_results.csv',rows)
lines=['# Чувствительность неизвестных параметров','',
       'Демонстрационные возмущения, не статистические интервалы. В каждом сравнении сохранены партии, их даты и численность флота. Изменён только указанный параметр; рейсы пересчитаны и независимо проверены validate.audit. Скорости обеих направлений масштабируются вместе, осадки всех барж и буксиров сдвигаются вместе. Производительность погрузки/выгрузки меняется, фиксированные 2 часа операции сохраняются.','',
       '| Вариант | FIFO вовремя, т | Эвристика вовремя, т | FIFO всего, т | Эвристика всего, т |','|---|---:|---:|---:|---:|']
for name in variants:
    values=[]
    for metric in ['delivered_on_time_t','total_delivered_t']:
        for strategy in ['baseline','optimized_heuristic']:
            values.append(mean(float(r[metric]) for r in rows if r['variant']==name and r['strategy']==strategy))
    lines.append('| '+name+' | '+' | '.join(f'{v:.3f}' for v in values)+' |')
(BASE/'sensitivity_report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(f'Sensitivity schedules audited: {len(rows)}')
