"""Synthetic transport/controller contract tests. Never reported as API quality."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from rag7.agent_runtime import run_agent, request_payload, validate_final
from rag7.agent_dev import bind_experiment, build_review_template, seal_result, load_result
from rag7 import agent_dev
from rag7.common import object_hash, save_json
from rag7.gemini_provider import GeminiClient, SessionPaused, ProviderFailure, redact
from rag7.retrievers import pack_context


CID = 'a' * 64
OTHER = 'b' * 64
PUBLIC = {'id':'fixture', 'question':'Which method adds one item?', 'filter':{'chapter':5}}
CONFIG = {'id':'fixture_config', 'method':'dense', 'max_searches':2, 'context_chunks':3, 'budget_chars':3500}
PROVIDER = {'model':'gemini-3.8-flash', 'endpoint':'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent'}
GENERATION = {'temperature':0, 'thinkingConfig':{'thinkingLevel':'low'}}


def chunk(cid=CID, text='Use list.append(x).'):
    return {'id':cid,'source':'fixture.pdf','source_id':'fixture','page':1,'text':text,
        'metadata':{'chapter':5},'start_char':0,'end_char':len(text),'rank':1,'score':1}


def action(name,args,signature='opaque-signature'):
    return {'modelVersion':'fixture-model-version','responseId':'fixture-response',
        'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':5,'totalTokenCount':15},
        'candidates':[{'finishReason':'STOP','content':{'role':'model','parts':[
            {'functionCall':{'name':name,'args':args},'thoughtSignature':signature}]}}]}


def finish(answer=None,citations=None,abstain=False):
    return action('finalize',{'answer':answer or ('Use list.append(x). ['+CID+']'),
        'citations':([CID] if citations is None else citations),'abstain':abstain,'reason':'Fixture evidence.'})


class ControllerFixture:
    def __init__(self,responses):
        self.responses=iter(responses);self.pacing_seconds=0;self.payloads=[]
    def generate(self,payload,namespace):
        self.payloads.append(payload)
        response=next(self.responses)
        if isinstance(response,Exception):raise response
        return {'request_id':object_hash(payload),'request_sha256':object_hash(payload),
                'response':response,'network_seconds':.01},False


def search(query,filters):
    return {'ranking':[chunk()],'components':{'dense':[chunk()]}}


class ClockFixture:
    def __init__(self):self.now=0;self.waits=[]
    def clock(self):return self.now
    def sleep(self,seconds):self.now+=seconds;self.waits.append(seconds)


class GeminiAgentTests(unittest.TestCase):
    def test_native_search_then_cited_answer_preserves_signature_and_scope(self):
        client=ControllerFixture([action('search',{'query':'list.append'}),finish()])
        calls=[]
        def scoped(query,filters):calls.append((query,filters));return search(query,filters)
        t=run_agent(PUBLIC,CONFIG,GENERATION,client,scoped)
        self.assertEqual(t['status'],'answered')
        self.assertEqual(calls,[('list.append',{'chapter':5})])
        payload=client.payloads[1]
        self.assertEqual(payload['contents'][1]['parts'][0]['thoughtSignature'],'opaque-signature')
        self.assertIn(CID,payload['contents'][2]['parts'][0]['functionResponse']['response']['context_ids'])
        self.assertEqual(t['model_calls'][-1]['context_sent'],t['final_context'])

    def test_gold_fields_cannot_enter_controller(self):
        for field in ['gold','answerable','expected_answer','absence_reason']:
            with self.assertRaises(ValueError):
                request_payload({**PUBLIC,field:'secret-label'},CONFIG,GENERATION,[],{},None)

    def test_search_budget_forces_finalization_and_no_extra_tool_execution(self):
        client=ControllerFixture([action('search',{'query':'one'}),action('search',{'query':'two'}),action('search',{'query':'three'})])
        calls=[]
        def counted(query,filters):calls.append(query);return search(query,filters)
        with self.assertRaises(ValueError):run_agent(PUBLIC,CONFIG,GENERATION,client,counted)
        self.assertEqual(calls,['one','two'])
        self.assertEqual(client.payloads[-1]['toolConfig']['functionCallingConfig']['allowedFunctionNames'],['finalize'])

    def test_rewritten_queries_use_one_bounded_latest_context(self):
        client=ControllerFixture([action('search',{'query':'one'},signature='first-signature'),action('search',{'query':'two'},signature='second-signature'),finish()])
        t=run_agent(PUBLIC,CONFIG,GENERATION,client,search)
        payload=client.payloads[-1]
        self.assertEqual(len(payload['contents']),5)
        self.assertEqual(payload['contents'][1]['parts'][0]['thoughtSignature'],'first-signature')
        self.assertEqual(payload['contents'][3]['parts'][0]['thoughtSignature'],'second-signature')
        self.assertNotIn('context',payload['contents'][2]['parts'][0]['functionResponse']['response'])
        tool=payload['contents'][-1]['parts'][0]['functionResponse']['response']
        self.assertEqual(tool['queries'],['one','two'])
        self.assertEqual(tool['context_ids'],[CID])
        self.assertLessEqual(len(tool['context']),3500)
        self.assertEqual(t['search_calls'],2)

    def test_lite_config_uses_supported_thinking_and_output_settings(self):
        config=json.loads((Path(__file__).parents[1]/'config/agent.json').read_text(encoding='utf-8'))
        self.assertEqual(config['model'],'gemini-3.5-flash-lite')
        self.assertTrue(config['endpoint'].endswith('/gemini-3.5-flash-lite:generateContent'))
        self.assertEqual(config['generation']['thinkingConfig'],{'thinkingLevel':'low'})
        self.assertEqual(config['generation']['maxOutputTokens'],4096)
        self.assertNotIn('thinkingBudget',config['generation']['thinkingConfig'])
        self.assertNotIn('temperature',config['generation'])

    def test_invalid_or_unlisted_citations_trigger_guard_not_success(self):
        context=pack_context([chunk()])
        for answer,citations in [('Fact. ['+OTHER+']',[OTHER]),('Fact.',[CID]),
                                 ('Fact. ['+CID+'] ['+OTHER+']',[CID])]:
            args={'answer':answer,'citations':citations,'abstain':False,'reason':'fixture'}
            self.assertEqual(validate_final(args,context)['guard'],'invalid_citations')

    def test_empty_context_and_explicit_abstention_are_separate(self):
        client=ControllerFixture([action('search',{'query':'missing'}),finish()])
        t=run_agent(PUBLIC,CONFIG,GENERATION,client,lambda q,f:{'ranking':[],'components':{}})
        self.assertEqual(t['guard'],'invalid_citations')
        client=ControllerFixture([action('search',{'query':'missing'}),finish('The corpus does not establish this.',[],True)])
        t=run_agent(PUBLIC,CONFIG,GENERATION,client,search)
        self.assertEqual(t['guard'],'model_abstention')

    def test_provider_and_tool_errors_propagate(self):
        with self.assertRaises(ProviderFailure):
            run_agent(PUBLIC,CONFIG,GENERATION,ControllerFixture([ProviderFailure('fixture')]),search)
        client=ControllerFixture([action('search',{'query':'x'})])
        def broken(q,f):raise RuntimeError('database fixture error')
        with self.assertRaises(RuntimeError):run_agent(PUBLIC,CONFIG,GENERATION,client,broken)

    def test_cached_response_resumes_without_new_network_or_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def transport(*args):calls.append(args);return 200,{},json.dumps(action('search',{'query':'x'}))
            c=GeminiClient('fixture-key',PROVIDER,tmp,min_interval=0,transport=transport)
            a,cached=c.generate({'contents':[]},namespace='one')
            b,cached2=c.generate({'contents':[]},namespace='one')
            self.assertFalse(cached);self.assertTrue(cached2)
            self.assertEqual(a,b);self.assertEqual(len(calls),1)
            self.assertEqual(c.new_calls,1)

    def test_transient_429_retry_and_daily_quota_stop_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=ClockFixture()
            replies=iter([(429,{'Retry-After':'12'},json.dumps({'error':{'message':'minute quota'}})),
                (200,{},json.dumps(action('search',{'query':'x'})))])
            c=GeminiClient('key',PROVIDER,tmp,min_interval=0,transport=lambda *a:next(replies),sleep=clock.sleep,clock=clock.clock)
            c.generate({},namespace='one')
            self.assertEqual(c.new_calls,2);self.assertEqual(clock.waits,[12])
        with tempfile.TemporaryDirectory() as tmp:
            c=GeminiClient('key',PROVIDER,tmp,min_interval=0,transport=lambda *a:(429,{},json.dumps({'error':{'message':'RequestsPerDay limit: 0'}})))
            with self.assertRaises(SessionPaused):c.generate({},namespace='one')
            self.assertEqual(c.new_calls,1)

    def test_budget_blocks_network_and_no_model_switch_on_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def transport(*a):calls.append(a);return 404,{},json.dumps({'error':{'message':'model missing'}})
            c=GeminiClient('key',PROVIDER,tmp,min_interval=0,max_calls=1,transport=transport)
            with self.assertRaises(ProviderFailure):c.generate({},namespace='one')
            with self.assertRaises(SessionPaused):c.generate({},namespace='two')
            self.assertEqual(len(calls),1)

    def test_pacing_secret_redaction_and_resolved_version_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=ClockFixture()
            c=GeminiClient('fixture-secret',PROVIDER,tmp,min_interval=15,sleep=clock.sleep,clock=clock.clock,
                transport=lambda *a:(200,{},json.dumps(action('search',{'query':'x'}))))
            c.generate({'a':1},namespace='one');c.generate({'a':2},namespace='two')
            self.assertEqual(clock.waits,[15]);self.assertEqual(c.pacing_seconds,15)
            with self.assertRaises(ValueError):c._check_version({'modelVersion':'changed'})
            self.assertNotIn('fixture-secret',redact('error fixture-secret',['fixture-secret']))

    def test_experiment_and_completed_trace_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bind_experiment(tmp,{'model':'one'})
            with self.assertRaises(ValueError):bind_experiment(tmp,{'model':'two'})
            path=Path(tmp)/'case.json';save_json(path,seal_result({'status':'answered'}))
            self.assertEqual(load_result(path)['status'],'answered')
            value=json.loads(path.read_text(encoding='utf-8'));value['status']='abstain';save_json(path,value)
            with self.assertRaises(ValueError):load_result(path)

    def test_review_template_stays_blank_and_preserves_later_human_labels(self):
        result={'config_id':'fixture','query_id':'Q','trace_sha256':'hash','public_query':PUBLIC,
            'evaluation_only':{'answerable':True},'status':'answered','answer':'x','citations':[CID],
            'final_context':pack_context([chunk()])}
        rows=build_review_template([result])
        self.assertIsNone(rows[0]['review']['answer_grade'])
        self.assertEqual(rows[0]['review']['citation_support'],[None])
        rows[0]['review']['answer_grade']=2
        self.assertEqual(build_review_template([result],rows)[0]['review']['answer_grade'],2)

    def test_smoke_driver_writes_real_context_audit_and_resumes_completed_case(self):
        # End-to-end driver contract using explicitly synthetic HTTP/store fixtures.
        # These responses are never included as real benchmark results.
        config=json.loads((Path(__file__).parents[1]/'config/agent.json').read_text(encoding='utf-8'))
        retrieval={'bm25':{'k1':1.2,'b':.75},'rrf':{'rank_constant':60}}
        public={**PUBLIC,'id':'D01'}
        case={**public,'answerable':True,'gold':[{'source_id':'fixture','page':1,'evidence':'Use list.append(x).'}]}
        manifest={'model':{'signature':'fixture'},'import_identity':{'db_digest':'fixture'}}
        lock={'seal_sha256':'fixture','source_content':{'fixture':True}}
        replies=iter([action('search',{'query':'list.append'}),finish()])
        net=[]
        class Store:
            def __init__(self,*a,**kw):pass
            def search(self,*a,**kw):return [chunk()]
            def verify_fresh_snapshot(self):return 'fixture'
            def close(self):pass
        class Encoder:
            def __init__(self,*a,**kw):pass
            def embed_query(self,q):return [1]+[0]*383
            def provenance(self):return {'synthetic_fixture':True}
        def client_factory(key,conf,folder,**kwargs):
            def transport(*a):net.append(a);return 200,{},json.dumps(next(replies))
            return GeminiClient(key,conf,folder,min_interval=0,max_calls=3,transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            args=SimpleNamespace(mode='smoke',day6_starter=Path(tmp),experiment=None,min_interval=0,max_api_calls=3,offline=True)
            with patch.multiple(agent_dev,ROOT=Path(tmp),load_agent_config=lambda:config,load_config=lambda:retrieval,
                    load_corpus=lambda:([chunk()],manifest,lock),load_dev=lambda l:([case],[public]),
                    read_credentials=lambda:('fixture-secret',config['model']),read_database_url=lambda p:'fixture-url',
                    code_hash=lambda:'fixture-code',DensePgStore=Store,LocalQueryEncoder=Encoder,GeminiClient=client_factory):
                self.assertEqual(agent_dev.run(args),0)
                folder=Path(tmp)/'results/agent_runs/gemini35lite_smoke_v5'
                rows=json.loads((folder/'agent_traces.json').read_text(encoding='utf-8'))
                self.assertTrue(rows[0]['automatic_provisional']['actual_final_context_exact_evidence'])
                self.assertEqual(rows[0]['status'],'answered')
                self.assertEqual(agent_dev.run(args),0)
                self.assertEqual(len(net),2)
                for p in folder.rglob('*.json'):
                    self.assertNotIn('fixture-secret',p.read_text(encoding='utf-8'))

    def test_transport_success_but_invalid_model_action_is_not_an_answer(self):
        invalid=action('search',{'query':'x'})
        invalid['candidates'][0]['finishReason']='MAX_TOKENS'
        client=ControllerFixture([invalid])
        with self.assertRaisesRegex(ValueError,'finish normally'):
            run_agent(PUBLIC,CONFIG,GENERATION,client,search)

    def test_configurable_timeout_is_forwarded_and_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            timeouts=[]
            def transport(endpoint,key,payload,timeout):
                timeouts.append(timeout)
                raise TimeoutError('The read operation timed out')
            client=GeminiClient('fixture-secret',PROVIDER,tmp,min_interval=0,
                                timeout_seconds=180,transport=transport)
            with self.assertRaisesRegex(ProviderFailure,'no automatic duplicate'):
                client.generate({},namespace='timeout-fixture')
            self.assertEqual(timeouts,[180])
            self.assertEqual(client.new_calls,1)
            events=[json.loads(line) for line in (Path(tmp)/'api_attempts.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(events[-1]['event'],'attempt_failed')
            self.assertEqual(events[-1]['socket_timeout_seconds'],180)
            self.assertIn('unknown',events[-1]['usage'])

    def test_timeout_bounds_and_successful_long_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            for timeout in [0,9,601,float('nan'),float('inf')]:
                with self.assertRaises(ValueError):
                    GeminiClient('fixture-secret',PROVIDER,tmp,timeout_seconds=timeout)
            captured=[]
            def transport(endpoint,key,payload,timeout):
                captured.append(timeout)
                return 200,{},json.dumps(action('search',{'query':'x'}))
            client=GeminiClient('fixture-secret',PROVIDER,tmp,timeout_seconds=300,transport=transport)
            client.generate({},namespace='long-timeout')
            self.assertEqual(captured,[300])

    def test_explicit_lite_profile_and_endpoint_model_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={'model':'gemini-3.5-flash-lite','endpoint':'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent'}
            client=GeminiClient('fixture-key',config,tmp,min_interval=0,
                transport=lambda *a:(200,{},json.dumps(action('search',{'query':'x'}))))
            client.generate({},namespace='lite-profile')
            self.assertEqual(client.new_calls,1)
            with self.assertRaises(ValueError):
                GeminiClient('fixture-key',{**config,'endpoint':PROVIDER['endpoint']},tmp)

    def test_missing_inline_citation_gets_one_real_model_repair_with_signed_history(self):
        client=ControllerFixture([action('search',{'query':'append'}),
            finish('Use list.append(x).'),finish()])
        t=run_agent(PUBLIC,CONFIG,GENERATION,client,search)
        self.assertEqual(t['status'],'answered')
        self.assertEqual(len(t['model_calls']),3)
        self.assertEqual(len(t['format_repairs']),1)
        self.assertEqual(t['format_repairs'][0]['rejected_proposal']['answer'],'Use list.append(x).')
        self.assertEqual(t['search_calls'],1)
        payload=client.payloads[-1]
        self.assertEqual(payload['toolConfig']['functionCallingConfig']['allowedFunctionNames'],['finalize'])
        self.assertEqual(payload['contents'][-2]['parts'][0]['functionCall']['name'],'finalize')
        self.assertEqual(payload['contents'][-2]['parts'][0]['thoughtSignature'],'opaque-signature')
        self.assertEqual(payload['contents'][-1]['parts'][0]['functionResponse']['name'],'finalize')
        contexts=[p['functionResponse']['response']['context'] for c in payload['contents']
            for p in c['parts'] if 'functionResponse' in p and 'context' in p['functionResponse']['response']]
        self.assertEqual(contexts,[t['final_context']['text']])

    def test_second_invalid_citation_proposal_still_abstains_without_invented_citations(self):
        client=ControllerFixture([action('search',{'query':'append'}),
            finish('Use list.append(x).'),finish('Use list.append(x).')])
        t=run_agent(PUBLIC,CONFIG,GENERATION,client,search)
        self.assertEqual(t['status'],'abstain')
        self.assertEqual(t['guard'],'invalid_citations')
        self.assertEqual(len(t['model_calls']),3)
        self.assertEqual(len(t['format_repairs']),1)
        self.assertEqual(t['citations'],[])


if __name__=='__main__':unittest.main()
