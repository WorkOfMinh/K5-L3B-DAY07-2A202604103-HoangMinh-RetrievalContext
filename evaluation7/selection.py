"""Select using completed dev evidence. Does not open test question files."""
import json
from pathlib import Path
from statistics import median
import zipfile
from rag7.common import ROOT,object_hash,read_json,require,save_json,file_hash,load_corpus,load_config,load_dev
from rag7.agent_dev import code_hash,load_agent_config
from rag7.gemini_provider import utc_now
from scripts.check_day7_dev_audit import check

LOCK_PATH=ROOT/'results/day7_selection_lock.json'


def framework_hash():
    files={str(p.relative_to(ROOT)).replace('\\','/'):file_hash(p)
           for p in sorted((ROOT/'evaluation7').glob('*.py'))}
    path=ROOT/'scripts/check_day7_dev_audit.py'
    files['scripts/check_day7_dev_audit.py']=file_hash(path)
    return object_hash(files)


def sealed(value):
    return {**value,'seal_sha256':object_hash(value)}


def verify_seal(lock):
    require(isinstance(lock,dict) and lock.get('seal_sha256')==object_hash({k:v for k,v in lock.items() if k!='seal_sha256'}),'Selection lock missing or changed.')
    require(lock.get('stage')=='selected_on_dev_before_test','Wrong selection stage.')
    require(lock['baseline_id']=='dense_base' and lock['selected_id']!=lock['baseline_id'],'Invalid baseline/selected comparison.')
    return lock


def choose(metrics,*,costs=None,wall_times=None):
    remaining=sorted(metrics);path=[]
    criteria=[
        ('mean_grounded_answer_grade',lambda c:metrics[c]['mean_grounded_answer_grade'],True),
        ('correct_abstentions',lambda c:metrics[c]['correct_abstentions']['numerator'],True),
        ('supported_citation_coverage',lambda c:metrics[c]['supported_citation_coverage_on_answerable']['rate'],True),
        ('reviewed_context_evidence',lambda c:metrics[c]['reviewed_actual_final_context_evidence']['rate'],True),
        ('original_query_evidence_hit_at_3',lambda c:metrics[c]['original_query_metrics']['original_query_evidence_hit_at_3']['rate'],True)]
    for name,getter,maximize in criteria:
        scores={c:getter(c) for c in remaining};best=max(scores.values())
        remaining=[c for c in remaining if scores[c]==best]
        path.append({'criterion':name,'scores':scores,'remaining':remaining[:]})
        if len(remaining)==1:return remaining[0],path
    require(costs is not None and all(c in costs and costs[c] is not None for c in remaining),
            'Quality tie reaches the cost criterion, but actual/justified cost values are missing. Do not invent zero cost.')
    values={c:costs[c] for c in remaining};best=min(values.values())
    remaining=[c for c in remaining if values[c]==best]
    path.append({'criterion':'paid_api_cost','scores':values,'remaining':remaining[:]})
    if len(remaining)==1:return remaining[0],path
    require(wall_times is not None and all(wall_times.get(c) is not None for c in remaining),'Latency tie-breaker lacks comparable uncached wall times.')
    values={c:wall_times[c] for c in remaining};best=min(values.values())
    remaining=[c for c in remaining if values[c]==best]
    path.append({'criterion':'median_end_to_end_latency','scores':values,'remaining':remaining[:]})
    selected=sorted(remaining)[0]
    if len(remaining)>1:path.append({'criterion':'lexicographic_configuration_id','remaining':[selected]})
    return selected,path


