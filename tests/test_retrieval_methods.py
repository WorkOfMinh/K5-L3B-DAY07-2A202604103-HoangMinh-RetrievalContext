"""Synthetic algorithm/contract tests; fixtures are not measured retrieval benchmarks."""
import math
import unittest
from rag7.retrievers import BM25Index,RetrievalEngine,pack_context,reciprocal_rank_fusion
from rag7.dense_backend import vector_literal
from rag7.retrieval_dev import evidence_hit,mark_result,summarize_retrieval


def chunk(id,text='alpha',chapter=5,page=1):
    return dict(id=id,source_id='doc',source='source.pdf',page=page,text=text,
        metadata={'chapter':chapter},start_char=0,end_char=len(text),local_source_path='source.pdf')


def ranked(c,score=1,rank=1):return {**c,'score':score,'rank':rank}


class RetrievalMethodTests(unittest.TestCase):
    def test_bm25_matches_independent_term_formula(self):
        index=BM25Index([chunk('a','alpha alpha'),chunk('b','beta beta')])
        hits=index.search('alpha')
        expected=math.log(1+(2-1+.5)/(1+.5))*2*(1.2+1)/(2+1.2)
        self.assertEqual([h['id'] for h in hits],['a'])
        self.assertAlmostEqual(hits[0]['score'],expected,places=12)

    def test_bm25_identifier_and_scope_handling(self):
        index=BM25Index([chunk('a','__all__ alpha',5),chunk('b','__all__ alpha',6),
                         chunk('c','__name__ beta',6)])
        self.assertEqual([h['id'] for h in index.search('__all__',filters={'chapter':6})],['b'])
        self.assertEqual(index.search('__all__',filters={'chapter':99}),[])

    def test_bm25_empty_or_unseen_query_and_invalid_filter(self):
        index=BM25Index([chunk('a')])
        self.assertEqual(index.search(''),[])
        self.assertEqual(index.search('never_seen_token'),[])
        with self.assertRaises(ValueError):index.search('alpha',filters={'chapter':True})
        with self.assertRaises(ValueError):index.search('alpha',top_k=0)

    def test_rrf_uses_ranks_not_raw_score_scales(self):
        a,b,c=chunk('a'),chunk('b'),chunk('c')
        component={'dense':[ranked(a,100000),ranked(b,90000,2)],
                   'bm25':[ranked(b,.001),ranked(c,.0001,2)]}
        hits=reciprocal_rank_fusion(component)
        self.assertEqual([h['id'] for h in hits],['b','a','c'])
        self.assertAlmostEqual(hits[0]['score'],1/61+1/62)
        self.assertEqual(hits[0]['component_ranks'],{'dense':2,'bm25':1})

    def test_rrf_rejects_duplicate_component_ids(self):
        a=ranked(chunk('a'))
        with self.assertRaises(ValueError):reciprocal_rank_fusion({'dense':[a,a]})

    def test_context_budget_dedup_and_source_text_preservation(self):
        a=ranked(chunk('a','if True:\n    alpha()'))
        b=ranked(chunk('b','beta'))
        empty=pack_context([a],budget_chars=1)
        self.assertEqual(empty['text'],'')
        self.assertEqual(empty['excluded'][0]['reason'],'budget')
        context=pack_context([a,a,b],budget_chars=500,max_chunks=2)
        self.assertEqual(context['chunk_ids'],['a','b'])
        self.assertIn('if True:\n    alpha()',context['text'])
        self.assertLessEqual(context['characters'],500)
        self.assertEqual(context['excluded'][0]['reason'],'duplicate_id')

    def test_engine_rejects_missing_dense_and_scope_violation(self):
        a=chunk('a',chapter=5);lex=BM25Index([a])
        engine=RetrievalEngine([a],lex,None)
        with self.assertRaises(ValueError):engine.search('dense','alpha')
        class WrongScopeFixture:
            def search(self,*args,**kwargs):return [ranked(a)]
        engine=RetrievalEngine([a],lex,WrongScopeFixture())
        with self.assertRaises(ValueError):engine.search('dense','alpha',filters={'chapter':6},query_vector=[1]+[0]*383)

    def test_vector_shape_normalization_and_finite_values(self):
        self.assertTrue(vector_literal([1.0]+[0.0]*383).startswith('[1,'))
        for vector in [[1],[0]*384,[float('nan')]+[0]*383]:
            with self.assertRaises(ValueError):vector_literal(vector)

    def test_metrics_exclude_insufficient_cases_and_do_not_infer_agent_results(self):
        traces=[];hit=ranked(chunk('a','alpha'))
        result={'ranking':[hit],'components':{},'method':'dense'}
        context=pack_context([hit])
        for method in ['dense','bm25','hybrid_rrf']:
            for i in range(17):
                q=dict(id=f'fixture-{i}',split='dev',question='alpha',filter=None,answerable=i<15,
                       gold=[{'source_id':'doc','page':1,'evidence':'alpha'}] if i<15 else [])
                trace=mark_result(q,{**result,'method':method},context,[.1,.2,.3],.01,.001)
                traces.append(trace)
                if i>=15:self.assertIsNone(trace['evidence_hit_at_3'])
                self.assertIsNone(trace['abstention_decision'])
        summary=summarize_retrieval(traces)
        for method in summary:
            self.assertEqual(summary[method]['evidence_hit_at_3']['denominator'],15)
            self.assertEqual(summary[method]['timed_lookups'],51)
            self.assertIsNone(summary[method]['agent_answer_grade'])

    def test_evidence_needs_valid_source_page_and_complete_span(self):
        gold=[{'source_id':'doc','page':1,'evidence':'alpha beta'}]
        self.assertTrue(evidence_hit(chunk('a','alpha\n beta'),gold))
        self.assertFalse(evidence_hit(chunk('a','alpha'),gold))
        self.assertFalse(evidence_hit(chunk('a','alpha beta',page=2),gold))


if __name__=='__main__':unittest.main()
