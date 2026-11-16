"""Check real submission evidence; missing test judgments are not auto-filled."""
import json
import zipfile
from rag7.common import ROOT,read_json,require,object_hash
from .selection import LOCK_PATH,verify_current
from .report import reviewed_metrics,git_value
from .test_run import load_test


def main():
    try:
        lock=read_json(LOCK_PATH);verify_current(lock)
        cases,public=load_test(lock)
        bundle=ROOT/'results/agent_test_bundle.zip'
        require(bundle.exists(),'Held-out test has not been run.')
        with zipfile.ZipFile(bundle) as z:
            traces=json.loads(z.read('test_traces.json'));test_lock=json.loads(z.read('selection_lock.json'))
            summary=json.loads(z.read('test_summary.json'))
            require(test_lock==lock and summary['selection_lock_seal']==lock['seal_sha256'],'Test selection lock mismatch.')
            require(len(traces)==14 and len({(t['config_id'],t['query_id']) for t in traces})==14,'Test comparison is incomplete.')
            require({t['config_id'] for t in traces}=={lock['baseline_id'],lock['selected_id']},'Test contains an unselected configuration.')
            for cid in [lock['baseline_id'],lock['selected_id']]:
                rows=[t for t in traces if t['config_id']==cid]
                require(len(rows)==7 and {t['query_id'] for t in rows}=={c['id'] for c in cases},'Wrong test question coverage.')
            for t in traces:
                require(t['trace_sha256']==object_hash({k:v for k,v in t.items() if k!='trace_sha256'}),'Test trace changed.')
                require(t['selection_lock_seal']==lock['seal_sha256'] and t['split']=='test','Test trace lacks the pre-test lock.')
                require(t['final_context']==t['model_calls'][-1]['context_sent'],'Test final context differs from model input.')
                for c in t['model_calls']:
                    r=json.loads(z.read('api_cache/'+c['request_id']+'.json'))
                    require(r['record_sha256']==object_hash({k:v for k,v in r.items() if k!='record_sha256'}),'Test API checkpoint changed.')
                    require(r['request_sha256']==object_hash(r['request'])==c['request_sha256'],'Test request changed.')
                    require(r['response'].get('modelVersion')==lock['resolved_model_version'],'Test used a different model version.')
        review_path=ROOT/'results/ai_review/test_agent_review.json'
        require(review_path.exists(),'Test source judgments are pending. Audit the actual answers/citations/abstentions; do not copy blank template labels as completed reviews.')
        metrics=reviewed_metrics(traces,read_json(review_path),5)
        require((ROOT/'report/individual/2A202604103_HoangMinh.md').is_file(),'Individual report missing.')
        print('Submission evidence check OK: locked dev selection, 7 test questions x 2 configurations, source judgments and individual report.')
        print('Review authorship remains AI-assisted; no human confirmation is invented.')
        origin=git_value(['remote','get-url','origin']);commit=git_value(['rev-parse','HEAD']);dirty=git_value(['status','--porcelain'])
        tracked=git_value(['ls-files']) or ''
        require(not any('/.env'==('/'+p).replace('\\','/')[-5:] or p.replace('\\','/').split('/')[-1]=='.env'
                        for p in tracked.splitlines()), 'A real .env is tracked by Git; remove it from the index before submission.')
        if not origin or 'VinUni-AI20k/K5-L3-Day07' in origin:
            print('Pending: set the personal repository origin, grant grader access and submit its URL.');return 2
        if dirty:
            print('Pending: commit the intended code/report/results before submission. Keep .env and model caches excluded.');return 2
        print('Repository: '+origin);print('Final commit: '+str(commit))
        print('Submit this personal repository link through the instructor\'s submission channel.')
        return 0
    except Exception as exc:
        print('Submission pending: '+str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
