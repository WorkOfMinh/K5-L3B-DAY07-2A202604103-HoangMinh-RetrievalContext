"""Build the individual report from recorded evidence; missing grades stay pending."""
import json
from pathlib import Path
import subprocess
import zipfile
from rag7.common import ROOT,read_json,require,object_hash
from .selection import LOCK_PATH,verify_current


def metric(n,N):return f'{n}/{N}'


def reviewed_metrics(traces,reviews,positive_count):
    lookup={(r['config_id'],r['query_id']):r for r in reviews}
    require(len(lookup)==len(reviews)==len(traces),'Review coverage is incomplete or duplicated.')
    out={}
    for config in sorted({t['config_id'] for t in traces}):
        rows=[t for t in traces if t['config_id']==config];grades=[];support=total=coverage=ctx=neg=false=0
        for t in rows:
            r=lookup[(config,t['query_id'])]
            require(r['trace_sha256']==t['trace_sha256'] and r['answer']==t['answer'] and r['actual_final_context']==t['final_context'],'Review snapshot differs from actual trace.')
            rv=r['review'];cit=rv['citation_support']
            require(len(cit)==len(t['citations']) and all(type(v) is bool for v in cit),'Citation support labels missing.')
            require(isinstance(rv.get('reason'),str) and rv['reason'].strip(),'Review rationale missing.')
            support+=sum(cit);total+=len(cit)
            if t['evaluation_only']['answerable']:
                grade=rv['answer_grade'];require(type(grade) is int and grade in (0,1,2),'Answer grade missing.')
                require(t['status']!='abstain' or grade==0,'False abstention must receive grade 0.')
                require(type(rv['context_evidence_supported']) is bool,'Context review missing.')
                grades.append(grade);ctx+=rv['context_evidence_supported'];false+=t['status']=='abstain'
                coverage+=bool(t['status']=='answered' and cit and all(cit))
            else:
                require(type(rv['abstention_correct']) is bool,'Correct-abstention review missing.')
                require(not rv['abstention_correct'] or t['status']=='abstain','Correct abstention conflicts with actual status.')
                neg+=rv['abstention_correct']
        require(len(grades)==positive_count,'Wrong answerable denominator.')
        out[config]={'mean':sum(grades)/len(grades),'grade_counts':{str(g):grades.count(g) for g in (0,1,2)},
            'full':metric(grades.count(2),positive_count),'citations':metric(support,total),
            'coverage':metric(coverage,positive_count),'context':metric(ctx,positive_count),
            'abstentions':metric(neg,len(rows)-positive_count),'false_abstentions':metric(false,positive_count)}
    return out


def git_value(args):
    try:return subprocess.check_output(['git',*args],cwd=ROOT,stderr=subprocess.DEVNULL,text=True).strip()
    except (OSError,subprocess.CalledProcessError):return None


