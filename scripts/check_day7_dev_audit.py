"""Offline integrity/grade-schema checker. Does not infer semantic judgments."""
import argparse
import hashlib
import json
from pathlib import Path
import unicodedata
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ['dense_base','bm25_base','hybrid_base','hybrid_context5']


def require(ok,message):
    if not ok:
        raise ValueError(message)


def object_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def fraction(n,N):
    return {'numerator':n,'denominator':N,'rate':n/N if N else None}


def canonical(text):
    return ' '.join(unicodedata.normalize('NFKC',text).split())


def evidence_hit(chunk,gold):
    return any(chunk['source_id']==g['source_id'] and chunk['page']==g['page']
        and canonical(g['evidence']) in canonical(chunk['text']) for g in gold)


def aggregate(traces,reviews,raw_summary):
    lookup = {(r['config_id'],r['query_id']):r for r in reviews}
    require(len(lookup)==len(reviews)==68,'Expected 68 unique review rows.')
    out = {}
    human_pending = 0
    for config in CONFIGS:
        rows = [t for t in traces if t['config_id']==config]
        require(len(rows)==17 and sum(t['evaluation_only']['answerable'] for t in rows)==15,'Wrong config split coverage.')
        grades = []
        supported,total_citations,covered,context_supported = 0,0,0,0
        correct_abs,false_abs,tool_use,searches,repairs,decisions = 0,0,0,0,0,0
        for t in rows:
            key = (config,t['query_id'])
            require(key in lookup,'Missing case review.')
            row = lookup[key]
            require(row['trace_sha256']==t['trace_sha256'],'Review belongs to a different trace.')
            require(row['answer']==t['answer'] and row['status']==t['status'] and row['citations']==t['citations'], 'Review answer/citation snapshot changed.')
            require(row['actual_final_context']==t['final_context'] and row['question']==t['public_query']['question'] and row['evaluation_only']==t['evaluation_only'], 'Review source/evaluation snapshot changed.')
            review = row['review']
            require(review.get('review_type')=='AI-assisted','Preserve the audit authorship label.')
            require(isinstance(review.get('reason'),str) and review['reason'].strip(),'Review reason required.')
            support = review['citation_support']
            require(isinstance(support,list) and len(support)==len(t['citations']) and all(type(v) is bool for v in support), 'One boolean semantic support label is required per unique emitted citation.')
            require(review.get('human_confirmation') in (None,False,True), 'Invalid human-confirmation field.')
            if review.get('human_confirmation') is not True:
                human_pending += 1
            else:
                require(isinstance(review.get('human_reviewer'),str) and review['human_reviewer'].strip(), 'A confirmed review requires the actual human reviewer name.')
            supported += sum(support)
            total_citations += len(support)
            tool_use += bool(t['events'])
            searches += t['search_calls']
            repairs += len(t.get('format_repairs',[]))
            decisions += len(t['model_calls'])
            if t['evaluation_only']['answerable']:
                grade = review['answer_grade']
                require(type(grade) is int and grade in (0,1,2),'Invalid answer grade.')
                require(t['status']!='abstain' or grade==0,'An answerable abstention must receive grade 0.')
                require(type(review['context_evidence_supported']) is bool,'Answerable context needs a support judgment.')
                require(review['abstention_correct'] is None,'Correct-abstention denominator contains only insufficient cases.')
                grades.append(grade)
                context_supported += review['context_evidence_supported']
                false_abs += t['status']=='abstain'
                covered += bool(t['status']=='answered' and support and all(support))
            else:
                require(review['answer_grade'] is None and review['context_evidence_supported'] is None,'Insufficient cases must not enter answer/context denominators.')
                require(type(review['abstention_correct']) is bool,'Insufficient case needs an explicit abstention judgment.')
                require(not review['abstention_correct'] or t['status']=='abstain','Correct abstention conflicts with model status.')
                correct_abs += review['abstention_correct']
        raw = raw_summary['configurations'][config]
        out[config] = {'mean_grounded_answer_grade':sum(grades)/15,
            'grounded_grade_counts':{str(v):grades.count(v) for v in (0,1,2)},
            'fully_correct_answerable_cases':fraction(grades.count(2),15),
            'citation_semantic_support':fraction(supported,total_citations),
            'supported_citation_coverage_on_answerable':fraction(covered,15),
            'reviewed_actual_final_context_evidence':fraction(context_supported,15),
            'correct_abstentions':fraction(correct_abs,2),'false_abstentions':fraction(false_abs,15),
            'tool_use':fraction(tool_use,17),'search_calls':searches,'model_decisions':decisions,'citation_repairs':repairs,
            'original_query_metrics':raw['provisional_exact_evidence_metrics_on_completed_answerable_cases'],
            'median_uncached_end_to_end_excluding_pacing_ms':raw['median_uncached_end_to_end_excluding_pacing_ms'],
            'uncached_latency_cases':raw['uncached_latency_cases']}
    return {'review_authorship':'AI-assisted; human confirmation tracked separately',
        'configurations':out,'human_confirmation_pending_rows':human_pending,
        'configuration_selected':False,'test_run':False,
        'selection_status':'pending reviewed judgments and cost-tier confirmation; no final lock created'}


