"""Real revision-pinned E5 query embeddings and read-only Day 6 pgvector search."""
import importlib.metadata
import math
from pathlib import Path
from urllib.parse import unquote,urlsplit
from .common import ROOT,canonical,object_hash,require
from .retrievers import validate_filters,validate_k


def read_database_url(day6):
    from dotenv import dotenv_values
    path=Path(day6)/'.env'
    require(path.is_file(),'Day 6 .env not found. Pass the existing Day 6 starter directory.')
    value=dotenv_values(path).get('DATABASE_URL') or ''
    require(bool(value),'DATABASE_URL is missing from the Day 6 .env.')
    return value


def redact_error(error,database_url=''):
    message=str(error) or 'Interrupted by user.'
    if database_url:
        message=message.replace(database_url,'[DATABASE_URL]')
        try:
            password=urlsplit(database_url).password
            if password:message=message.replace(password,'[REDACTED]').replace(unquote(password),'[REDACTED]')
        except ValueError:pass
    return message


def vector_literal(vector):
    require(len(vector)==384 and all(math.isfinite(float(x)) for x in vector),'Expected 384 finite query values.')
    require(abs(sum(float(x)**2 for x in vector)-1)<.002,'Expected an L2-normalized query embedding.')
    return '['+','.join(format(float(x),'.9g') for x in vector)+']'


class LocalQueryEncoder:
    def __init__(self,manifest,day6,offline=False):
        import numpy as np
        import torch
        from sentence_transformers import SentenceTransformer
        self.np=np;self.contract=manifest['model'];settings=dict(self.contract['settings'])
        self.settings=settings
        versions={name:importlib.metadata.version(name) for name in
                  ['sentence-transformers','transformers','torch','numpy','huggingface-hub']}
        self.signature=object_hash({'settings':settings,'versions':versions})
        require(self.signature==self.contract['signature'],
                'Embedding settings/package versions differ from the indexed Day 6 environment. Run using the existing Day 6 virtual environment; do not silently change models.')
        require(settings['dimensions']==384 and settings['query_prefix']=='query: '
                and settings['document_prefix']=='passage: ' and settings['normalize_embeddings'] is True,
                'Unexpected E5 embedding contract.')
        torch.manual_seed(42);np.random.seed(42)
        print('Loading the pinned Day 6 E5 query encoder on CPU.',flush=True)
        self.model=SentenceTransformer(settings['name'],revision=settings['revision'],device=settings['device'],
            cache_folder=str(Path(day6)/'.cache/huggingface'),local_files_only=offline,
            trust_remote_code=False,model_kwargs={'use_safetensors':True})
        dimension_fn=getattr(self.model,'get_embedding_dimension',None)
        dimensions=dimension_fn() if dimension_fn is not None else self.model.get_sentence_embedding_dimension()
        require(dimensions==384,'Unexpected query embedding dimension.')
        self.model.max_seq_length=min(512,self.model.max_seq_length)
        self.cache=ROOT/'rag7/.cache/query_vectors'/self.signature
        self.cache.mkdir(parents=True,exist_ok=True)
        self.cache_hits=0;self.new_vectors=0;self.token_counts={}

    def embed_query(self,query):
        text=self.settings['query_prefix']+canonical(query)
        ids=self.model.tokenizer([text],add_special_tokens=True,truncation=False,padding=False)['input_ids'][0]
        require(len(ids)<=self.model.max_seq_length,'Query exceeds embedding token limit; no silent truncation.')
        self.token_counts[object_hash(query)]=len(ids)
        path=self.cache/(object_hash(text)+'.npy')
        if path.exists():
            vector=self.np.load(path,allow_pickle=False);self.cache_hits+=1
        else:
            vector=self.model.encode([text],prompt='',batch_size=1,normalize_embeddings=True,
                convert_to_numpy=True,show_progress_bar=False)[0]
            self.new_vectors+=1
        vector=self.np.asarray(vector,dtype=self.np.float32)
        require(vector.shape==(384,) and self.np.isfinite(vector).all()
                and self.np.isclose(self.np.linalg.norm(vector),1,atol=1e-4),'Invalid query vector/cache.')
        if not path.exists():
            temp=path.with_suffix('.tmp')
            with temp.open('wb') as stream:self.np.save(stream,vector,allow_pickle=False)
            temp.replace(path)
        return vector.tolist()

    def provenance(self):
        return dict(**self.contract,verified_runtime_signature=self.signature,
            query_cache_hits=self.cache_hits,new_query_vectors=self.new_vectors,
            query_token_counts=self.token_counts,max_query_tokens=self.model.max_seq_length,
            paid_api_cost_usd=0)


class DensePgStore:
    def __init__(self,database_url,manifest,chunks):
        import psycopg
        from psycopg.rows import dict_row
        self.database_url=database_url;self.manifest=manifest;self.chunks={c['id']:c for c in chunks}
        self.search_calls=0
        self.connection=psycopg.connect(database_url,connect_timeout=10,row_factory=dict_row)
        try:
            self.connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            self.verify_connection(self.connection)
        except BaseException:
            self.close();raise

    def verify_connection(self,connection):
        row=connection.execute('SELECT provenance FROM day06_indexes WHERE strategy=%s',('C',)).fetchone()
        require(row is not None,'Day 6 C index missing.')
        require(all(row['provenance'].get(k)==v for k,v in self.manifest['index_provenance'].items()),
                'Live index provenance differs from the imported Day 7 snapshot.')
        rows=connection.execute('''SELECT strategy,chunk_id,document_id,source,page,text,
            start_char,end_char,metadata,embedding::text AS embedding
            FROM day06_chunks WHERE strategy=%s ORDER BY strategy,chunk_id''',('C',)).fetchall()
        require(len(rows)==142 and object_hash(rows)==self.manifest['import_identity']['db_digest'],
                'Live C vectors/chunks differ from the imported snapshot. Do not evaluate mixed indexes.')
        return object_hash(rows)

    def verify_fresh_snapshot(self):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.database_url,connect_timeout=10,row_factory=dict_row) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            return self.verify_connection(connection)

    def search(self,query_vector,*,filters=None,top_k=10):
        validate_filters(filters);validate_k(top_k);vector=vector_literal(query_vector)
        where='strategy=%s';params=[vector,'C']
        if filters is not None:
            where+=" AND (metadata->>'chapter')::integer=%s";params.append(filters['chapter'])
        params.extend([vector,top_k])
        # Adding 0.0 forces full exact sorting instead of approximate vector-index ordering.
        rows=self.connection.execute('''SELECT chunk_id,document_id,source,page,text,start_char,end_char,
            metadata,1-(embedding <=> %s::vector) AS score FROM day06_chunks WHERE '''+where+'''
            ORDER BY (embedding <=> %s::vector)+0.0,chunk_id LIMIT %s''',params).fetchall()
        self.search_calls+=1
        hits=[]
        for rank,row in enumerate(rows,1):
            require(row['chunk_id'] in self.chunks,'Database returned a chunk outside the imported corpus.')
            chunk=self.chunks[row['chunk_id']]
            require(row['document_id']==chunk['source_id'] and all(row[k]==chunk[k]
                for k in ['source','page','text','start_char','end_char','metadata']),
                'Returned database content differs from imported chunk.')
            hits.append({**chunk,'rank':rank,'score':float(row['score'])})
        return hits

    def close(self):
        if self.connection is not None:
            self.connection.close();self.connection=None
