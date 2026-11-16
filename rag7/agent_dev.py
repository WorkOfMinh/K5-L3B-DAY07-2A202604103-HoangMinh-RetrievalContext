"""Gemini-backed dev smoke/comparison. No test runner or automatic selection."""
import argparse
import json
from pathlib import Path
import re
import statistics
import sys
from time import perf_counter
import zipfile
from .common import (ROOT, METHODS, file_hash, load_config, load_corpus, load_dev,
                     object_hash, read_json, require, save_json)
from .dense_backend import DensePgStore, LocalQueryEncoder, read_database_url, redact_error
from .gemini_provider import (GeminiClient, ProviderFailure, SessionPaused, append_jsonl,
                              read_credentials, redact, utc_now, SUPPORTED_MODELS)
from .agent_runtime import run_agent
from .retrievers import BM25Index, RetrievalEngine
from .retrieval_dev import evidence_hit


def load_agent_config():
    config = read_json(ROOT / 'config/agent.json')
    require(config['provider'] == 'gemini' and config['model'] in SUPPORTED_MODELS, 'Unsupported agent provider/model.')
    rows = config['configurations']
    require([c['id'] for c in rows] == ['dense_base','bm25_base','hybrid_base','hybrid_context5'], 'Expected four declared configurations.')
    require(config['candidate_pool'] == 10 and config['generation']['thinkingConfig'] == {'thinkingLevel':'low'},
            'Unexpected retrieval/thinking policy.')
    require(config['pricing']['declared_tier'] == 'free', 'This runner is configured for a user-verified Free-tier project.')
    for c in rows:
        require(c['method'] in METHODS and c['max_searches'] == 2, 'Unexpected method/tool budget.')
        require((c['context_chunks'],c['budget_chars']) == ((5,5500) if c['id'] == 'hybrid_context5' else (3,3500)),
                'Unexpected context policy.')
    return config


def code_hash():
    return object_hash({p.name:file_hash(p) for p in sorted((ROOT / 'rag7').glob('*.py'))})


def bind_experiment(folder, identity):
    folder = Path(folder)
    path = folder / 'experiment.json'
    if path.exists():
        saved = read_json(path)
        require(saved['identity'] == identity and saved['identity_sha256'] == object_hash(identity),
                'Experiment inputs/code changed. Preserve this run; use a new --experiment name after explaining the change.')
        return saved
    saved = {'created_at':utc_now(), 'identity':identity, 'identity_sha256':object_hash(identity)}
    save_json(path, saved)
    return saved


def seal_result(result):
    return {**result, 'trace_sha256':object_hash(result)}


def load_result(path):
    result = read_json(path)
    require(result['trace_sha256'] == object_hash({k:v for k,v in result.items() if k != 'trace_sha256'}),
            'Saved completed trace changed; do not silently overwrite evaluation evidence.')
    return result


def attach_evaluation(result, case, original):
    # Scoring happens only after the agent has finalized. Labels never enter its inputs.
    answerable = case['answerable']
    gold = case['gold']
    context_sent = result['model_calls'][-1]['context_sent']
    require(context_sent == result['final_context'], 'Final context was not actually sent to the final model call.')
    result['evaluation_only'] = {'answerable':answerable, 'gold':gold,
        'expected_answer':case.get('expected_answer'), 'absence_reason':case.get('absence_reason'),
        'note':'Attached after the model run. Never passed to controller or search.'}
    result['original_query_retrieval'] = original
    result['automatic_provisional'] = {
        'original_query_hit_at_1': evidence_hit(original['ranking'][0],gold) if answerable and original['ranking'] else (False if answerable else None),
        'original_query_evidence_hit_at_3':any(evidence_hit(h,gold) for h in original['ranking'][:3]) if answerable else None,
        'actual_final_context_exact_evidence':any(evidence_hit(h,gold) for h in context_sent['selected_chunks']) if answerable else None,
        'answer_grade':None, 'citation_semantic_support':None, 'correct_abstention':None,
        'note':'Source-equivalent evidence and semantic judgments require review. Valid citation IDs do not establish support.'}
    return seal_result(result)


