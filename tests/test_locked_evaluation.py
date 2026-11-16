"""Frozen-selection and held-out-loader contracts using synthetic fixtures."""
from pathlib import Path
import tempfile
import unittest
import json
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from rag7.common import object_hash,save_json
from evaluation7.selection import choose,sealed,verify_seal
from evaluation7.test_run import load_test,test_summary as summarize_test
from evaluation7.report import reviewed_metrics
from evaluation7 import test_run
from rag7.gemini_provider import GeminiClient


def metrics(mean=2):
    return {'mean_grounded_answer_grade':mean,'correct_abstentions':{'numerator':2},
        'supported_citation_coverage_on_answerable':{'rate':1},'reviewed_actual_final_context_evidence':{'rate':1},
        'original_query_metrics':{'original_query_evidence_hit_at_3':{'rate':1}}}


def lock_for(hashes=None):
    return sealed({'stage':'selected_on_dev_before_test','baseline_id':'dense_base','selected_id':'bm25_base',
                   'benchmark_content_hashes':hashes or {}})


def synthetic_cases():
    return [{'id':f'F{i}','split':'test','question':'Synthetic fixture '+str(i),'filter':None,
             'answerable':i<5,'gold':[]} for i in range(7)]


class LockedEvaluationTests(unittest.TestCase):
    def test_unique_primary_winner_does_not_require_invented_cost(self):
        winner,path=choose({'bm25_base':metrics(2),'hybrid_base':metrics(29/15),'dense_base':metrics(28/15)})
        self.assertEqual(winner,'bm25_base');self.assertEqual(len(path),1)
        self.assertEqual(path[0]['criterion'],'mean_grounded_answer_grade')

    def test_quality_tie_cannot_treat_missing_cost_as_zero(self):
        with self.assertRaisesRegex(ValueError,'cost values are missing'):
            choose({'a':metrics(),'b':metrics()})

    def test_declared_cost_latency_and_final_tie_order(self):
        self.assertEqual(choose({'a':metrics(),'b':metrics()},costs={'a':2,'b':1})[0],'b')
        self.assertEqual(choose({'a':metrics(),'b':metrics()},costs={'a':0,'b':0},wall_times={'a':3,'b':2})[0],'b')
        self.assertEqual(choose({'b':metrics(),'a':metrics()},costs={'a':0,'b':0},wall_times={'a':2,'b':2})[0],'a')

    def test_lock_tampering_is_rejected(self):
        lock=lock_for();verify_seal(lock);lock['selected_id']='hybrid_base'
        with self.assertRaises(ValueError):verify_seal(lock)

    def test_invalid_lock_fails_before_test_file_is_opened(self):
        with patch('evaluation7.test_run.read_json',side_effect=AssertionError('Held-out file was opened')) as reader:
            with self.assertRaises(ValueError):load_test({},Path('unused-fixture-path'))
            reader.assert_not_called()

    def test_only_public_question_scope_view_reaches_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);cases=synthetic_cases();public=[{k:c[k] for k in ['id','question','filter']} for c in cases]
            save_json(folder/'test.json',cases);save_json(folder/'test_queries.json',public)
            lock=lock_for({'test.json':object_hash(cases),'test_queries.json':object_hash(public)})
            loaded,view=load_test(lock,folder)
            self.assertEqual(len(loaded),7);self.assertEqual(sum(c['answerable'] for c in loaded),5)
            self.assertTrue(all(set(q)=={'id','question','filter'} for q in view))
            public[0]['answerable']=True;save_json(folder/'test_queries.json',public)
            lock=lock_for({'test.json':object_hash(cases),'test_queries.json':object_hash(public)})
            with self.assertRaisesRegex(ValueError,'labels'):load_test(lock,folder)

    def test_test_metric_denominators_exclude_insufficient_cases(self):
        rows=[]
        for case in synthetic_cases():
            pos=case['answerable']
            rows.append({'config_id':'dense_base','evaluation_only':{'answerable':pos},
                'automatic_provisional':{k:(True if pos else None) for k in ['original_query_hit_at_1','original_query_evidence_hit_at_3','actual_final_context_exact_evidence']},
                'status':'answered' if pos else 'abstain','search_calls':1,'model_calls':[],
                'timing':{'wall_seconds_this_invocation':2,'wall_seconds_excluding_pacing':1,'reused_api_checkpoints':False}})
        with tempfile.TemporaryDirectory() as tmp:
            summary=summarize_test(rows,[{'id':'dense_base'}],tmp)['configurations']['dense_base']
            self.assertEqual(summary['provisional_exact_metrics']['original_query_hit_at_1']['denominator'],5)
            self.assertEqual(summary['completed_insufficient'],2)
            self.assertIsNone(summary['grounded_answer_grade'])

    def test_semantic_report_rejects_false_abstention_grade_and_blank_support(self):
        traces=[];reviews=[]
        for case in synthetic_cases():
            pos=case['answerable'];cid='a'*64
            t={'config_id':'dense_base','query_id':case['id'],'trace_sha256':'fixture-'+case['id'],
               'answer':'fixture','status':'answered' if pos else 'abstain',
               'citations':[cid] if pos else [],'final_context':{'fixture':True},'evaluation_only':{'answerable':pos}}
            r={k:t[k] for k in ['config_id','query_id','trace_sha256','answer']}
            r['actual_final_context']=t['final_context'];r['review']={'answer_grade':2 if pos else None,
                'citation_support':[True] if pos else [],'context_evidence_supported':True if pos else None,
                'abstention_correct':None if pos else True,'reason':'Synthetic fixture.'}
            traces.append(t);reviews.append(r)
        self.assertEqual(reviewed_metrics(traces,reviews,5)['dense_base']['mean'],2)
        traces[0]['status']='abstain'
        with self.assertRaisesRegex(ValueError,'grade 0'):reviewed_metrics(traces,reviews,5)

    def test_locked_driver_completes_and_resumes_synthetic_fourteen_case_run(self):
        # Only synthetic questions, HTTP responses and store data are used here.
        config=json.loads((Path(__file__).parents[1]/'config/agent.json').read_text(encoding='utf-8'))
        cases=synthetic_cases();public=[{k:c[k] for k in ['id','question','filter']} for c in cases]
        cid='a'*64
        chunk={'id':cid,'source_id':'fixture','source':'fixture.pdf','page':1,'text':'alpha',
            'metadata':{'chapter':5},'start_char':0,'end_char':5,'rank':1,'score':1}
        lock=sealed({'stage':'selected_on_dev_before_test','baseline_id':'dense_base','selected_id':'bm25_base',
            'agent_config':config,'requested_model':config['model'],'resolved_model_version':'fixture-version',
            'retrieval_config':{'bm25':{'k1':1.2,'b':.75},'rrf':{'rank_constant':60}}})
        calls=[]
        class Store:
            def __init__(self,*a,**kw):pass
            def search(self,*a,**kw):return [chunk]
            def verify_fresh_snapshot(self):pass
            def close(self):pass
        class Encoder:
            def __init__(self,*a,**kw):pass
            def embed_query(self,q):return [1]+[0]*383
            def provenance(self):return {'synthetic_fixture':True}
        def factory(key,conf,folder,**kwargs):
            def transport(endpoint,key,payload,timeout):
                calls.append(payload)
                first=payload['toolConfig']['functionCallingConfig']['allowedFunctionNames']==['search']
                call={'name':'search','args':{'query':'alpha'}} if first else {'name':'finalize','args':{
                    'answer':'Synthetic alpha ['+cid+']','citations':[cid],'abstain':False,'reason':'Synthetic fixture.'}}
                response={'modelVersion':'fixture-version','usageMetadata':{'promptTokenCount':1,'candidatesTokenCount':1,'totalTokenCount':2},
                    'candidates':[{'finishReason':'STOP','content':{'role':'model','parts':[{'functionCall':call,'thoughtSignature':'fixture'}]}}]}
                return 200,{},json.dumps(response)
            return GeminiClient(key,conf,folder,min_interval=0,max_calls=100,transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'selection.json';save_json(path,lock)
            args=SimpleNamespace(day6_starter=root,max_api_calls=100,api_timeout=180,min_interval=0,offline=True)
            with patch.multiple(test_run,ROOT=root,LOCK_PATH=path,verify_current=lambda l:([chunk],{'fixture':True},{'fixture':True}),
                    load_test=lambda l:(cases,public),read_credentials=lambda:('fixture-secret',config['model']),
                    read_database_url=lambda p:'fixture-url',GeminiClient=factory,DensePgStore=Store,LocalQueryEncoder=Encoder),redirect_stdout(StringIO()):
                self.assertEqual(test_run.run(args),0)
                self.assertEqual(len(calls),28)
                summary=json.loads((root/'results/test_runs/locked_test_v1/test_summary.json').read_text(encoding='utf-8'))
                self.assertEqual(summary['status'],'complete_pending_semantic_audit')
                self.assertFalse(summary['configuration_reselected'])
                self.assertEqual(test_run.run(args),0)
                self.assertEqual(len(calls),28)
