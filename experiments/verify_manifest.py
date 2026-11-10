"""Check historical SHA-256 values after restructuring without editing manifests."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL_SOURCES = {'generate.py','optimizer.py','validate.py','run_final_experiments.py','run_extended_experiments.py'}

def normalize(key):
    return Path(*key.replace('\\','/').split('/'))

def resolve_manifest_path(key):
    path=normalize(key)
    if path.parts and path.parts[0]=='final_results':
        return Path('results')/path
    return path

def check_table(table, base, source_map=False, previous=False):
    failures=[]
    for key,expected in table.items():
        path=normalize(key)
        if source_map and key in HISTORICAL_SOURCES:
            path=(Path('results/provenance/optimizer_previous.py') if previous and key=='optimizer.py' else Path('release_2.1.0')/key)
        target=base/path
        if not target.is_file(): failures.append(f'{key}: missing at {target}')
        elif hashlib.sha256(target.read_bytes()).hexdigest()!=expected: failures.append(f'{key}: SHA-256 mismatch')
    return failures

def main():
    version=json.loads((ROOT/'ALGORITHM_VERSION.json').read_text(encoding='utf-8-sig'))
    final_manifest=ROOT/resolve_manifest_path(version['scenario_input_manifest'])
    manifests=[('scenario archive',ROOT/'data/manifest.json',ROOT/'data','csv_sha256',False,False),
               ('final experiment',final_manifest,ROOT,None,True,False),
               ('previous input',ROOT/'results/previous_input_hashes.json',ROOT,None,True,True),
               ('algorithm 2.1.0',ROOT/'ALGORITHM_VERSION.json',ROOT/'release_2.1.0','source_sha256',False,False)]
    count=0
    for label,path,base,field,source_map,previous in manifests:
        if not path.is_file():
            raise FileNotFoundError(f'{path}: full local archive is required for {label}')
        data=json.loads(path.read_text(encoding='utf-8-sig')); table=data[field] if field else data
        failures=check_table(table,base,source_map,previous)
        if label=='scenario archive':
            actual=hashlib.sha256((ROOT/'scenario_config.json').read_bytes()).hexdigest()
            if actual!=data['config_sha256']: failures.append('scenario_config.json: SHA-256 mismatch')
            count+=1
        if failures: raise AssertionError(f'{label}: '+ '; '.join(failures[:5]))
        count+=len(table); print(f'{label}: {len(table)} hashes verified')
    print(f'PASS: {count} historical hashes; manifests and checksums unchanged')

if __name__=='__main__': main()
