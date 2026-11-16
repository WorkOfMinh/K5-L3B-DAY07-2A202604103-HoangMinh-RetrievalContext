"""Display normalization and migration tests; no model/store/network calls."""
from pathlib import Path
import shutil
import tempfile
import unittest
from rag7.agent_runtime import validate_final
from scripts.recover_day7_abstention import (recover,code_files,folder_files,
    object_hash,read_json,save_json,SOURCE_NAME,TARGET_NAME)


class AbstentionDisplayTests(unittest.TestCase):
    def test_blank_explicit_abstention_uses_original_reason_without_changing_decision(self):
        args={'answer':'','abstain':True,'citations':[], 'reason':'The supplied context does not establish the requested value.'}
        result=validate_final(args,{'chunk_ids':[]})
        self.assertEqual(result['status'],'abstain')
        self.assertEqual(result['answer'],args['reason'])
        self.assertEqual(result['raw_model_answer'],'')
        self.assertEqual(result['citations'],[])
        self.assertEqual(result['reason'],args['reason'])
        self.assertEqual(args['answer'],'')

    def test_blank_factual_answer_is_still_an_error(self):
        with self.assertRaisesRegex(ValueError,'Empty or invalid final answer'):
            validate_final({'answer':'','abstain':False,'citations':[], 'reason':'A reason is not a factual answer.'},{'chunk_ids':[]})

    def test_empty_abstention_and_empty_reason_are_still_an_error(self):
        with self.assertRaisesRegex(ValueError,'nonempty answer or reason'):
            validate_final({'answer':'  ','abstain':True,'citations':[], 'reason':'  '},{'chunk_ids':[]})


class RecoveryMigrationTests(unittest.TestCase):
    def fixture(self,root):
        (root/'rag7').mkdir()
        (root/'rag7/__init__.py').write_text('',encoding='utf-8')
        (root/'rag7/agent_runtime.py').write_text('# synthetic old runtime\n',encoding='utf-8')
        (root/'patches').mkdir()
        patch=Path(__file__).resolve().parents[1]/'patches/agent_runtime_empty_abstention.py'
        shutil.copyfile(patch,root/'patches/agent_runtime_empty_abstention.py')
        baseline=object_hash(code_files(root))
        source=root/'results/agent_runs'/SOURCE_NAME
        identity={'code_sha256':baseline,'mode':'dev','provider_model':'synthetic_fixture'}
        save_json(source/'experiment.json',{'identity':identity,'identity_sha256':object_hash(identity),'created_at':'fixture'})
        trace={'query_id':'D01','config_id':'dense_base','status':'answered','answer':'synthetic fixture'}
        trace['trace_sha256']=object_hash(trace)
        save_json(source/'cases/dense_base_D01.json',trace)
        save_json(source/'agent_traces.json',[trace])
        (source/'api_cache').mkdir()
        return source,baseline

    def test_continuation_preserves_original_cases_cache_and_identity_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,baseline=self.fixture(root)
            before=folder_files(source)
            migration=recover(root,_baseline=baseline)
            target=root/'results/agent_runs'/TARGET_NAME
            self.assertEqual(folder_files(source),before)
            self.assertEqual((source/'cases/dense_base_D01.json').read_bytes(),(target/'cases/dense_base_D01.json').read_bytes())
            old=read_json(source/'experiment.json')['identity']
            new=read_json(target/'experiment.json')['identity']
            self.assertEqual({k:v for k,v in old.items() if k!='code_sha256'},{k:v for k,v in new.items() if k!='code_sha256'})
            self.assertNotEqual(old['code_sha256'],new['code_sha256'])
            self.assertEqual(migration['prior_completed_cases'],1)
            self.assertEqual(read_json(target/'recovery/original_experiment.json'),read_json(source/'experiment.json'))
            self.assertEqual(object_hash(code_files(root)),new['code_sha256'])

    def test_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,baseline=self.fixture(root)
            first=recover(root,_baseline=baseline)
            target=root/'results/agent_runs'/TARGET_NAME
            before=folder_files(target)
            second=recover(root,_baseline=baseline)
            self.assertEqual(first,second)
            self.assertEqual(folder_files(target),before)

    def test_mismatched_frozen_code_stops_before_creating_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,baseline=self.fixture(root)
            (root/'rag7/agent_runtime.py').write_text('# changed unexpectedly\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'do not match'):
                recover(root,_baseline=baseline)
            self.assertFalse((root/'results/agent_runs'/TARGET_NAME).exists())


if __name__=='__main__':unittest.main()
