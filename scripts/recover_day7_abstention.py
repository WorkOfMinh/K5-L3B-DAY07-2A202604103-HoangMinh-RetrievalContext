"""Audited compatibility migration; standard library, no DB/model/network calls.

Copies a frozen partial run to a new continuation folder and changes ONLY the
empty-abstention display validator. Original run, raw model responses and all
completed cases are preserved. Do not loosen the experiment hash by hand.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
BASELINE_SIGNATURE = 'b0e3b54d0ff352a1f696b839c089d65c71699e7ce1c475178cd2808e8adbde36'
PATCH_SIGNATURE = 'ef2cd5fbb3974879e70657fa6aa16202b2ffe921189839197563cbb1cb4befa5'
SOURCE_NAME = 'gemini35lite_dev_v5'
TARGET_NAME = 'gemini35lite_dev_v5_abstention_fix_v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def object_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
    temporary.replace(path)


def code_files(root):
    return {p.name:file_hash(p) for p in sorted((root/'rag7').glob('*.py'))}


def folder_files(folder):
    return {p.relative_to(folder).as_posix():file_hash(p) for p in sorted(folder.rglob('*')) if p.is_file()}


def recover(root=ROOT, source_name=SOURCE_NAME, target_name=TARGET_NAME, *, _baseline=BASELINE_SIGNATURE):
    # _baseline is injectable only for synthetic unit fixtures, never a CLI flag.
    root = Path(root).resolve()
    for name in [source_name,target_name]:
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,100}',name), 'Invalid experiment name.')
    require(source_name != target_name, 'The continuation must use a new experiment folder.')
    source = root/'results/agent_runs'/source_name
    target = root/'results/agent_runs'/target_name
    require(source.is_dir(), 'Original partial run not found. Run this script from the Day 07 project root after extracting this recovery package.')
    patch = root/'patches/agent_runtime_empty_abstention.py'
    require(patch.is_file() and file_hash(patch) == PATCH_SIGNATURE, 'Recovery patch missing or changed. Extract the supplied patches directory unchanged.')
    old = read_json(source/'experiment.json')
    require(old['identity_sha256'] == object_hash(old['identity']), 'Original experiment identity changed.')
    require(old['identity']['code_sha256'] == _baseline, 'This recovery does not match the original frozen code. Preserve files and send the latest bundle.')
    current_files = code_files(root)
    current_signature = object_hash(current_files)

    if target.exists():
        require((target/'recovery/migration.json').is_file(), 'Target already exists without a completed migration; it will not be overwritten.')
        migration = read_json(target/'recovery/migration.json')
        require(migration['migration_sha256'] == object_hash({k:v for k,v in migration.items() if k != 'migration_sha256'}), 'Migration record changed.')
        require(current_signature == migration['new_code_sha256'], 'Current code differs from the recorded recovery.')
        continued = read_json(target/'experiment.json')
        require(continued['identity_sha256'] == object_hash(continued['identity']) and continued['identity']['code_sha256'] == current_signature,
                'Continuation identity changed.')
        for relative,digest in migration['preserved_completed_case_files'].items():
            require(file_hash(target/relative) == digest, 'A preserved completed trace changed.')
        print(f'Recovery already applied. Continue --experiment {target_name}. No files changed.')
        return migration

    require(current_signature == _baseline, 'Current rag7 files do not match the frozen original. Do not merge another pipeline patch first; send your latest bundle if this check fails.')
    cases = sorted((source/'cases').glob('*.json'))
    require(bool(cases), 'No completed cases found to preserve.')
    traces = read_json(source/'agent_traces.json')
    for value in traces:
        require(value['trace_sha256'] == object_hash({k:v for k,v in value.items() if k != 'trace_sha256'}), 'Aggregated completed trace changed.')
    trace_seals = set()
    for path in cases:
        value = read_json(path)
        require(value['trace_sha256'] == object_hash({k:v for k,v in value.items() if k != 'trace_sha256'}), f'Completed case changed: {path.name}')
        trace_seals.add(value['trace_sha256'])
    require(len(trace_seals) == len(cases) and trace_seals == {t['trace_sha256'] for t in traces}, 'Completed case list and saved traces disagree.')
    for path in (source/'api_cache').glob('*.json'):
        value = read_json(path)
        require(value['record_sha256'] == object_hash({k:v for k,v in value.items() if k != 'record_sha256'}), f'API checkpoint changed: {path.name}')
        require(value['request_sha256'] == object_hash(value['request']), 'API checkpoint request changed.')

    before_source = folder_files(source)
    old_runtime = (root/'rag7/agent_runtime.py').read_bytes()
    updated_files = {**current_files,'agent_runtime.py':PATCH_SIGNATURE}
    new_signature = object_hash(updated_files)
    require(new_signature != current_signature, 'No validator change detected.')
    changed_runtime = False
    try:
        shutil.copytree(source,target)
        require(folder_files(target) == before_source and folder_files(source) == before_source, 'Source changed during copying; stop the active runner before recovery.')
        # Snapshots are JSON so the normal bundle writer includes them for audit.
        save_json(target/'recovery/original_experiment.json',old)
        save_json(target/'recovery/code_snapshots.json',{
            'original_rag7_files':{p.name:p.read_text(encoding='utf-8') for p in sorted((root/'rag7').glob('*.py'))},
            'patched_agent_runtime':patch.read_text(encoding='utf-8'),
            'original_file_sha256':current_files,'patched_file_sha256':updated_files})
        new_identity = {**old['identity'],'code_sha256':new_signature}
        save_json(target/'experiment.json',{'created_at':datetime.now(timezone.utc).isoformat(),
            'identity':new_identity,'identity_sha256':object_hash(new_identity),
            'continuation_of':source_name,'original_identity_sha256':old['identity_sha256']})
        migration = {'created_at':datetime.now(timezone.utc).isoformat(),
            'kind':'empty_abstention_display_compatibility_fix',
            'source_experiment':source_name,'continuation_experiment':target_name,
            'original_identity_sha256':old['identity_sha256'],
            'old_code_sha256':current_signature,'new_code_sha256':new_signature,
            'original_run_files_sha256':before_source,
            'preserved_completed_case_files':{p.relative_to(source).as_posix():file_hash(p) for p in cases},
            'prior_completed_cases':len(cases),
            'scope':'Only allow blank answer text for explicit model abstention when the model supplies a nonempty reason; display that same reason. No prompt, model, search, context, citation policy, raw response or existing trace change.',
            'provenance':'Preserved cases were produced by the old frozen code; subsequent cases use the recorded display compatibility patch. Original code, manifest, raw responses and per-case hashes remain available.',
            'grading':'The model decision remains abstain, not a correct answer. Correctness is pending semantic review; answerable false abstentions receive grade 0.',
            'database_model_api_calls':0}
        migration['migration_sha256'] = object_hash(migration)
        save_json(target/'recovery/migration.json',migration)
        temporary = root/'rag7/agent_runtime.py.recovery-tmp'
        temporary.write_bytes(patch.read_bytes())
        temporary.replace(root/'rag7/agent_runtime.py')
        changed_runtime = True
        require(object_hash(code_files(root)) == new_signature, 'Patched code signature verification failed.')
        require(folder_files(source) == before_source, 'Original source run changed during recovery.')
    except BaseException:
        if changed_runtime:
            (root/'rag7/agent_runtime.py').write_bytes(old_runtime)
        if target.exists():
            shutil.rmtree(target)
        raise
    print(f'Recovery prepared: {len(cases)} completed cases preserved byte-for-byte.')
    print(f'Original run preserved: {source_name}')
    print(f'Continue --experiment {target_name}')
    print('No model, embedding, retrieval, database or API call performed. Raw model abstention remains unchanged.')
    return migration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-experiment',default=SOURCE_NAME)
    parser.add_argument('--target-experiment',default=TARGET_NAME)
    args = parser.parse_args()
    try:
        recover(source_name=args.source_experiment,target_name=args.target_experiment)
        return 0
    except Exception as exc:
        print(f'Recovery stopped: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
