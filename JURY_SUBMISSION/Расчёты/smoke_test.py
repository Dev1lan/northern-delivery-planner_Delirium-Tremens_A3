"""Check route portability and retained PDF evidence without touching the dataset."""
import copy
import hashlib
import json
from generate import BASE, read_csv, load_scenario, simulate
from validate import audit,require

c=json.loads((BASE/'scenario_config.json').read_text(encoding='utf-8'))
cargo,units,conditions=load_scenario(BASE/'data/scenarios/r1_baseline_central_central')
c=copy.deepcopy(c); c['origin']='Port_A'; c['destination']='Port_C'
for i,s in enumerate(c['segments']):
    s['from_node']=['Port_A','Port_B'][i]
    s['to_node']=['Port_B','Port_C'][i]
    s['distance_km']=[40,60][i]
tiny=[copy.deepcopy(cargo[0])]
tiny[0].update(origin='Port_A',destination='Port_C',volume_t=10,ready_date=c['start'],due_date=c['end'])
for x in conditions: x.update(usable_depth_m=5,max_allowed_draft_m=4.85,is_closed=0)
trips,metrics=simulate(c,tiny,units,conditions,'baseline')
audit(c,tiny,units,conditions,trips)
require(metrics['total_delivered_t']==10,'Portability to 100 km chain')
for row in read_csv(BASE/'reference/source_file_hashes.csv'):
    require(hashlib.sha256((BASE/'sources_local'/row['file']).read_bytes()).hexdigest()==row['sha256'],'PDF evidence hash')
print('PASS: different nodes and distance; original PDF checksums')
