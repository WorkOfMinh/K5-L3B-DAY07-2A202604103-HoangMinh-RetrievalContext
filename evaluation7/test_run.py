"""Run only the frozen baseline/selected comparison on the seven test cases."""
import argparse
import json
from pathlib import Path
import statistics
import zipfile
from rag7.common import ROOT,object_hash,read_json,require,save_json
from rag7.agent_dev import (bind_experiment,load_result,attach_evaluation,seal_result,build_review_template)
from rag7.agent_runtime import run_agent
from rag7.dense_backend import DensePgStore,LocalQueryEncoder,read_database_url,redact_error
from rag7.gemini_provider import GeminiClient,SessionPaused,append_jsonl,read_credentials,redact,utc_now
from rag7.retrievers import BM25Index,RetrievalEngine
from .selection import LOCK_PATH,verify_current,verify_seal


def load_test(lock,folder=None):
    # Fail before opening a held-out file if no valid pre-test selection exists.
    verify_seal(lock)
    folder=Path(folder) if folder is not None else ROOT/'benchmark/day7'
    cases=read_json(folder/'test.json');public=read_json(folder/'test_queries.json')
    for name,value in [('test.json',cases),('test_queries.json',public)]:
        require(object_hash(value)==lock['benchmark_content_hashes'][name],'Held-out benchmark changed: '+name)
    require(len(cases)==7 and sum(c['answerable'] for c in cases)==5 and all(c['split']=='test' for c in cases),'Expected five answerable and two insufficient test cases.')
    require(public==[{k:c.get(k) for k in ['id','question','filter']} for c in cases],'Test public view changed or contains labels.')
    return cases,public


def test_summary(results,configurations,folder):
    output={}
    for config in configurations:
        rows=[r for r in results if r['config_id']==config['id']]
        positive=[r for r in rows if r['evaluation_only']['answerable']]
        metrics={name:{'numerator':sum(r['automatic_provisional'][name] for r in positive),
                    'denominator':len(positive)} for name in
                 ['original_query_hit_at_1','original_query_evidence_hit_at_3','actual_final_context_exact_evidence']}
        for m in metrics.values():m['rate']=m['numerator']/m['denominator'] if m['denominator'] else None
        wall=[r['timing']['wall_seconds_this_invocation'] for r in rows if not r['timing']['reused_api_checkpoints']]
        active=[r['timing']['wall_seconds_excluding_pacing'] for r in rows if not r['timing']['reused_api_checkpoints']]
        output[config['id']]={'completed_cases':len(rows),'expected_cases':7,'completed_answerable':len(positive),
            'completed_insufficient':len(rows)-len(positive),'provisional_exact_metrics':metrics,
            'answer_statuses':{s:sum(r['status']==s for r in rows) for s in ['answered','abstain']},
            'search_calls':sum(r['search_calls'] for r in rows),'model_decisions':sum(len(r['model_calls']) for r in rows),
            'citation_repairs':sum(len(r.get('format_repairs',[])) for r in rows),
            'median_uncached_wall_seconds':statistics.median(wall) if wall else None,
            'median_uncached_seconds_excluding_pacing':statistics.median(active) if active else None,
            'uncached_timing_cases':len(wall),'grounded_answer_grade':None,'citation_support':None,'correct_abstentions':None}
    ledger=Path(folder)/'api_attempts.jsonl'
    events=[json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines() if line.strip()] if ledger.exists() else []
    records=[read_json(p) for p in sorted((Path(folder)/'api_cache').glob('*.json'))]
    usage={}
    for record in records:
        for key,value in (record['response'].get('usageMetadata') or {}).items():
            if type(value) is int:usage[key]=usage.get(key,0)+value
    return {'created_at':utc_now(),'split':'test','status':'complete_pending_semantic_audit' if len(results)==14 else 'partial_resume_required',
        'configurations':output,'successful_api_responses':len(records),
        'api_attempts':sum(e['event']=='attempt_started' for e in events),
        'api_failures':sum(e['event']=='attempt_failed' for e in events),'successful_response_usage':usage,
        'missing_usage_responses':sum(not r['response'].get('usageMetadata') for r in records),
        'cost':{'estimated_free_tier_paid_api_cost_usd':0,'actual_billed_cost_usd':None,
                'note':'Free-tier estimate; account billing not verified. Failed/interrupted response usage may be unknown.'},
        'configuration_reselected':False,'semantic_audit':'pending; output status does not establish correctness'}


def write_outputs(folder,results,configurations,lock):
    rows=sorted(results,key=lambda r:(r['query_id'],r['config_id']))
    save_json(folder/'test_traces.json',rows)
    review=folder/'test_review_template.json'
    old=read_json(review) if review.exists() else None
    save_json(review,build_review_template(rows,old))
    summary=test_summary(rows,configurations,folder)
    summary['selection_lock_seal']=lock['seal_sha256']
    save_json(folder/'test_summary.json',summary)
    save_json(folder/'selection_lock.json',lock)
    bundle=ROOT/'results/agent_test_bundle.zip';temporary=bundle.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file() and path.suffix in {'.json','.jsonl'}:archive.write(path,path.relative_to(folder))
    temporary.replace(bundle)
    return bundle


