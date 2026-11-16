import hashlib
import json
from pathlib import Path
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
METHODS=('dense','bm25','hybrid_rrf')


def require(condition,message):
    if not condition:raise ValueError(message)


def canonical(text):return ' '.join(unicodedata.normalize('NFKC',text).split())
def read_json(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def object_hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def file_hash(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
    temp.replace(path)


def save_jsonl(path,rows):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w',encoding='utf-8',newline='\n') as stream:
        for row in rows:stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
    temp.replace(path)


def load_corpus():
    folder=ROOT/'data/day6';manifest=read_json(folder/'manifest.json')
    for name,expected in manifest['files_sha256'].items():
        require(file_hash(folder/name)==expected,f'Imported corpus file changed: {name}')
    chunks=read_json(folder/'chunks.json')
    pages=[json.loads(line) for line in (folder/'pages.jsonl').read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    lock=read_json(ROOT/'benchmark/day7/benchmark_lock.json')
    require(object_hash({k:v for k,v in lock.items() if k!='seal_sha256'})==lock['seal_sha256'],'Invalid benchmark seal.')
    require(len(chunks)==142 and len({c['id'] for c in chunks})==142 and len(pages)==37,'Wrong source snapshot size.')
    expected=lock['source_content']
    require(file_hash(folder/'source/tutorial.pdf')==expected['pdf_sha256'],'PDF differs from frozen source.')
    require(object_hash(sorted(chunks,key=lambda c:c['id']))==expected['chunks_sha256'],'Chunk content differs from frozen source.')
    require(object_hash(sorted(pages,key=lambda p:(p['document_id'],p['page'])))==expected['pages_sha256'],'Page content differs from frozen source.')
    return chunks,manifest,lock


def load_dev(lock):
    folder=ROOT/'benchmark/day7'
    # Deliberately do not load test cases, test queries or their absence labels during dev retrieval.
    for name in ['dev.json','dev_queries.json','evaluation_policy.json']:
        require(object_hash(read_json(folder/name))==lock['json_content_sha256'][name],f'Frozen dev input changed: {name}')
    cases=read_json(folder/'dev.json');public=read_json(folder/'dev_queries.json')
    require(len(cases)==17 and sum(c['answerable'] for c in cases)==15,'Expected 15 answerable and 2 insufficient dev cases.')
    require(all(c['split']=='dev' for c in cases),'A test case entered a dev run.')
    require(public==[{k:c.get(k) for k in ['id','question','filter']} for c in cases],'Query view mismatch or label leakage.')
    return cases,public


def load_config():
    config=read_json(ROOT/'config/retrieval.json')
    require(config['methods']==list(METHODS),'Expected dense, bm25, hybrid_rrf methods.')
    require(type(config['candidate_pool']) is int and 3<=config['candidate_pool']<=100,'Invalid candidate pool.')
    require(config['comparison_top_k']==3,'Ranking comparison must retain top-3 cutoff.')
    require(type(config['timing_repeats']) is int and config['timing_repeats']==3,'Use three timed repetitions.')
    context=config['context']
    require(type(context['top_k']) is int and context['top_k']>=1 and context['top_k']<=config['candidate_pool'],'Invalid context top_k.')
    require(type(context['budget_chars']) is int and context['budget_chars']>0,'Invalid context budget.')
    require(context['truncation']=='none' and context['deduplication']=='chunk_id','Unsupported context policy.')
    return config