def build_review_template(results, existing=None):
    old = {(r['config_id'],r['query_id']):r for r in (existing or [])}
    rows = []
    for t in results:
        row = {'config_id':t['config_id'], 'query_id':t['query_id'], 'trace_sha256':t['trace_sha256'],
            'question':t['public_query']['question'], 'evaluation_only':t['evaluation_only'],
            'status':t['status'], 'answer':t['answer'], 'citations':t['citations'],
            'actual_final_context':t['final_context'],
            'review':{'answer_grade':None, 'citation_support':[None for _ in t['citations']],
                'abstention_correct':None, 'context_evidence_supported':None,
                'reviewer':None, 'review_type':None, 'reason':None, 'human_confirmation':None}}
        prior = old.get((t['config_id'],t['query_id']))
        if prior is not None:
            require(prior['trace_sha256'] == t['trace_sha256'], 'Existing review refers to a different trace.')
            row['review'] = prior['review']
        rows.append(row)
    return rows


def fraction(n, N):
    return {'numerator':n, 'denominator':N, 'rate':n/N if N else None}


def summarize(results, configurations, total_per_config, folder):
    methods = {}
    for c in configurations:
        rows = [r for r in results if r['config_id'] == c['id']]
        positive = [r for r in rows if r['evaluation_only']['answerable']]
        negative = [r for r in rows if not r['evaluation_only']['answerable']]
        metrics = {name:fraction(sum(r['automatic_provisional'][name] for r in positive),len(positive))
            for name in ['original_query_hit_at_1','original_query_evidence_hit_at_3','actual_final_context_exact_evidence']}
        uncached = [r['timing']['wall_seconds_excluding_pacing']*1000 for r in rows if not r['timing']['reused_api_checkpoints']]
        methods[c['id']] = {'method':c['method'], 'completed_cases':len(rows), 'expected_cases':total_per_config,
            'completed_answerable_cases':len(positive), 'completed_insufficient_cases':len(negative),
            'provisional_exact_evidence_metrics_on_completed_answerable_cases':metrics,
            'statuses':{s:sum(r['status'] == s for r in rows) for s in ['answered','abstain']},
            'successful_search_calls':sum(r['search_calls'] for r in rows),
            'model_decisions':sum(len(r['model_calls']) for r in rows),
            'median_uncached_end_to_end_excluding_pacing_ms':statistics.median(uncached) if uncached else None,
            'uncached_latency_cases':len(uncached),
            'grounded_answer_grade':None, 'citation_support':None, 'correct_abstentions':None,
            'review_status':'pending; output status is not a correctness judgment'}
    events = []
    ledger = Path(folder) / 'api_attempts.jsonl'
    if ledger.exists():
        events = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines() if line.strip()]
    successes = [e for e in events if e['event'] == 'attempt_succeeded']
    response_records = [read_json(p) for p in sorted((Path(folder) / 'api_cache').glob('*.json'))]
    # One successful cache entry per logical request. Ledger retains all actual attempts,
    # including retries, failures and an unpaired start if a process died mid-request.
    usage = {}
    for record in response_records:
        for name, value in (record['response'].get('usageMetadata') or {}).items():
            if type(value) is int:
                usage[name] = usage.get(name,0) + value
    errors = [e for e in events if e['event'] == 'attempt_failed']
    return {'created_at':utc_now(), 'status':'complete_pending_semantic_audit' if all(len([r for r in results if r['config_id'] == c['id']]) == total_per_config for c in configurations) else 'partial_resume_required',
        'split':'dev', 'configurations':methods, 'successful_api_responses':len(response_records),
        'api_attempts_started':sum(e['event'] == 'attempt_started' for e in events),
        'api_failures':len(errors), 'api_error_events':errors, 'observed_successful_response_usage':usage,
        'missing_usage_responses':sum(not r['response'].get('usageMetadata') for r in response_records),
        'unpaired_attempt_starts':max(0,sum(e['event'] == 'attempt_started' for e in events)-len(successes)-len(errors)),
        'cost':{'estimated_paid_api_cost_usd':0, 'actual_billed_cost_usd':None,
            'basis':'Free-tier declaration; verify project tier/usage in AI Studio. API responses do not prove billing tier. Failed/interrupted requests may have unknown token usage.'},
        'configuration_selected':False, 'test_run':False,
        'limitations':['Automatic exact evidence is provisional and may miss equivalent support.',
            'No human answer/citation/abstention judgments are inferred.',
            'Summary metrics use completed answerable cases only; partial runs are not full-dev comparisons.',
            'API response caching supports recovery, not independent repeated model trials.',
            'Pacing is reported separately. Resumed cases are excluded from uncached latency summaries.',
            'Provider model aliases and free quotas can change; resolved modelVersion is checked.']}


