"""Summarize all paired runs without selecting favorable realizations."""
from collections import defaultdict
from statistics import mean
from generate import BASE, read_csv

rows=read_csv(BASE/'data/comparison.csv')
pairs=defaultdict(dict)
for r in rows: pairs[r['scenario_id']][r['strategy']]=r
diffs=[float(p['optimized_heuristic']['delivered_on_time_t'])-float(p['baseline']['delivered_on_time_t']) for p in pairs.values()]
lines=['# Результаты демонстрационного эксперимента','',
       'Результаты относятся только к входам scenario_config.json. Положительный эффект не задан генератору. Это оценка эвристики ранних сроков, не доказательство глобального оптимума или реального эффекта на Яне.','',
       f'Парных сравнений: {len(diffs)}. Улучшение доставки вовремя: {sum(d>0.001 for d in diffs)}, равенство: {sum(abs(d)<=0.001 for d in diffs)}, ухудшение: {sum(d< -0.001 for d in diffs)}.',
       f'Изменение доставки вовремя: минимум {min(diffs):.3f} т, среднее {mean(diffs):.3f} т, максимум {max(diffs):.3f} т.','',
       '| Условие | Стратегия | Средняя доставка вовремя, т | Средняя доставка всего, т |','|---|---|---:|---:|']
for scenario in sorted({r['scenario'] for r in rows}):
    for strategy in ['baseline','optimized_heuristic']:
        rr=[r for r in rows if r['scenario']==scenario and r['strategy']==strategy]
        lines.append(f"| {scenario} | {strategy} | {mean(float(r['delivered_on_time_t']) for r in rr):.3f} | {mean(float(r['total_delivered_t']) for r in rr):.3f} |")
lines+=['','Чувствительность показана для базового внешнего условия, с усреднением остальных факторов и seed. Это не доверительные интервалы исторических параметров.','',
        '| Фактор | Значение | FIFO вовремя, т | Эвристика вовремя, т |','|---|---|---:|---:|']
for factor in ['depth_variant','timing_variant']:
    for value in sorted({r[factor] for r in rows}):
        means=[]
        for strategy in ['baseline','optimized_heuristic']:
            rr=[r for r in rows if r['scenario']=='baseline' and r[factor]==value and r['strategy']==strategy]
            means.append(mean(float(r['delivered_on_time_t']) for r in rr))
        lines.append(f'| {factor} | {value} | {means[0]:.3f} | {means[1]:.3f} |')
lines+=['','Скорости, паспортные осадки и производительность обработки не идентифицированы по фактическим рейсам. Их отдельная чувствительность не вычислялась; менять их следует в JSON и повторять весь эксперимент. Отрицательный или нулевой эффект алгоритма сохраняется в результатах.','']
(BASE/'experiment_summary.md').write_text('\n'.join(lines),encoding='utf-8')
print('\n'.join(lines[4:6]))