def verify_current(lock):
    verify_seal(lock)
    require(code_hash()==lock['pipeline_sha256'] and framework_hash()==lock['evaluation_framework_sha256'],'Frozen runtime/evaluation code changed.')
    require(load_agent_config()==lock['agent_config'] and load_config()==lock['retrieval_config'],'Frozen configuration changed.')
    require(object_hash(read_json(ROOT/'results/ai_review/dev_agent_review.json'))==lock['dev_reviews_sha256'],'Frozen dev judgments changed.')
    chunks,manifest,benchmark=load_corpus()
    require(benchmark['seal_sha256']==lock['benchmark_seal'] and benchmark['source_content']==lock['source_content'],'Frozen benchmark/source changed.')
    require(manifest['model']['signature']==lock['embedding_signature'] and manifest['import_identity']['db_digest']==lock['database_digest'],'Frozen index/model provenance changed.')
    return chunks,manifest,benchmark


def freeze():
    if LOCK_PATH.exists():
        lock=read_json(LOCK_PATH);verify_current(lock)
        print(f"Existing lock verified: {lock['selected_id']} vs {lock['baseline_id']}. No test file opened.")
        return lock
    bundle=ROOT/'results/agent_dev_bundle.zip'
    review_path=ROOT/'results/ai_review/dev_agent_review.json'
    identity_path=ROOT/'results/ai_review/dev_audit_identity.json'
    audited=check(bundle,review_path,identity_path)
    with zipfile.ZipFile(bundle) as archive:
        exp=json.loads(archive.read('experiment.json'))
        traces=json.loads(archive.read('agent_traces.json'))
        model=json.loads(archive.read('resolved_model.json'))
    chunks,manifest,benchmark=load_corpus();load_dev(benchmark)
    require(benchmark['seal_sha256']==exp['identity']['benchmark_seal'] and benchmark['source_content']==exp['identity']['source_content'], 'Current benchmark/source differs from completed dev evidence.')
    require(manifest['model']['signature']==exp['identity']['embedding_signature'] and manifest['import_identity']['db_digest']==exp['identity']['database_digest'], 'Current embedding/index differs from completed dev evidence.')
    policy=read_json(ROOT/'benchmark/day7/evaluation_policy.json')
    require(object_hash(policy)==benchmark['json_content_sha256']['evaluation_policy.json'],'Selection policy changed.')
    agent,retrieval=load_agent_config(),load_config()
    require(agent==exp['identity']['agent_config'] and retrieval==exp['identity']['retrieval_config'],'Current configuration differs from completed dev run.')
    require(code_hash()==exp['identity']['code_sha256'],'Pipeline differs from completed/recovered dev run.')
    selected,path=choose(audited['configurations'])
    lock=sealed({'schema_version':1,'created_at':utc_now(),'stage':'selected_on_dev_before_test',
        'selected_id':selected,'baseline_id':'dense_base','selection_path':path,'selection_policy':policy,
        'dev_metrics':audited['configurations'],'dev_reviews_sha256':object_hash(read_json(review_path)),
        'dev_traces_sha256':object_hash(traces),'dev_experiment_identity_sha256':exp['identity_sha256'],
        'review_authorship':'AI-assisted source review; no human confirmation inferred',
        'human_confirmations_pending':audited['human_confirmation_pending_rows'],
        'dev_review_revision':read_json(ROOT/'results/ai_review/dev_review_revision.json'),
        'cost_note':'Actual billed cost unknown. Unique primary-quality winner; cost and latency tie-breakers were not needed.',
        'pipeline_sha256':code_hash(),'evaluation_framework_sha256':framework_hash(),
        'agent_config':agent,'retrieval_config':retrieval,'requested_model':agent['model'],
        'resolved_model_version':model['model_version'],'benchmark_seal':benchmark['seal_sha256'],
        'benchmark_content_hashes':benchmark['json_content_sha256'],'source_content':benchmark['source_content'],
        'embedding_signature':manifest['model']['signature'],'database_digest':manifest['import_identity']['db_digest'],
        'test_results_used_for_selection':False})
    save_json(LOCK_PATH,lock)
    print(f"Frozen dev selection: {selected}. Baseline: dense_base. Selection criterion: {path[-1]['criterion']}.")
    print('Review authorship retained as AI-assisted. No API, retrieval or test question file opened.')
    return lock


def main():
    try:freeze();return 0
    except Exception as exc:print('Selection stopped: '+str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