def generate():
    lock=read_json(LOCK_PATH);verify_current(lock)
    reviews=read_json(ROOT/'results/ai_review/dev_agent_review.json')
    with zipfile.ZipFile(ROOT/'results/agent_dev_bundle.zip') as z:
        dev=json.loads(z.read('agent_traces.json'));raw=json.loads(z.read('agent_summary.json'))
    m=reviewed_metrics(dev,reviews,15)
    lines=['# Day 07: Retrieval and Context Engineering','',
        'Student: Hoang Minh · Student ID: 2A202604103','',
        '## Data and experimental protocol','',
        'The source is the Python Tutorial, release 3.12.11, distributed under the Python documentation/PSF licensing terms. The selected corpus contains 37 physical PDF pages from chapters 5–8 of the 161-page document. Day 06 strategy C provides 142 chunks in PostgreSQL 16 with pgvector, using normalized 384-dimensional intfloat/multilingual-e5-small embeddings. Model revision, package signatures, PDF/page/chunk fingerprints and the C database digest are preserved in the manifests.','',
        'The benchmark contains 24 questions: 15 answerable + 2 insufficient-evidence dev cases, and 5 answerable + 2 insufficient-evidence test cases. Positive cases include source/page/evidence; negative cases are scoped to the selected corpus. Explicit chapter filters are preserved. The model receives only public questions, scope and retrieved context; gold/answerability labels are attached after generation.','',
        f"The agent model is {lock['requested_model']} with low thinking, one candidate and 4096 combined output/thinking tokens. Native search/finalize calls permit two searches and one bounded citation-format repair. Current-context IDs must appear inline and in the citation array. Empty explicit abstentions may display the model's existing nonempty reason. Source text is data, not executable instructions. Errors are retained separately from answers and abstentions.", '',
        '## Retrieval methods and dev experiments','',
        '| Configuration | Method / context | Hit@1 | Evidence Hit@3 | Exact final context | Reviewed context |',
        '|---|---|---:|---:|---:|---:|']
    for c in lock['agent_config']['configurations']:
        cid=c['id'];r=raw['configurations'][cid]['provisional_exact_evidence_metrics_on_completed_answerable_cases']
        vals=[metric(r[k]['numerator'],r[k]['denominator']) for k in ['original_query_hit_at_1','original_query_evidence_hit_at_3','actual_final_context_exact_evidence']]
        lines.append(f"| {cid} | {c['method']}; {c['context_chunks']} chunks / {c['budget_chars']} chars | {' | '.join(vals)} | {m[cid]['context']} |")
    lines += ['',
        'Dense uses the existing vector store with exact cosine sorting. BM25 uses Unicode word tokens retaining underscores, k1=1.2 and b=0.75. RRF fuses the two top-10 rank lists using sum(1/(60+rank)); it does not add incompatible raw score scales. Packing keeps whole chunks, source/page headers and code indentation. All methods use the same corpus and original-question cutoffs, with scope filters applied before ranking. Model-rewritten queries and their results are recorded separately.','',
        'BM25 tested whether exact identifiers improve retrieval. Hybrid tested whether combining lexical and semantic ranks improves results. The five-chunk experiment tested whether more context recovers evidence outside the first three ranks. Each hypothesis could fail; method additions also serve as the required improvement configurations.','',
        '## Answer, citation and abstention results','',
        '| Dev configuration | Mean grounded grade / 2 | Grades 0 / 1 / 2 | Supported citations | Supported citation coverage | Correct abstention | False abstention | Searches / model decisions |',
        '|---|---:|---|---:|---:|---:|---:|---:|']
    for cid in m:
        v=m[cid];r=raw['configurations'][cid];g=v['grade_counts']
        lines.append(f"| {cid} | {v['mean']:.3f} | {g['0']} / {g['1']} / {g['2']} | {v['citations']} | {v['coverage']} | {v['abstentions']} | {v['false_abstentions']} | {r['successful_search_calls']} / {r['model_decisions']} |")
    lines += ['',
        'Answer grades are 0 for an incorrect/unsupported answer or false abstention, 1 for partial completeness, and 2 for a complete source-supported answer. Per-citation semantic support is separate from ID validity. Retrieval/context/answer denominators contain answerable cases only; correct-abstention denominators contain insufficient cases only. Source-equivalent passages can be accepted in reviewed metrics without rewriting raw exact-match counts.','',
        '## Dev errors and selection','',
        '- Dense D10: the first query retrieved the namespace-package exception, but the next query displaced it under latest-query-first packing. The final context lacked the answer, producing a false abstention.',
        '- Larger-context D05: the correct passage was present, but the model grouped two citations in one bracket after repair. The strict format guard rejected the proposal. The delivered abstention receives grade 0.',
        '- Larger-context D09: the model abstained despite the script-mode code example. The display recovery preserved abstain=true and the original reason, with no invented factual answer.',
        '- Hybrid D09: the answer explains a conditional without directly stating the requested assigned value. The final source review assigns partial grade 1. The earlier provisional grade-2 proposal remains archived; no test result informed the revision.',
        '- D14: all methods used a valid cleanup example on physical page 73 instead of the primary page-63 quotation. Reviewed context accepts the equivalent guarantee; raw exact-match scores remain unchanged.', '',
        f"The frozen selection is **{lock['selected_id']}**, compared with baseline **{lock['baseline_id']}**. The first selection criterion, mean grounded dev grade, uniquely resolves the choice (BM25 2.000 versus hybrid 1.933). Cost and latency tie-breakers were not used. The complete predeclared rule and its executed decision path are in results/day7_selection_lock.json. Test results cannot reselect or alter this configuration.", '',
        '## Held-out test comparison','']
    test_path=ROOT/'results/agent_test_bundle.zip';test_complete=False;test_reviewed=False
    if not test_path.exists():
        lines += ['Test execution is pending. No test result is claimed.','']
    else:
        with zipfile.ZipFile(test_path) as z:
            ts=json.loads(z.read('test_traces.json'));tr=json.loads(z.read('test_summary.json'));tl=json.loads(z.read('selection_lock.json'))
        require(tl['seal_sha256']==lock['seal_sha256'],'Test used a different selection lock.')
        test_complete=len(ts)==14
        lines += [f"Recorded test progress: {len(ts)}/14 question/configuration cases. Selection remains {lock['selected_id']}.",'',
            '| Configuration | Hit@1 | Evidence Hit@3 | Exact final context | Grounded answer / reviewed context |',
            '|---|---:|---:|---:|---|']
        audit=ROOT/'results/ai_review/test_agent_review.json';tm=None
        if test_complete and audit.exists():tm=reviewed_metrics(ts,read_json(audit),5);test_reviewed=True
        for cid,r in tr['configurations'].items():
            v=r['provisional_exact_metrics'];vals=[metric(v[k]['numerator'],v[k]['denominator']) for k in ['original_query_hit_at_1','original_query_evidence_hit_at_3','actual_final_context_exact_evidence']]
            label=f"{tm[cid]['mean']:.3f}/2; context {tm[cid]['context']}" if tm else 'Semantic source review pending'
            lines.append(f"| {cid} | {' | '.join(vals)} | {label} |")
        if tm:
            lines += ['', '| Test configuration | Grades 0 / 1 / 2 | Supported citations | Correct abstention | False abstention |','|---|---|---:|---:|---:|']
            for cid,v in tm.items():
                g=v['grade_counts'];lines.append(f"| {cid} | {g['0']} / {g['1']} / {g['2']} | {v['citations']} | {v['abstentions']} | {v['false_abstentions']} |")
        lines.append('')
    lines += ['## Time, usage and cost','',
        'The completed dev run recorded 180 HTTP attempts, 175 successful responses and five transient HTTP 503 failures. Successful-response usage was 249,354 prompt tokens and 17,928 candidate tokens. Failed-request usage can be unknown. Paid API cost is estimated zero for the intended Free tier; actual account billing was not verified and is not presented as a measured zero. Local embedding inference incurs no paid API fee; hardware/electricity are outside that estimate.','',
        'Dev median uncached case times excluding pacing were 3.740 s (dense), 3.048 s (BM25), 4.114 s (hybrid), and 4.650 s (five-chunk hybrid). Full wall time and pacing are preserved separately. Model startup is separate; resumed model responses do not count as uncached latency. Each case has one sampled model trajectory, so these are observed comparisons rather than repeated statistical estimates.','',
        '## Reproduction and evidence','',
        '1. Start the existing Day 06 PostgreSQL/pgvector database and use its virtual environment.',
        '2. Import/verify strategy C with scripts/import_day6.py; validate the frozen benchmark with scripts/validate_day7_benchmark.py.',
        '3. Run the standard-library unit tests. Dev evidence is in results/agent_dev_bundle.zip.',
        '4. Run python -m evaluation7.selection to verify/create the dev lock.',
        '5. Run python -m evaluation7.test_run --day6-starter PATH using the same pinned model and configuration.',
        '6. Review test_review_template.json against actual final contexts, save reviews as results/ai_review/test_agent_review.json, then run python -m evaluation7.report and python -m evaluation7.submission.', '',
        'Per-question tool rankings, contexts, answers, citations, model requests/responses, errors and usage are in the dev/test bundles. Review rows carry source rationales and trace hashes. The lock binds source/benchmark/model/index/config/code and review fingerprints. API keys and DATABASE_URL are read locally and excluded from evidence bundles.','',
        '## Provenance and limitations','',
        'Source judgments and implementation received AI assistance. Reviews remain labeled AI-assisted; no human confirmation was inferred. The student remains responsible for checking the work. The original 35 completed dev cases and raw caches were preserved during a documented display-validator compatibility fix; the migration includes original code/experiment snapshots and per-case hashes. This is a disclosed code-version boundary, not an unchanged-code claim.','',
        'The corpus and dev seeds were already studied in Day 06. Exact-evidence matching can miss equivalent code/text evidence. Larger context can introduce distractors, and another query can discard useful evidence. Provider overload/quota changes can interrupt evaluation. The optional multimodal bonus was not attempted.','',
        'Repository URL: '+(git_value(['remote','get-url','origin']) or 'Set the personal repository origin before submission.'),
        '', 'Report state: '+('test complete and semantically reviewed' if test_reviewed else 'test source review pending' if test_complete else 'test execution pending')+'.','']
    path=ROOT/'report/individual/2A202604103_HoangMinh.md';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('\n'.join(lines),encoding='utf-8',newline='\n')
    print('Report written: '+str(path.relative_to(ROOT)))
    return path


def main():
    try:generate();return 0
    except Exception as exc:print('Report stopped: '+str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