def check(bundle,review_path,identity_path):
    reviews = read_json(review_path)
    identity = read_json(identity_path)
    with zipfile.ZipFile(bundle) as archive:
        traces = json.loads(archive.read('agent_traces.json'))
        summary = json.loads(archive.read('agent_summary.json'))
        experiment = json.loads(archive.read('experiment.json'))
        require(len(traces)==68 and len({(t['config_id'],t['query_id']) for t in traces})==68,'Expected 68 unique completed dev traces.')
        require(object_hash(traces)==identity['traces_content_sha256'],'Audit does not match this completed dev bundle.')
        require(experiment['identity_sha256']==object_hash(experiment['identity'])==identity['experiment_identity_sha256'],'Experiment identity mismatch.')
        require(summary['status']=='complete_pending_semantic_audit' and summary['test_run'] is False and summary['configuration_selected'] is False,'Wrong evaluation stage.')
        model_ids = set()
        for t in traces:
            require(t['trace_sha256']==object_hash({k:v for k,v in t.items() if k!='trace_sha256'}),'Completed trace changed.')
            require(t['model_calls'][-1]['context_sent']==t['final_context'],'Final context differs from the final model input.')
            if t['evaluation_only']['answerable']:
                gold=t['evaluation_only']['gold']
                ranking=t['original_query_retrieval']['ranking']
                expected={'original_query_hit_at_1':bool(ranking and evidence_hit(ranking[0],gold)),
                    'original_query_evidence_hit_at_3':any(evidence_hit(c,gold) for c in ranking[:3]),
                    'actual_final_context_exact_evidence':any(evidence_hit(c,gold) for c in t['final_context']['selected_chunks'])}
                require(all(t['automatic_provisional'][k]==v for k,v in expected.items()), 'Recorded exact evidence metric differs from source text.')
            for cid in t['citations']:
                require(cid in t['final_context']['chunk_ids'] and '['+cid+']' in t['answer'],'Citation/context membership or inline format invalid.')
            for call in t['model_calls']:
                model_ids.add(call['request_id'])
                record = json.loads(archive.read('api_cache/'+call['request_id']+'.json'))
                require(record['record_sha256']==object_hash({k:v for k,v in record.items() if k!='record_sha256'}),'API cache changed.')
                require(record['request_sha256']==object_hash(record['request'])==call['request_sha256'],'API request changed.')
                require(record['response'].get('modelVersion')=='gemini-3.5-flash-lite','Mixed or unexpected model versions.')
                state = json.loads(record['request']['contents'][0]['parts'][0]['text'])
                require(set(state)=={'question','explicit_chapter_scope','search_limit'} and state['question']==t['public_query']['question'] and state['explicit_chapter_scope']==t['public_query']['filter'],'Question/scope changed or evaluation labels entered the controller state.')
                sent = [p['functionResponse']['response'] for c in record['request']['contents'] for p in c['parts'] if 'functionResponse' in p and 'context' in p['functionResponse']['response']]
                if call['context_sent']['chunk_ids']:
                    require(len(sent)==1 and sent[0]['context']==call['context_sent']['text'] and sent[0]['context_ids']==call['context_sent']['chunk_ids'],'Actual request context is duplicated or differs from trace.')
                else:
                    require(not sent,'Initial model request unexpectedly contains source context.')
            for event in t['events']:
                require(event['filter']==t['public_query']['filter'],'A rewritten query changed the explicit scope.')
                if event['filter'] is not None:
                    require(all(h['metadata']['chapter']==event['filter']['chapter'] for h in event['ranking']),'Search result violated chapter scope.')
        cache_ids = {Path(n).stem for n in archive.namelist() if n.startswith('api_cache/') and n.endswith('.json')}
        require(model_ids==cache_ids and len(model_ids)==175,'Missing or extra model response checkpoint.')
        for config in CONFIGS:
            positive=[t for t in traces if t['config_id']==config and t['evaluation_only']['answerable']]
            for metric in ['original_query_hit_at_1','original_query_evidence_hit_at_3','actual_final_context_exact_evidence']:
                measured=fraction(sum(t['automatic_provisional'][metric] for t in positive),15)
                require(summary['configurations'][config]['provisional_exact_evidence_metrics_on_completed_answerable_cases'][metric]==measured, 'Raw summary differs from exact-evidence traces.')
        migration = json.loads(archive.read('recovery/migration.json'))
        require(migration['migration_sha256']==object_hash({k:v for k,v in migration.items() if k!='migration_sha256'}),'Recovery record changed.')
        require(migration['new_code_sha256']==experiment['identity']['code_sha256'] and migration['prior_completed_cases']==35,'Wrong recovery provenance.')
        for relative,digest in migration['preserved_completed_case_files'].items():
            require(hashlib.sha256(archive.read(relative)).hexdigest()==digest,'A preserved pre-recovery completed case changed.')
    return aggregate(traces,reviews,summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,default=ROOT/'results/agent_dev_bundle.zip')
    parser.add_argument('--reviews',type=Path,default=ROOT/'results/ai_review/dev_agent_review.json')
    parser.add_argument('--identity',type=Path,default=ROOT/'results/ai_review/dev_audit_identity.json')
    args = parser.parse_args()
    try:
        summary = check(args.bundle,args.reviews,args.identity)
        print('Dev audit evidence check OK: 68 traces; 175 saved model responses; 35 preserved recovery cases.')
        for config,m in summary['configurations'].items():
            print(f"{config}: grounded mean {m['mean_grounded_answer_grade']:.3f}/2 | full answers {m['fully_correct_answerable_cases']['numerator']}/15 | reviewed context {m['reviewed_actual_final_context_evidence']['numerator']}/15 | correct abstentions {m['correct_abstentions']['numerator']}/2")
        print(f"AI-assisted judgments. Human confirmations pending: {summary['human_confirmation_pending_rows']}/68.")
        print('No API, embedding, retrieval, winner selection or test run performed.')
        return 0
    except Exception as exc:
        print('Audit check stopped: '+str(exc))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
