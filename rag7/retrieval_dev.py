"""Compare real dense/BM25/RRF retrieval on dev only; no answer-generating model or test run."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter
import zipfile
from .common import (ROOT,METHODS,canonical,file_hash,load_config,load_corpus,load_dev,
                     object_hash,require,save_json,save_jsonl)
from .dense_backend import DensePgStore,LocalQueryEncoder,read_database_url,redact_error
from .retrievers import BM25Index,RetrievalEngine,pack_context


def evidence_hit(chunk,golds):
    return any(chunk['source_id']==g['source_id'] and chunk['page']==g['page']
               and canonical(g['evidence']) in canonical(chunk['text']) for g in golds)


def mark_result(case,retrieval,context,timings,embedding_seconds,packing_seconds):
    answerable=case['answerable']
    top3=retrieval['ranking'][:3]
    top3=[{**h,'exact_evidence_hit':evidence_hit(h,case['gold']) if answerable else None} for h in top3]
    context_hit=any(evidence_hit(h,case['gold']) for h in context['selected_chunks']) if answerable else None
    return dict(query_id=case['id'],split='dev',method=retrieval['method'],
        public_query={k:case.get(k) for k in ['id','question','filter']},
        evaluation_only={'answerable':answerable,'gold':case['gold'],
            'expected_answer':case.get('expected_answer'),
            'absence_reason':case.get('absence_reason'),
            'note':'These labels are attached AFTER retrieval for scoring; never sent to the retriever/encoder.'},
        ranking=retrieval['ranking'],top3=top3,component_rankings=retrieval['components'],context=context,
        hit_at_1=bool(top3 and top3[0]['exact_evidence_hit']) if answerable else None,
        evidence_hit_at_3=any(h['exact_evidence_hit'] for h in top3) if answerable else None,
        packed_context_evidence_proxy=context_hit,
        timing={'retrieval_seconds_trials':timings,'median_retrieval_seconds':statistics.median(timings),
            'query_embedding_seconds':embedding_seconds if retrieval['method']!='bm25' else 0,
            'context_packing_seconds':packing_seconds,
            'note':'Retrieval trials reuse the same in-memory query vector. Dense/hybrid share one measured encoding; BM25 needs none. Do not sum shared encoding costs across methods.'},
        answer_status='not_generated',abstention_decision=None,
        status='retrieval_only; exact evidence proxy and packed context awaiting source/agent review')


def summarize_retrieval(traces):
    output={}
    for method in METHODS:
        rows=[t for t in traces if t['method']==method]
        require(len(rows)==17,'Expected 17 dev traces per method.')
        positive=[r for r in rows if r['evaluation_only']['answerable']]
        require(len(positive)==15,'Only 15 answerable cases belong in retrieval metric denominators.')
        metrics={}
        for name in ['hit_at_1','evidence_hit_at_3','packed_context_evidence_proxy']:
            count=sum(bool(r[name]) for r in positive)
            metrics[name]={'numerator':count,'denominator':15,'rate':count/15}
        trials=[v for t in rows for v in t['timing']['retrieval_seconds_trials']]
        require(len(trials)==51,'Expected 17 questions x 3 timed repetitions per method.')
        scoped=[r for r in rows if r['public_query']['filter'] is not None]
        compliance=sum(len(r['top3'])==3 and all(h['metadata']['chapter']==r['public_query']['filter']['chapter']
            for h in r['top3']) for r in scoped)
        output[method]=dict(**metrics,dev_cases=17,answerable_cases=15,insufficient_cases=2,
            median_cached_query_retrieval_ms=1000*statistics.median(trials),timed_lookups=len(trials),
            mean_packed_context_characters=statistics.mean(r['context']['characters'] for r in rows),
            empty_packed_contexts=sum(not r['context']['chunk_ids'] for r in rows),
            supplied_scope_compliance={'numerator':compliance,'denominator':len(scoped),
                'rate':compliance/len(scoped) if scoped else None},
            exact_evidence_misses=[r['query_id'] for r in positive if not r['evidence_hit_at_3']],
            agent_answer_grade=None,citation_support=None,abstention_success=None,
            note='Agent metrics are not measured in this step. Packed context has not yet been sent to an answer-generating model.')
    return output


def source_code_fingerprint():
    return object_hash({p.name:file_hash(p) for p in sorted((ROOT/'rag7').glob('*.py'))})


def run(args,database_url):
    started=perf_counter();config=load_config();chunks,manifest,lock=load_corpus()
    cases,public=load_dev(lock)
    code_hash=source_code_fingerprint();config_hash=object_hash(config)
    store=DensePgStore(database_url,manifest,chunks)
    try:
        tick=perf_counter();encoder=LocalQueryEncoder(manifest,args.day6_starter,offline=args.offline)
        model_load_seconds=perf_counter()-tick
        tick=perf_counter();bm25=BM25Index(chunks,**config['bm25']);lexical_build_seconds=perf_counter()-tick
        engine=RetrievalEngine(chunks,bm25,store,config['rrf']['rank_constant'])
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        run_dir=ROOT/'results/retrieval_dev_runs'/run_id;run_dir.mkdir(parents=True,exist_ok=False)
        traces=[]
        for qi,(case,query) in enumerate(zip(cases,public,strict=True)):
            tick=perf_counter();vector=encoder.embed_query(query['question']);embedding_seconds=perf_counter()-tick
            # Only public question text and explicitly supplied filter reach the engine.
            first={};timings={m:[] for m in METHODS}
            for repeat in range(config['timing_repeats']):
                shift=(qi+repeat)%3
                for method in METHODS[shift:]+METHODS[:shift]:
                    tick=perf_counter()
                    result=engine.search(method,query['question'],filters=query['filter'],
                        top_k=config['candidate_pool'],query_vector=vector if method!='bm25' else None)
                    timings[method].append(perf_counter()-tick)
                    require(method not in first or first[method]==result,'Ranking changed across repeated exact queries.')
                    first[method]=result
            for method in METHODS:
                tick=perf_counter()
                context=pack_context(first[method]['ranking'],budget_chars=config['context']['budget_chars'],
                                     max_chunks=config['context']['top_k'])
                traces.append(mark_result(case,first[method],context,timings[method],embedding_seconds,perf_counter()-tick))
            save_jsonl(run_dir/'retrieval_traces.jsonl',traces)
            print(f"Dev {qi+1}/17: {case['id']} retrieved with dense/BM25/hybrid.",flush=True)
        store.verify_fresh_snapshot()
        chunks_now,manifest_now,lock_now=load_corpus();cases_now,public_now=load_dev(lock_now)
        require(object_hash(manifest_now)==object_hash(manifest) and lock_now==lock
                and cases_now==cases and public_now==public,'Inputs changed during dev comparison.')
        require(object_hash(load_config())==config_hash and source_code_fingerprint()==code_hash,
                'Code/configuration changed during the run.')
        metrics=summarize_retrieval(traces)
        summary=dict(schema_version=1,run_id=run_id,completed_at=datetime.now(timezone.utc).isoformat(),
            status='dev_retrieval_complete_real_agent_not_run',split='dev',dev_cases=17,
            methods=metrics,config=config,config_sha256=config_hash,model=encoder.provenance(),
            corpus=dict(chunks=142,pages=37,source_content=lock['source_content'],
                database_strategy='C',verified_database_digest=manifest['import_identity']['db_digest']),
            benchmark_seal_sha256=lock['seal_sha256'],source_code_sha256=code_hash,
            real_pgvector_query_calls=store.search_calls,lexical_search_calls=bm25.search_calls,
            model_load_seconds=model_load_seconds,lexical_index_build_seconds=lexical_build_seconds,
            total_run_seconds=perf_counter()-started,answer_generating_model_calls=0,paid_api_cost_usd=0,
            test_retrieval_performed=False,configuration_selected=False,
            limitations=['Exact-evidence matching requires source review for equivalent valid passages.',
                'Packed-context proxy is not final Context Evidence Rate for context actually sent to a model.',
                'No answer/citation/abstention correctness has been measured.',
                'Latency is warm-query component timing, not an independent full-agent end-to-end comparison.',
                'All experiments must later be measured with a real answer-generating agent before final selection.'])
        save_json(run_dir/'retrieval_summary.json',summary)
        reviews=[dict(query_id=t['query_id'],method=t['method'],context=t['context']['text'],
            context_ids=t['context']['chunk_ids'],evaluation_only=t['evaluation_only'],
            context_grade_0_1_2=None,reviewer='',reason='',
            status='pending source review; planned context not yet sent to an agent model')
            for t in traces if t['evaluation_only']['answerable']]
        save_json(run_dir/'packed_context_review_template.json',reviews)
        save_json(run_dir/'retrieval_config.json',config)
        report=['# Dev retrieval comparison','',
            'Measured ranking/context preparation only. No answer-generating model, test evaluation or final selection was run.','',
            '| Method | Exact Hit@1 | Exact Evidence Hit@3 | Packed-context proxy | Median lookup |',
            '|---|---:|---:|---:|---:|']
        for method in METHODS:
            m=metrics[method]
            report.append(f"| {method} | {m['hit_at_1']['numerator']}/15 | {m['evidence_hit_at_3']['numerator']}/15 | {m['packed_context_evidence_proxy']['numerator']}/15 | {m['median_cached_query_retrieval_ms']:.2f} ms |")
        report+=['','Each method ran on all 17 dev queries; only 15 answerable cases enter ranking/context denominators. The two insufficient cases do not have abstention results yet.',
            '', 'Hybrid uses RRF over dense/BM25 top-10 candidates, not summed raw similarity scores. All methods apply the same explicitly supplied chapter scope.',
            '', 'The context budget is 3500 characters and at most three whole chunks. Candidate scanning can backfill lower-ranked chunks if a higher-ranked block exceeds budget; traces record selected/excluded IDs. No source chunk is truncated.',
            '', 'Do not select a winner from this retrieval-only table: the predeclared primary selection criterion requires real-agent answer quality. Source-equivalence and final context/answer/citation audits remain pending.','']
        (run_dir/'RETRIEVAL_REPORT.md').write_text('\n'.join(report),encoding='utf-8',newline='\n')
        bundle=ROOT/'results/retrieval_dev_bundle.zip'
        with zipfile.ZipFile(bundle.with_suffix('.tmp'),'w',zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(run_dir.iterdir()):archive.write(path,path.name)
            archive.write(ROOT/'data/day6/manifest.json','day6_import_manifest.json')
            archive.write(ROOT/'benchmark/day7/benchmark_lock.json','benchmark_lock.json')
        bundle.with_suffix('.tmp').replace(bundle)
        print('\nProvisional dev ranking results (exact evidence):')
        for method in METHODS:
            m=metrics[method]
            print(f"{method}: Hit@1 {m['hit_at_1']['numerator']}/15 | Evidence Hit@3 {m['evidence_hit_at_3']['numerator']}/15")
        print('Saved results/retrieval_dev_bundle.zip')
        print('No final configuration selected. No answer-generating model or test retrieval run.')
    finally:store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day6-starter',type=Path,required=True)
    parser.add_argument('--offline',action='store_true',help='Require already cached embedding model files.')
    args=parser.parse_args();args.day6_starter=args.day6_starter.expanduser().resolve()
    database_url=''
    try:
        database_url=read_database_url(args.day6_starter)
        run(args,database_url)
    except (Exception,KeyboardInterrupt) as error:
        message=redact_error(error,database_url)
        folder=ROOT/'results';folder.mkdir(parents=True,exist_ok=True)
        with (folder/'retrieval_errors.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'at':datetime.now(timezone.utc).isoformat(),
                'error_type':type(error).__name__,'message':message})+'\n')
        print(f'ERROR [{type(error).__name__}]: {message}',file=sys.stderr)
        print('Stopped without mock fallback. Partial traces, if any, remain in results/retrieval_dev_runs/.',file=sys.stderr)
        return 130 if isinstance(error,KeyboardInterrupt) else 1
    return 0


if __name__=='__main__':raise SystemExit(main())