def write_outputs(folder, results, configurations, total_per_config, mode):
    results = sorted(results, key=lambda r:(r['query_id'],r['config_id']))
    save_json(folder / 'agent_traces.json', results)
    review_path = folder / 'agent_review_template.json'
    previous = read_json(review_path) if review_path.exists() else None
    save_json(review_path, build_review_template(results,previous))
    summary = summarize(results,configurations,total_per_config,folder)
    save_json(folder / 'agent_summary.json', summary)
    bundle = ROOT / 'results' / ('agent_smoke_bundle.zip' if mode == 'smoke' else 'agent_dev_bundle.zip')
    temporary = bundle.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        # Include exact API requests/responses for context/citation and usage audits.
        for path in sorted(folder.rglob('*')):
            if path.is_file() and path.suffix in {'.json','.jsonl'}:
                archive.write(path,path.relative_to(folder))
    temporary.replace(bundle)
    return summary,bundle


def run(args):
    config, retrieval = load_agent_config(), load_config()
    chunks, manifest, benchmark_lock = load_corpus()
    cases, public_queries = load_dev(benchmark_lock)
    key, model = read_credentials()
    day6 = args.day6_starter.resolve()
    database_url = read_database_url(day6)
    require(model == config['model'], f"Model settings differ: environment/.env uses {model}, config/agent.json uses {config['model']}. Match both before running; keep model changes in separate experiments.")
    configurations = [c for c in config['configurations'] if c['id'] == 'hybrid_base'] if args.mode == 'smoke' else config['configurations']
    selected_public = public_queries[:1] if args.mode == 'smoke' else public_queries
    cases_by_id = {c['id']:c for c in cases}
    prefix = 'gemini35lite' if model == 'gemini-3.5-flash-lite' else 'gemini38'
    experiment = args.experiment or (prefix + ('_smoke_v5' if args.mode == 'smoke' else '_dev_v5'))
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,64}',experiment), 'Use a short letters/digits/underscore experiment name.')
    folder = ROOT / 'results/agent_runs' / experiment
    identity = {'mode':args.mode, 'provider_model':model, 'agent_config':config, 'retrieval_config':retrieval,
        'transport_policy':{'socket_timeout_seconds':getattr(args,'api_timeout',180)},
        'code_sha256':code_hash(), 'benchmark_seal':benchmark_lock['seal_sha256'],
        'source_content':benchmark_lock['source_content'], 'database_digest':manifest['import_identity']['db_digest'],
        'embedding_signature':manifest['model']['signature'], 'query_ids':[p['id'] for p in selected_public]}
    bind_experiment(folder, identity)
    save_json(folder / 'agent_config.json',config)
    save_json(folder / 'retrieval_config.json',retrieval)
    save_json(folder / 'source_provenance.json',{'benchmark_lock':benchmark_lock, 'day6_manifest':manifest})
    # Rotation across question/configuration combinations prevents all of one
    # configuration being generated before the others across quota windows.
    tasks = []
    for index, public in enumerate(selected_public):
        rotated = configurations[index % len(configurations):] + configurations[:index % len(configurations)]
        tasks.extend((public,c) for c in rotated)
    results, pending = [], []
    for public, c in tasks:
        path = folder / 'cases' / (c['id'] + '_' + public['id'] + '.json')
        if path.exists():
            result = load_result(path)
            require(result['public_query'] == public and result['config_id'] == c['id'], 'Saved case identity mismatch.')
            results.append(result)
        else:
            pending.append((public,c,path))
    if not pending:
        _, bundle = write_outputs(folder,results,configurations,len(selected_public),args.mode)
        print(f'All {len(results)} cases already complete. Saved {bundle.relative_to(ROOT)}; no new API/model/search calls.')
        return 0
    client = GeminiClient(key,config,folder,min_interval=args.min_interval,max_calls=args.max_api_calls,
                          timeout_seconds=getattr(args,'api_timeout',180))
    store = None
    code_before = code_hash()
    status = 0
    try:
        store = DensePgStore(database_url,manifest,chunks)
        encoder = LocalQueryEncoder(manifest,day6,offline=args.offline)
        lexical = BM25Index(chunks,**retrieval['bm25'])
        engine = RetrievalEngine(chunks,lexical,store,rank_constant=retrieval['rrf']['rank_constant'])
        for public,c,path in pending:
            # Reserve worst-case search+final calls; a mid-case quota error is
            # still recoverable using exact saved model response checkpoints.
            if client.new_calls and args.max_api_calls - client.new_calls < c['max_searches'] + 2:
                raise SessionPaused('Not enough session request budget for another case. Run the same command to resume.')
            print(f"Running {c['id']} / {public['id']} ({len(results)}/{len(tasks)} complete).",flush=True)
            def search(query,filters):
                vector = encoder.embed_query(query) if c['method'] != 'bm25' else None
                return engine.search(c['method'],query,filters=filters,top_k=config['candidate_pool'],query_vector=vector)
            # Independent original-question ranking is never silently replaced
            # by the model's rewritten query ranking for Hit@1 / Hit@3.
            original = search(public['question'],public['filter'])
            trace = run_agent(public,c,config['generation'],client,search)
            store.verify_fresh_snapshot()
            require(code_hash() == code_before, 'Runtime code changed during this experiment.')
            current_chunks,current_manifest,current_lock = load_corpus()
            require(current_lock == benchmark_lock and current_chunks == chunks and
                    current_manifest == manifest and load_agent_config() == config and load_config() == retrieval,
                    'Inputs changed while running; do not mix evidence.')
            load_dev(current_lock)
            result = attach_evaluation(trace,cases_by_id[public['id']],original)
            save_json(path,result)
            results.append(result)
            save_json(folder / 'embedding_provenance.json',encoder.provenance())
            print(f"  {result['status']} | searches={result['search_calls']} | citations={len(result['citations'])}",flush=True)
            write_outputs(folder,results,configurations,len(selected_public),args.mode)
    except (Exception,KeyboardInterrupt) as exc:
        message = redact(redact_error(exc,database_url),[key])
        paused = isinstance(exc,(SessionPaused,KeyboardInterrupt))
        append_jsonl(folder / 'runtime_events.jsonl',{'at':utc_now(), 'event':'paused' if paused else 'error',
            'error':message, 'completed_cases':len(results),
            'note':'No failed/pending case is converted to an abstention or a success.'})
        print(('PAUSED: ' if paused else 'ERROR: ') + message,flush=True)
        status = 2 if paused else 1
    finally:
        if store is not None:
            store.close()
        summary,bundle = write_outputs(folder,results,configurations,len(selected_public),args.mode)
        print(f"Saved {bundle.relative_to(ROOT)} | completed {len(results)}/{len(tasks)} cases | new HTTP attempts this session {client.new_calls}.",flush=True)
        if args.mode == 'smoke' and results:
            print('Smoke answer: ' + results[0]['answer'],flush=True)
        print('Semantic audits pending. No winner selected. No test retrieval or test model call performed.',flush=True)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a real Gemini tool-calling agent on dev only. Free-tier pacing/checkpoints; no test or automatic selection.')
    parser.add_argument('mode',choices=['smoke','dev'])
    parser.add_argument('--day6-starter',type=Path,required=True)
    parser.add_argument('--max-api-calls',type=int,default=20,help='Maximum new HTTP attempts per invocation; cached responses do not consume this budget.')
    parser.add_argument('--min-interval',type=float,default=15,help='Minimum seconds between HTTP starts (0-60). Check actual AI Studio RPM/TPM/RPD.')
    parser.add_argument('--api-timeout',type=float,default=180,help='Socket connect/read timeout in seconds (10-600); not a total job deadline. Network timeouts stop without automatic duplicate requests.')
    parser.add_argument('--experiment',help='New explicit experiment name when changing frozen code/config/model inputs.')
    parser.add_argument('--offline',action='store_true',help='Require already cached local E5 model files.')
    args = parser.parse_args(argv)
    try:
        require(args.max_api_calls >= 1 and 0 <= args.min_interval <= 60, 'Invalid request budget or pacing interval.')
        require(10 <= args.api_timeout <= 600, 'API timeout must be 10-600 seconds.')
        return run(args)
    except (Exception,KeyboardInterrupt) as exc:
        print('ERROR: ' + redact(exc),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