def run(args):
    lock=read_json(LOCK_PATH);chunks,manifest,benchmark=verify_current(lock)
    cases,public=load_test(lock)
    chosen=[lock['baseline_id'],lock['selected_id']]
    configs=[next(c for c in lock['agent_config']['configurations'] if c['id']==name) for name in chosen]
    folder=ROOT/'results/test_runs/locked_test_v1'
    identity={'stage':'frozen_test_comparison','selection_lock_seal':lock['seal_sha256'],
        'query_ids':[p['id'] for p in public],'configuration_ids':chosen,'socket_timeout_seconds':args.api_timeout}
    bind_experiment(folder,identity)
    # Test uses the resolved dev model version, never a silent provider upgrade.
    resolved=folder/'resolved_model.json'
    expected={'requested_model':lock['requested_model'],'model_version':lock['resolved_model_version']}
    if resolved.exists():require(read_json(resolved)==expected,'Test model version checkpoint changed.')
    else:save_json(resolved,expected)
    save_json(folder/'source_provenance.json',{'benchmark_lock':benchmark,'day6_manifest':manifest})
    tasks=[]
    for i,p in enumerate(public):
        order=configs if i%2==0 else list(reversed(configs))
        tasks.extend((p,c,folder/'cases'/f"{c['id']}_{p['id']}.json") for c in order)
    results=[];pending=[]
    for p,c,path in tasks:
        if path.exists():
            result=load_result(path)
            require(result['public_query']==p and result['config_id']==c['id'] and result['selection_lock_seal']==lock['seal_sha256'],'Saved test case belongs to a different comparison.')
            results.append(result)
        else:pending.append((p,c,path))
    if not pending:
        bundle=write_outputs(folder,results,configs,lock)
        print(f'All 14 test cases already complete. Saved {bundle.relative_to(ROOT)}; no new API/search calls.')
        return 0
    key,model=read_credentials();require(model==lock['requested_model'],'Active Gemini model differs from frozen selection.')
    url=read_database_url(args.day6_starter)
    client=GeminiClient(key,lock['agent_config'],folder,min_interval=args.min_interval,
                        max_calls=args.max_api_calls,timeout_seconds=args.api_timeout)
    store=None;status=0;by_id={c['id']:c for c in cases}
    try:
        store=DensePgStore(url,manifest,chunks)
        encoder=LocalQueryEncoder(manifest,args.day6_starter,offline=args.offline)
        retrieval=lock['retrieval_config']
        engine=RetrievalEngine(chunks,BM25Index(chunks,**retrieval['bm25']),store,rank_constant=retrieval['rrf']['rank_constant'])
        for p,c,path in pending:
            if client.new_calls and args.max_api_calls-client.new_calls<c['max_searches']+2:
                raise SessionPaused('Session request budget reached; rerun the same test command to resume.')
            print(f"Test {len(results)+1}/14: {c['id']} / {p['id']} (selection remains {lock['selected_id']}).",flush=True)
            def search(query,filters):
                vector=encoder.embed_query(query) if c['method']!='bm25' else None
                return engine.search(c['method'],query,filters=filters,top_k=lock['agent_config']['candidate_pool'],query_vector=vector)
            original=search(p['question'],p['filter'])
            trace=run_agent(p,c,lock['agent_config']['generation'],client,search)
            store.verify_fresh_snapshot();verify_current(lock)
            current_cases,current_public=load_test(lock)
            require(current_cases==cases and current_public==public,'Test inputs changed during execution.')
            evaluated=attach_evaluation(trace,by_id[p['id']],original)
            result=seal_result({**{k:v for k,v in evaluated.items() if k!='trace_sha256'},
                'split':'test','selection_lock_seal':lock['seal_sha256']})
            save_json(path,result);results.append(result)
            save_json(folder/'embedding_provenance.json',encoder.provenance())
            print(f"  {result['status']} | searches={result['search_calls']} | citations={len(result['citations'])}",flush=True)
            write_outputs(folder,results,configs,lock)
    except (Exception,KeyboardInterrupt) as exc:
        message=redact(redact_error(exc,url),[key]);paused=isinstance(exc,(SessionPaused,KeyboardInterrupt))
        append_jsonl(folder/'runtime_events.jsonl',{'at':utc_now(),'event':'paused' if paused else 'error','message':message,
            'completed_cases':len(results),'note':'Failure is not replaced with an abstention or a successful result.'})
        print(('PAUSED: ' if paused else 'ERROR: ')+message,flush=True);status=2 if paused else 1
    finally:
        if store is not None:store.close()
        bundle=write_outputs(folder,results,configs,lock)
        print(f'Saved {bundle.relative_to(ROOT)} | completed {len(results)}/14 | new HTTP attempts {client.new_calls}.')
        print('Selection unchanged. Semantic test audits pending; do not tune or reselect from test results.')
    return status


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day6-starter',type=Path,required=True)
    parser.add_argument('--max-api-calls',type=int,default=100)
    parser.add_argument('--api-timeout',type=float,default=180)
    parser.add_argument('--min-interval',type=float,default=15)
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args(argv)
    try:
        require(args.max_api_calls>0 and 10<=args.api_timeout<=600 and 0<=args.min_interval<=60,'Invalid API budget/timeout/pacing.')
        status=run(args)
        from .report import generate
        generate()
        return status
    except (Exception,KeyboardInterrupt) as exc:
        print('Test stopped: '+redact(exc));return 1


if __name__=='__main__':raise SystemExit(main())
