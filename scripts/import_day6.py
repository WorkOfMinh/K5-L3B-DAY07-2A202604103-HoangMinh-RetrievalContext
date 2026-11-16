"""Read-only Day 6 strategy-C snapshot import. Run from the Day 7 project root."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unicodedata
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_pages_fingerprint(path, recorded_sha256):
    """Allow only a byte-proven LF/CRLF serialization difference, not changed JSON/text."""
    data=path.read_bytes()
    actual=hashlib.sha256(data).hexdigest()
    if actual==recorded_sha256:
        return dict(status='exact',actual_sha256=actual,recorded_sha256=recorded_sha256)
    lf=data.replace(b'\r\n',b'\n')
    # Newlines in JSON string values are escaped, so this only changes JSONL record separators.
    for label,candidate in [('LF',lf),('CRLF',lf.replace(b'\n',b'\r\n'))]:
        if hashlib.sha256(candidate).hexdigest()==recorded_sha256:
            return dict(status='line_ending_equivalent',actual_sha256=actual,
                recorded_sha256=recorded_sha256,recorded_serialization=label,
                note='Only LF/CRLF record separators differ; a transformed byte hash exactly matches the recorded index hash.')
    raise ValueError('Prepared source differs from index beyond LF/CRLF line endings. '
                     'Stop and inspect the source; do not change the recorded index hash.')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n',
                    encoding='utf-8', newline='\n')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(text):
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def convert_chunk(row, page):
    required = ['chunk_id','document_id','source','page','text','start_char','end_char','metadata']
    require(all(k in row for k in required), 'Database chunk is missing required source fields.')
    require(row['document_id']==page['document_id'] and row['source']==page['source'],
            'Chunk document/source does not match the prepared page.')
    require(type(row['page']) is int and row['page']==page['page'], 'Chunk page mismatch.')
    start,end=row['start_char'],row['end_char']
    require(type(start) is int and type(end) is int and 0<=start<end<=len(page['text']),
            'Invalid chunk source offsets.')
    require(row['text']==page['text'][start:end], 'Database text differs from source span.')
    require(all(page.get(k)==v for k,v in row['metadata'].items()), 'Chunk metadata contradicts source page.')
    identity=f"{row['document_id']}:{row['page']}:{start}:{end}:".encode()+row['text'].encode()
    require(hashlib.sha256(identity).hexdigest()==row['chunk_id'], 'Chunk ID/source fingerprint mismatch.')
    return dict(id=row['chunk_id'],source_id=row['document_id'],source=row['source'],
                page=row['page'],text=row['text'],start_char=start,end_char=end,
                metadata=dict(row['metadata']),local_source_path='data/day6/source/tutorial.pdf')


def convert_dev_question(row, page_map):
    require(row.get('split')=='dev', 'Only dev questions may be imported in this step.')
    require(isinstance(row.get('id'),str) and bool(row['id'].strip()), 'Missing dev question ID.')
    require(isinstance(row.get('question'),str) and bool(row['question'].strip()), 'Missing question text.')
    require(isinstance(row.get('expected_answer'),str) and bool(row['expected_answer'].strip()), 'Missing expected answer.')
    golds=row.get('acceptable_evidence') or [dict(source_id=row['gold_source_id'],
            page=row['gold_page'],evidence_quote=row['evidence_quote'])]
    converted=[]
    for gold in golds:
        key=(gold['source_id'],gold['page'])
        require(key in page_map,'Gold source page is outside the imported corpus.')
        require(canonical(gold['evidence_quote']) in canonical(page_map[key]['text']),
                'Dev evidence does not occur on its declared source page.')
        converted.append(dict(source_id=gold['source_id'],page=gold['page'],evidence=gold['evidence_quote']))
    filters=row.get('filter')
    require(filters is None or (set(filters)=={'chapter'} and type(filters['chapter']) is int),
            'Only an explicit integer chapter filter is supported.')
    if filters is not None:
        require(f"chapter {filters['chapter']}" in row['question'].lower(),
                'Chapter scope must already be supplied in the question.')
    return dict(id=row['id'],split='dev',question=row['question'],answerable=True,
                expected_answer=row['expected_answer'],gold=converted,filter=filters,
                question_type=row.get('question_type'),origin='day6_dev_seed')


def coverage(chunks, questions):
    result=[]
    for question in questions:
        matching=[c['id'] for c in chunks if any(c['source_id']==g['source_id'] and c['page']==g['page']
                  and canonical(g['evidence']) in canonical(c['text']) for g in question['gold'])]
        result.append(dict(question_id=question['id'],single_chunk_gold_available=bool(matching),
                           covering_chunk_ids=matching))
    return result


def validate_export(directory):
    manifest=read_json(directory/'manifest.json')
    for name,expected in manifest['files_sha256'].items():
        require(sha256(directory/name)==expected, f'Exported file changed: {name}')
    chunks=read_json(directory/'chunks.json');questions=read_json(directory/'dev_seed_questions.json')
    require(len(chunks)==manifest['chunk_count']==142, 'Expected 142 strategy-C chunks.')
    require(len({c['id'] for c in chunks})==len(chunks), 'Duplicate exported chunk IDs.')
    require(len(questions)==15 and all(q['split']=='dev' for q in questions), 'Expected only 15 dev seed questions.')
    require(len({q['id'] for q in questions})==15, 'Duplicate dev question IDs.')
    return manifest


def import_snapshot(day6, database_url):
    import psycopg
    from psycopg.rows import dict_row
    target=ROOT/'data/day6'
    source_manifest=read_json(day6/'data/manifest.json')
    ingestion=read_json(day6/'results/ingestion_summary.json')
    config=read_json(day6/'config/strategies.json')
    pdf=day6/source_manifest['local_path']
    require(pdf.resolve().is_relative_to(day6.resolve()), 'Unexpected source path outside Day 6 starter.')
    require(sha256(pdf)==source_manifest['sha256'], 'Original Day 6 PDF snapshot changed.')
    pages=read_jsonl(day6/'data/pages.jsonl')
    require(len(pages)==source_manifest['counted_content_pages']==37, 'Expected the verified 37-page corpus.')
    page_map={(p['document_id'],p['page']):p for p in pages}
    require(len(page_map)==37, 'Duplicate source pages.')
    dev_questions=read_jsonl(day6/'benchmark/dev.jsonl')
    converted=[convert_dev_question(q,page_map) for q in dev_questions]
    require(len(converted)==15, 'Expected 15 Day 6 dev questions.')
    require(ingestion.get('idempotency',{}).get('passed'), 'Day 6 idempotency evidence is missing.')
    with psycopg.connect(database_url,connect_timeout=10,row_factory=dict_row) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        index=connection.execute('SELECT provenance FROM day06_indexes WHERE strategy=%s',('C',)).fetchone()
        require(index is not None, 'Day 6 strategy C index is missing.')
        provenance=index['provenance']
        require(provenance['config_sha256']==object_hash(config), 'Day 6 configuration differs from stored index.')
        pages_fingerprint=verify_pages_fingerprint(day6/'data/pages.jsonl',provenance['pages_sha256'])
        require(provenance['pdf_sha256']==sha256(pdf), 'PDF differs from stored index.')
        require(provenance['model_signature']==ingestion['model']['signature'], 'Stored model differs from ingest summary.')
        require(provenance['dimensions']==384, 'Expected 384-dimensional E5 embeddings.')
        rows=connection.execute('''SELECT strategy,chunk_id,document_id,source,page,text,
            start_char,end_char,metadata,embedding::text AS embedding,
            vector_dims(embedding) AS dimensions FROM day06_chunks
            WHERE strategy=%s ORDER BY strategy,chunk_id''',('C',)).fetchall()
        require(len(rows)==142 and all(r['dimensions']==384 for r in rows), 'C must have 142 vectors of dimension 384.')
        original=[{k:v for k,v in r.items() if k!='dimensions'} for r in rows]
        digest=object_hash(original)
        require(digest==ingestion['idempotency']['second']['C']['digest'], 'Stored C snapshot differs from successful Day 6 ingestion.')
        database_versions=connection.execute("SELECT current_setting('server_version') AS postgres, "
            "extversion AS pgvector FROM pg_extension WHERE extname='vector'").fetchone()
    chunks=[convert_chunk(row,page_map[(row['document_id'],row['page'])]) for row in rows]
    if pages_fingerprint['status']=='line_ending_equivalent':
        print('Verified LF/CRLF-only source difference; original index hash and all chunk spans match.')
    gold_coverage=coverage(chunks,converted)
    identity=dict(db_strategy='C',db_digest=digest,pdf_sha256=sha256(pdf),
        pages_sha256=sha256(day6/'data/pages.jsonl'),dev_seed_sha256=sha256(day6/'benchmark/dev.jsonl'))
    if target.exists():
        existing=validate_export(target)
        require(existing['import_identity']==identity, 'A different export already exists; preserve it and investigate before replacing it.')
        print('Existing Day 7 snapshot matches Day 6; no files were overwritten.')
        return existing
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.day6-import-',dir=target.parent) as temp:
        staged=Path(temp)/'snapshot';staged.mkdir();(staged/'source').mkdir()
        write_json(staged/'chunks.json',chunks)
        write_json(staged/'dev_seed_questions.json',converted)
        shutil.copyfile(day6/'data/pages.jsonl',staged/'pages.jsonl')
        shutil.copyfile(pdf,staged/'source/tutorial.pdf')
        model_keys=['model_name','revision','dimensions','settings','versions','signature','max_tokens','input_normalization']
        index_keys=['config_sha256','pages_sha256','pdf_sha256','source_reader','chunker_sha256',
                    'strategy','configuration','model_signature','model_name','model_revision','dimensions','metric','search','chunk_count']
        manifest=dict(schema_version=1,created_at=datetime.now(timezone.utc).isoformat(),
            stage='day6_bridge_only; Day 7 benchmark and agent not yet complete',
            chunk_count=len(chunks),source_pages=len(pages),dimensions=384,day6_strategy='C',
            import_identity=identity,model={k:ingestion['model'][k] for k in model_keys if k in ingestion['model']},
            source_pages_fingerprint=pages_fingerprint,
            index_provenance={k:provenance[k] for k in index_keys if k in provenance},
            database_versions=database_versions,source_manifest=source_manifest,
            source_resolution={'original_path_base':'Day 6 starter folder',
                'source_mapping':{'data/raw/tutorial.pdf':'data/day6/source/tutorial.pdf'}},
            dev_seed_questions=len(converted),dev_gold_coverage=gold_coverage,
            benchmark_note='Only Day 6 dev is imported. Add two insufficient-evidence dev cases and build a fresh 7-case Day 7 test set before retrieval tuning. No Day 6 test questions imported.',
            database_access='One REPEATABLE READ, READ ONLY transaction; no INSERT/UPDATE/DELETE/DDL.',
            vector_note='Stored vectors remain in pgvector. They were read as text solely to verify the historical snapshot digest, not exported or regenerated.',
            files_sha256={n:sha256(staged/n) for n in ['chunks.json','dev_seed_questions.json','pages.jsonl','source/tutorial.pdf']})
        write_json(staged/'manifest.json',manifest)
        validate_export(staged)
        staged.rename(target)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day6-starter',type=Path,help='Existing Day 6 starter directory with .env and successful outputs.')
    parser.add_argument('--verify',action='store_true',help='Verify the existing Day 7 export without database access or third-party imports.')
    args=parser.parse_args()
    database_url=''
    try:
        if args.verify:
            manifest=validate_export(ROOT/'data/day6')
        else:
            require(args.day6_starter is not None, 'Supply --day6-starter or use --verify.')
            from dotenv import dotenv_values
            day6=args.day6_starter.expanduser().resolve()
            require((day6/'.env').is_file(), 'Day 6 .env not found at the supplied starter directory.')
            database_url=dotenv_values(day6/'.env').get('DATABASE_URL') or ''
            require(bool(database_url), 'DATABASE_URL missing from the Day 6 .env file.')
            manifest=import_snapshot(day6,database_url)
        print('Day 6 -> Day 7 import OK.')
        print(f"{manifest['chunk_count']} strategy-C chunks | {manifest['source_pages']} source pages | {manifest['dimensions']} dimensions.")
        print('15 dev seed questions imported; no Day 6 test questions imported.')
        print('Saved/verified data/day6/manifest.json and source files.')
        print('Database unchanged. No model, embedding or retrieval run performed.')
        return 0
    except Exception as error:
        message=str(error)
        if database_url:
            message=message.replace(database_url,'[DATABASE_URL]')
            try:
                password=urlsplit(database_url).password
                if password:message=message.replace(password,'[REDACTED]').replace(unquote(password),'[REDACTED]')
            except ValueError:pass
        print(f'ERROR [{type(error).__name__}]: {message}',file=sys.stderr)
        print('Import stopped. No mock or empty-corpus fallback was used.',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
