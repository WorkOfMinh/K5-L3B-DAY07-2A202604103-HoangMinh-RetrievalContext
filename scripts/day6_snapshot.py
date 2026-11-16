"""Export/restore the exact strategy-C index for Day 07 reproduction."""
import argparse
import importlib.metadata
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from rag7.common import load_corpus,object_hash,read_json,require,save_json
from rag7.dense_backend import DensePgStore,read_database_url,redact_error

SNAPSHOT=ROOT/'data/day6/index_snapshot.json'
SELECT_ROWS='''SELECT strategy,chunk_id,document_id,source,page,text,start_char,end_char,
    metadata,embedding::text AS embedding FROM day06_chunks WHERE strategy=%s ORDER BY strategy,chunk_id'''


def validate(snapshot,manifest):
    require(snapshot['snapshot_sha256']==object_hash({k:v for k,v in snapshot.items() if k!='snapshot_sha256'}),'Snapshot hash mismatch.')
    require(snapshot['strategy']=='C' and len(snapshot['rows'])==142,'Expected exactly 142 strategy-C rows.')
    require(len({r['chunk_id'] for r in snapshot['rows']})==142,'Duplicate chunk IDs in snapshot.')
    require(all(r['strategy']=='C' and len(json.loads(r['embedding']))==384
                and all(math.isfinite(float(v)) for v in json.loads(r['embedding']))
                for r in snapshot['rows']),'Invalid strategy or vector shape/value.')
    require(object_hash(snapshot['rows'])==snapshot['database_digest']==manifest['import_identity']['db_digest'],'Snapshot differs from the evaluated index.')
    require(snapshot['index_provenance']==manifest['index_provenance'],'Index provenance differs.')
    require(snapshot['embedding_signature']==manifest['model']['signature'],'Embedding signature differs.')
    return snapshot


def export(day6):
    chunks,manifest,_=load_corpus();url=read_database_url(day6);store=None
    try:
        store=DensePgStore(url,manifest,chunks)
        rows=store.connection.execute(SELECT_ROWS,('C',)).fetchall()
        body={'schema_version':1,'strategy':'C','rows':rows,
            'database_digest':manifest['import_identity']['db_digest'],
            'index_provenance':manifest['index_provenance'],
            'embedding_signature':manifest['model']['signature'],
            'database_versions':manifest['database_versions'],
            'scope':'Day 07 strategy-C index only; not a complete Day 06 A/B/C database backup.'}
        body['snapshot_sha256']=object_hash(body);validate(body,manifest)
        store.verify_fresh_snapshot()
        packages={name:importlib.metadata.version(name) for name in
            ['sentence-transformers','transformers','torch','numpy','huggingface-hub','psycopg','python-dotenv']}
        require(all(packages[k]==v for k,v in manifest['model']['versions'].items()),'Active embedding package versions differ from the evaluated environment.')
        save_json(SNAPSHOT,body)
        (ROOT/'requirements-runtime.txt').write_text('\n'.join(('psycopg[binary]' if name=='psycopg' else name)+'=='+version for name,version in packages.items())+'\n',encoding='utf-8',newline='\n')
        print('Export OK: 142 real C chunks and their original 384-dimensional vectors.')
        print('Saved data/day6/index_snapshot.json and requirements-runtime.txt.')
        print('Read-only database access; no re-embedding, retrieval, model or API call. No credentials exported.')
    except Exception as exc:
        raise ValueError(redact_error(exc,url)) from None
    finally:
        if store is not None:store.close()


def restore(env_file):
    from dotenv import dotenv_values
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from psycopg import sql
    _,manifest,_=load_corpus();snapshot=validate(read_json(SNAPSHOT),manifest)
    url=dotenv_values(env_file).get('DATABASE_URL') or ''
    require(bool(url),'Target .env must define DATABASE_URL for a fresh PostgreSQL database.')
    try:
        with psycopg.connect(url,connect_timeout=10,row_factory=dict_row) as connection:
            existing=connection.execute("SELECT to_regclass('day06_indexes') AS indexes, to_regclass('day06_chunks') AS chunks").fetchone()
            require(existing['indexes'] is None and existing['chunks'] is None,'Target already has Day 06 tables; restore refuses to overwrite them. Use a fresh database.')
            major=int(connection.execute('SHOW server_version_num').fetchone()['server_version_num'])//10000
            require(major==16,'Use PostgreSQL 16 for this recorded index.')
            version=snapshot['database_versions']['pgvector']
            connection.execute(sql.SQL('CREATE EXTENSION IF NOT EXISTS vector VERSION {}').format(sql.Literal(version)))
            installed=connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()['extversion']
            require(installed==version,'Installed pgvector version differs from the recorded index.')
            connection.execute('CREATE TABLE day06_indexes (strategy text PRIMARY KEY, provenance jsonb NOT NULL)')
            connection.execute('''CREATE TABLE day06_chunks (
                strategy text NOT NULL REFERENCES day06_indexes(strategy), chunk_id text NOT NULL,
                document_id text NOT NULL, source text NOT NULL, page integer NOT NULL,
                text text NOT NULL, start_char integer NOT NULL, end_char integer NOT NULL,
                metadata jsonb NOT NULL, embedding vector(384) NOT NULL,
                PRIMARY KEY(strategy,chunk_id))''')
            connection.execute('INSERT INTO day06_indexes VALUES (%s,%s)',('C',Jsonb(snapshot['index_provenance'])))
            with connection.cursor() as cursor:
                cursor.executemany('''INSERT INTO day06_chunks
                    (strategy,chunk_id,document_id,source,page,text,start_char,end_char,metadata,embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)''',
                    [(r['strategy'],r['chunk_id'],r['document_id'],r['source'],r['page'],r['text'],
                      r['start_char'],r['end_char'],Jsonb(r['metadata']),r['embedding']) for r in snapshot['rows']])
            rows=connection.execute(SELECT_ROWS,('C',)).fetchall()
            require(object_hash(rows)==snapshot['database_digest'],'Restored row/vector digest mismatch; transaction will roll back.')
        print('Restore OK: exact C index digest verified. Original database was not changed.')
    except Exception as exc:
        raise ValueError(redact_error(exc,url)) from None


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    exp=sub.add_parser('export');exp.add_argument('--day6-starter',type=Path,required=True)
    res=sub.add_parser('restore');res.add_argument('--env-file',type=Path,required=True)
    sub.add_parser('verify')
    args=parser.parse_args()
    try:
        if args.command=='export':export(args.day6_starter)
        elif args.command=='restore':restore(args.env_file)
        else:
            _,manifest,_=load_corpus();validate(read_json(SNAPSHOT),manifest)
            print('Snapshot integrity OK: matches the evaluated C-index digest. No database/API call.')
        return 0
    except Exception as exc:
        print('Snapshot operation stopped: '+str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
