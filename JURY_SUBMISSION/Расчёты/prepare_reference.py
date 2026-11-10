"""One-time import of the supplied archive; retained references make generation standalone."""
import hashlib
import shutil
from pathlib import Path
from generate import BASE, read_csv, write_csv

root=BASE.parent
ref=BASE/'reference'; ref.mkdir(exist_ok=True)
local=BASE/'sources_local'; local.mkdir(exist_ok=True)
hashes=[]
for name in ['norm_2026.pdf','bulletin_2026-09-25.pdf','bulletin_2026-09-29.pdf']:
    shutil.copyfile(root/name,local/name)
    hashes.append(dict(file=name,sha256=hashlib.sha256((local/name).read_bytes()).hexdigest(),provenance='User supplied archive; local copy inspected'))
write_csv(ref/'source_file_hashes.csv',hashes)
observed=read_csv(root/'conditions.csv')
for r in observed:
    r['data_status']='OBSERVED' if r['record_kind'] in ['observation','actual_event'] else 'NORMATIVE'
    r['verification']='LOCAL_PDF_CHECKED' if r['source_pdf_page'] else 'PRIMARY_HTML_CHECKED'
write_csv(ref/'observed_conditions.csv',observed)
url='https://lorp.ru/about/news/1287-ekspert-rossijskogo-klassifikatsionnogo-obshchestva-rabotaet-na-r-yana/'
names=['ТО-601','РТ-475','СПН-722','СПН-699','СПН-701','Дунай','Капитан Лазарев','МП-1391','МТТ-1386','Рион','Киренга','Т-120','Т-169']
write_csv(ref/'historical_vessels.csv',[dict(vessel_name=n,as_of_date='2024-07-26',project='',vessel_type='barge' if n in ['МП-1391','МТТ-1386'] else 'auxiliary' if n.startswith('Т-') else 'UNKNOWN',capacity_t='',draft_empty_m='',draft_full_m='',confirmed_2026_pilot_count='',source_url=url,data_status='HISTORICAL',note='Mention in inspection article; no individual project inferred') for n in names])
write_csv(ref/'parameter_provenance.csv',[
 dict(dataset='actuals',fields='tonnage_t;as_of_date;area_id',status='OBSERVED',note='planned is NORMATIVE; official aggregate; not pilot'),
 dict(dataset='routes',fields='distance_km;width_m;turn_radius_m;navigation_start;navigation_end;guaranteed_depth_m',status='NORMATIVE',note='local PDF pages 75 and 92'),
 dict(dataset='vessel_projects',fields='project;deadweight_t;length_m;beam_m',status='HISTORICAL',note='catalogue characteristic; no assignment to 2026 pilot'),
 dict(dataset='vessel_projects',fields='draft_empty_m;draft_full_m;confirmed_2026_count',status='UNKNOWN',note='blank'),
 dict(dataset='cargo_plans',fields='volume_t;destination;beneficiary_district',status='NORMATIVE',note='INDEXED_ONLY_NOT_PRIMARY_VERIFIED; provisional'),
 dict(dataset='cargo_plans',fields='origin',status='UNKNOWN',note='blank; sender not established'),
 dict(dataset='cargo',fields='volume_t;ready_date;due_date;priority',status='SYNTHETIC',note='batches split provisional plan; dates/priority generated'),
 dict(dataset='cargo',fields='origin',status='ASSUMED',note='scenario boundary; all inbound before origin excluded'),
 dict(dataset='vessels;units',fields='capacity_t;draft_empty_m;draft_full_m;tug_draft_m;speed_upstream_kmh;speed_downstream_kmh;available_from;available_to;compatibility_status;handling_tph',status='ASSUMED',note='toy values; not inferred from catalogue'),
 dict(dataset='conditions',fields='usable_depth_m;max_allowed_draft_m;is_closed',status='ASSUMED',note='synthetic daily profile; not official clearance'),
 dict(dataset='trips',fields='all event fields',status='SYNTHETIC',note='calculated from scenario; no AIS or real voyage evidence')])
print('Reference inputs prepared')
