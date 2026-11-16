"""Check the frozen Day 07 benchmark against the imported corpus; no retrieval/model calls."""
import hashlib
import json
from pathlib import Path
import sys
import unicodedata

ROOT=Path(__file__).resolve().parents[1]


def require(condition,message):
    if not condition:raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def object_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def sha256(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(text):return ' '.join(unicodedata.normalize('NFKC',text).split())


def query_view(case):
    """Only this view, never gold/answerability/expected behavior, may reach an agent/tool."""
    return {key:case.get(key) for key in ['id','question','filter']}


def count_cases(cases):
    return {split:{'answerable':sum(c['split']==split and c['answerable'] for c in cases),
                   'insufficient':sum(c['split']==split and not c['answerable'] for c in cases)}
            for split in ['dev','test']}


def validate_case(case,page_map,chunks):
    require(isinstance(case.get('id'),str) and bool(case['id'].strip()),'Missing case ID.')
    require(case.get('split') in {'dev','test'},'Invalid split.')
    require(type(case.get('answerable')) is bool,'answerable must be a boolean evaluation label.')
    require(isinstance(case.get('question'),str) and bool(case['question'].strip()),'Missing question.')
    filters=case.get('filter')
    if filters is not None:
        require(isinstance(filters,dict) and set(filters)=={'chapter'} and type(filters['chapter']) is int,
                'Expected an explicit integer chapter filter.')
        require(f"chapter {filters['chapter']}" in case['question'].casefold(),
                'Filter must be supplied in question scope, not inferred from gold.')
    require(isinstance(case.get('gold'),list),'gold must be an evaluation-only list.')
    if not case['answerable']:
        require(not case['gold'],'Insufficient-evidence cases must not have positive gold evidence.')
        require(case.get('expected_behavior')=='search_and_abstain','Missing abstention target.')
        require(bool(case.get('absence_reason','').strip()),'Missing source-scoped absence rationale.')
        return []
    require(bool(case.get('expected_answer','').strip()) and bool(case['gold']),
            'Answerable cases need an expected answer and gold evidence.')
    matches=[]
    for gold in case['gold']:
        key=(gold.get('source_id'),gold.get('page'))
        require(key in page_map,'Gold page is outside the selected corpus.')
        require(isinstance(gold.get('evidence'),str) and bool(gold['evidence'].strip()),'Empty gold evidence.')
        require(canonical(gold['evidence']) in canonical(page_map[key]['text']),
                f"{case['id']}: evidence missing from declared source page.")
        if filters is not None:
            require(page_map[key]['chapter']==filters['chapter'],'Gold contradicts user-supplied chapter scope.')
        covering=[c['id'] for c in chunks if c['source_id']==key[0] and c['page']==key[1]
            and canonical(gold['evidence']) in canonical(c['text'])]
        require(bool(covering),f"{case['id']}: complete required evidence is not available in a single imported chunk; review schema before evaluating.")
        matches.extend(covering)
    return sorted(set(matches))


def validate_benchmark(root=ROOT):
    folder=root/'benchmark/day7';data=root/'data/day6'
    lock=read_json(folder/'benchmark_lock.json')
    require(object_hash({k:v for k,v in lock.items() if k!='seal_sha256'})==lock['seal_sha256'],
            'Benchmark lock seal changed.')
    for name,expected in lock['json_content_sha256'].items():
        require(object_hash(read_json(folder/name))==expected,f'Frozen benchmark field values changed: {name}')
    source_manifest=read_json(data/'manifest.json')
    for name,expected in source_manifest['files_sha256'].items():
        require(sha256(data/name)==expected,f'Imported corpus file changed: {name}')
    pages=[json.loads(s) for s in (data/'pages.jsonl').read_text(encoding='utf-8-sig').splitlines() if s.strip()]
    chunks=read_json(data/'chunks.json')
    require(len(pages)==37 and len(chunks)==142,'Expected 37 pages and 142 fixed C chunks.')
    require(len({c['id'] for c in chunks})==142,'Duplicate chunk IDs.')
    page_map={(p['document_id'],p['page']):p for p in pages}
    require(len(page_map)==37,'Duplicate source pages.')
    expected=lock['source_content']
    require(sha256(data/'source/tutorial.pdf')==expected['pdf_sha256'],'PDF differs from benchmark source.')
    require(object_hash(sorted(pages,key=lambda p:(p['document_id'],p['page'])))==expected['pages_sha256'],
            'Source page values differ from benchmark. LF/CRLF formatting alone is permitted.')
    require(object_hash(sorted(chunks,key=lambda c:c['id']))==expected['chunks_sha256'],
            'Chunk/source values differ from benchmark.')
    cases=[]
    for split in ['dev','test']:
        rows=read_json(folder/f'{split}.json')
        require(isinstance(rows,list) and all(c['split']==split for c in rows),'Split/file mismatch.')
        public=read_json(folder/f'{split}_queries.json')
        require(public==[query_view(c) for c in rows],'Agent-facing query view differs or leaks evaluation labels.')
        cases.extend(rows)
    require(len(cases)==24 and len({c['id'] for c in cases})==24,'Expected 24 distinct cases.')
    require(len({c['question'] for c in cases})==24,'Duplicate question text.')
    require(count_cases(cases)=={'dev':{'answerable':15,'insufficient':2},
                                'test':{'answerable':5,'insufficient':2}},'Incorrect dev/test counts.')
    seeds=read_json(data/'dev_seed_questions.json')
    retained=[c for c in cases if c.get('origin')=='day6_dev_seed']
    require(retained==seeds,'Day 6 dev seed labels/questions were changed during import.')
    available={}
    for case in cases:available[case['id']]=validate_case(case,page_map,chunks)
    reviews=read_json(folder/'absence_review.json')
    absent={c['id']:c for c in cases if not c['answerable']}
    require(len(reviews)==4 and {r['id'] for r in reviews}==set(absent),'Missing or duplicate absence reviews.')
    page_numbers=sorted(p['page'] for p in pages)
    for review in reviews:
        require(sorted(review['source_pages_scanned'])==page_numbers,'Absence review does not cover the entire selected corpus.')
        require(set(review['reviewed_relevant_pages']).issubset(page_numbers),'Relevant review page outside scope.')
        require(review['absence_reason']==absent[review['id']]['absence_reason'],'Absence rationales disagree.')
        require(bool(review.get('reviewer')) and bool(review.get('scope')),'Missing absence reviewer/scope.')
        for concordance in review['full_corpus_keyword_concordance']:
            term=concordance['term'].casefold()
            expected_matches=[]
            for p in pages:
                count=canonical(p['text']).casefold().count(term)
                if count:expected_matches.append({'page':p['page'],'occurrences':count})
            require(concordance['matching_pages']==expected_matches,'Absence concordance does not match source.')
            require(concordance['total_occurrences']==sum(m['occurrences'] for m in expected_matches),
                    'Absence concordance count mismatch.')
    return dict(status='source_contract_checks_passed_no_retrieval',counts=count_cases(cases),
        answerable_evidence_cases_verified=20,absence_cases_with_full_corpus_review=4,
        imported_chunks=142,selected_source_pages=37,
        filter_cases={s:sum(c['split']==s and c.get('filter') is not None for c in cases) for s in ['dev','test']},
        evidence_covering_chunk_ids=available,
        reviewer_note='Source/absence judgments are AI-assisted; keyword consistency is not an automatic proof of nonexistence or an independent human review.',
        retrieval_runs=0,model_runs=0)


def main():
    try:
        report=validate_benchmark()
        output=ROOT/'results/day7_benchmark_validation.json';output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8',newline='\n')
        print('Day 07 benchmark validation OK.')
        print('Dev: 17 cases = 15 answerable + 2 insufficient-evidence.')
        print('Test: 7 cases = 5 answerable + 2 insufficient-evidence.')
        print('20 answerable cases: source evidence and single-chunk availability verified.')
        print('4 absence reviews checked across all 37 selected pages.')
        print('142 chunks and frozen source/benchmark content verified.')
        print('No retrieval or model run performed.')
        return 0
    except Exception as error:
        print(f'ERROR [{type(error).__name__}]: {error}',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
