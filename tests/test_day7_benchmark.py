"""Synthetic validation checks, not retrieval/model benchmark scores."""
import json
import unittest

from scripts.validate_day7_benchmark import query_view,count_cases,validate_case,object_hash


class Day7BenchmarkTests(unittest.TestCase):
    def fixture(self):
        p={('doc',1):{'text':'A complete statement.','chapter':6}}
        chunks=[dict(id='c',source_id='doc',page=1,text='A complete statement.')]
        q=dict(id='fixture',split='dev',question='Within chapter 6, what is stated?',answerable=True,
               expected_answer='A complete statement.',gold=[dict(source_id='doc',page=1,evidence='A complete statement.')],
               filter={'chapter':6})
        return p,chunks,q

    def test_query_view_excludes_evaluation_labels(self):
        _,_,q=self.fixture();public=query_view(q)
        self.assertEqual(set(public),{'id','question','filter'})
        self.assertNotIn('answerable',public)
        self.assertNotIn('expected_answer',public)
        self.assertNotIn('gold',public)

    def test_split_and_answerability_counts_are_separate(self):
        cases=[dict(split='dev',answerable=True),dict(split='dev',answerable=False),
               dict(split='test',answerable=True),dict(split='test',answerable=True)]
        self.assertEqual(count_cases(cases),{'dev':{'answerable':1,'insufficient':1},
                                           'test':{'answerable':2,'insufficient':0}})

    def test_positive_evidence_and_explicit_filter_required(self):
        p,chunks,q=self.fixture();self.assertEqual(validate_case(q,p,chunks),['c'])
        with self.assertRaises(ValueError):validate_case(dict(q,question='No scope supplied'),p,chunks)
        with self.assertRaises(ValueError):validate_case(dict(q,gold=[dict(source_id='doc',page=1,evidence='Missing')]),p,chunks)

    def test_insufficient_case_needs_rationale_and_no_positive_gold(self):
        p,chunks,q=self.fixture()
        absent=dict(q,answerable=False,gold=[],expected_behavior='search_and_abstain',absence_reason='Not specified in selected source.')
        self.assertEqual(validate_case(absent,p,chunks),[])
        with self.assertRaises(ValueError):validate_case(dict(absent,gold=q['gold']),p,chunks)
        with self.assertRaises(ValueError):validate_case(dict(absent,absence_reason=''),p,chunks)

    def test_json_formatting_tolerance_preserves_text_changes(self):
        data={'question':'Same words','gold':[]}
        self.assertEqual(object_hash(data),object_hash(json.loads(json.dumps(data,indent=2).replace('\n','\r\n'))))
        self.assertNotEqual(object_hash(data),object_hash(dict(data,question='Changed words')))


if __name__=='__main__':unittest.main()
