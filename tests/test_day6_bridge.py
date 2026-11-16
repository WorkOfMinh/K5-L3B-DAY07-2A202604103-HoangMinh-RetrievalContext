"""Source-contract tests; no model, vector database or fake benchmark run."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.import_day6 import convert_chunk,convert_dev_question,coverage,validate_export,verify_pages_fingerprint


def page():
    return dict(document_id='doc',source='data/raw/tutorial.pdf',page=1,
                text='alpha beta\n gamma delta',chapter=5)


def chunk_row(p):
    text=p['text'];start=0;end=len(text)
    identity=f"doc:1:{start}:{end}:".encode()+text.encode()
    return dict(chunk_id=hashlib.sha256(identity).hexdigest(),document_id='doc',
        source=p['source'],page=1,text=text,start_char=start,end_char=end,metadata={'chapter':5})


class Day6BridgeTests(unittest.TestCase):
    def test_exact_and_lf_crlf_source_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'pages.jsonl'
            lf=b'{"text":"alpha\\n beta"}\n{"text":"gamma"}\n'
            crlf=lf.replace(b'\n',b'\r\n')
            for original,current in [(lf,lf),(crlf,lf),(lf,crlf)]:
                p.write_bytes(current)
                expected=hashlib.sha256(original).hexdigest()
                result=verify_pages_fingerprint(p,expected)
                self.assertEqual(result['recorded_sha256'],expected)
                self.assertEqual(result['status'],'exact' if original==current else 'line_ending_equivalent')

    def test_line_ending_tolerance_does_not_accept_changed_text(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'pages.jsonl'
            original=b'{"text":"alpha"}\r\n'
            p.write_bytes(b'{"text":"changed"}\n')
            with self.assertRaisesRegex(ValueError,'beyond LF/CRLF'):
                verify_pages_fingerprint(p,hashlib.sha256(original).hexdigest())

    def test_ids_metadata_and_offsets_are_preserved(self):
        p=page();row=chunk_row(p);c=convert_chunk(row,p)
        self.assertEqual(c['id'],row['chunk_id'])
        self.assertEqual(c['source_id'],'doc')
        self.assertEqual(c['text'],p['text'][c['start_char']:c['end_char']])
        self.assertEqual(c['metadata'],row['metadata'])
        c['metadata']['chapter']=8
        self.assertEqual(row['metadata']['chapter'],5)

    def test_corrupt_text_offsets_or_metadata_rejected(self):
        p=page();row=chunk_row(p)
        for bad in [dict(row,text='wrong'),dict(row,end_char=999),
                    dict(row,metadata={'chapter':6}),dict(row,chunk_id='wrong')]:
            with self.assertRaises(ValueError):convert_chunk(bad,p)

    def test_only_dev_and_all_alternative_gold_are_preserved(self):
        p=page();q=dict(id='D1',split='dev',question='Within chapter 5, give the text.',
            expected_answer='alpha beta',filter={'chapter':5},
            acceptable_evidence=[dict(source_id='doc',page=1,evidence_quote='alpha beta'),
                                 dict(source_id='doc',page=1,evidence_quote='gamma delta')])
        converted=convert_dev_question(q,{('doc',1):p})
        self.assertEqual(len(converted['gold']),2)
        self.assertEqual(converted['filter'],{'chapter':5})
        self.assertTrue(converted['answerable'])
        with self.assertRaises(ValueError):convert_dev_question(dict(q,split='test'),{('doc',1):p})
        with self.assertRaises(ValueError):convert_dev_question(dict(q,question='No chapter supplied'),{('doc',1):p})

    def test_coverage_does_not_combine_incomplete_chunks(self):
        q={'id':'D1','gold':[dict(source_id='doc',page=1,evidence='alpha beta gamma')]}
        chunks=[dict(id='a',source_id='doc',page=1,text='alpha beta'),
                dict(id='b',source_id='doc',page=1,text='gamma')]
        self.assertFalse(coverage(chunks,[q])[0]['single_chunk_gold_available'])
        chunks.append(dict(id='c',source_id='doc',page=1,text='alpha beta\n gamma'))
        self.assertEqual(coverage(chunks,[q])[0]['covering_chunk_ids'],['c'])

    def test_export_file_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);f=root/'chunks.json';f.write_text('[]',encoding='utf-8')
            digest=hashlib.sha256(f.read_bytes()).hexdigest()
            (root/'manifest.json').write_text(json.dumps({'files_sha256':{'chunks.json':digest}}),encoding='utf-8')
            f.write_text('[{}]',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'Exported file changed'):validate_export(root)


if __name__=='__main__':unittest.main()
