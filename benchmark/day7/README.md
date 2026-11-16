# Day 07 benchmark v1

This benchmark is fixed before Day 07 retrieval/model experiments. It uses the imported 142 Day 06 strategy-C chunks and the same 37-page source snapshot.

| Split | Answerable | Insufficient evidence | Total |
|---|---:|---:|---:|
| Dev | 15 | 2 | 17 |
| Test | 5 | 2 | 7 |
| Total | 20 | 4 | 24 |

## Files and label boundaries

- `dev.json` / `test.json`: evaluation-only cases with answerability, expected answers, gold evidence and absence rationales. Never send whole records to the agent or retrieval tools.
- `dev_queries.json` / `test_queries.json`: public views containing only `id`, `question`, and explicit `filter`. The model receives the question and filter, not hidden evaluation labels.
- `absence_review.json`: AI-assisted scope review and exhaustive keyword concordances over all 37 selected pages, with relevant passages and limitations described.
- `evaluation_policy.json`: planned comparison and deterministic dev-selection priorities fixed before model results.
- `benchmark_lock.json`: canonical JSON/source content fingerprints and a seal. LF/CRLF and JSON pretty-print changes outside field values do not invalidate these fingerprints; changing text, labels, source spans or configuration policy does.

The imported 15 dev questions are preserved. The five Day 07 answerable test questions are newly authored, not the Day 06 test questions: dictionary-key insertion order; package __all__; binary-mode bytes/encoding; user-defined exception inheritance; disabling exception chaining. Three original dev questions and one new test question explicitly supply chapter scope. All three retrieval methods must receive the same original question and supplied scope under the comparison protocol.

The corpus and its concepts were already studied during Day 06, including related content. New question strings do not make this an independent dataset. Test source authoring/verification is allowed before experiments, but test retrieval/model results must not guide tuning. Fix the agent/retriever configuration on dev before evaluating test.

## Insufficient-evidence cases

These are near-topic questions, not claims that Python cannot do something:

- D7DN01: exact sorting time for ten million integers on an unspecified computer.
- D7DN02: a maximum csv.reader row count specified by these pages.
- D7TN01: an exact maximum JSON file size for an unspecified laptop.
- D7TN02: an AES mode/key length supposedly specified for pickle encryption.

The source discusses sorting, file I/O, JSON and pickle, but does not establish the requested measurements/specifications. Full-corpus keyword scans support the scope check; absence is not inferred from a failed top-k query. Relevant sections were considered, including the general file-memory warning and pickle security warning. A response may state these supported facts with citations while explicitly refusing to invent the missing value. Do not answer from general model knowledge or external referenced documentation.

Absence judgments are AI-assisted and source-scoped; a concordance is not a mathematical proof of nonexistence or an independent human audit. The student should review the rationale. The scope is only the 37 selected pages, not all Python documentation.

## Scoring rules

Retrieval Hit@1 and Evidence Hit@3 use the original question ranking at fixed cutoffs 1 and 3. Only the 15 dev or 5 test **answerable** cases enter those denominators. A hit needs one retrieved chunk at an acceptable source/page containing complete evidence, or a documented equivalent passage accepted on review. Similarity scores are not correctness labels. Accept valid alternatives on other source pages; retain automatic and reviewed decisions separately. Evidence split across chunks does not become a mandatory single-chunk hit.

All positive gold entries in this version are alternative complete evidence passages, not required multi-hop groups. The validation script verifies source text and single-chunk availability; availability is a corpus check, not retrieval success. Context Evidence Rate is separately scored for sufficient evidence in the final context actually sent to the model; a combination of properly sourced chunks may support context sufficiency even without a single-chunk retrieval hit.

Grounded answer: 0 wrong/unsupported or refuses an answerable case; 1 partial; 2 correct and complete with sufficient evidence. Context: 0 no evidence; 1 incomplete conditions/source; 2 enough evidence and source. Read at least all five answerable test cases for baseline and selected configuration, plus both insufficient-evidence cases. Record reasons/reviewer type, not invented human labels.

Citation precision requires context membership **and actual support for the cited claim**. Report citation coverage, tool-use/call counts, abstention success on the two insufficient cases, and answerable false refusals separately. Citation `0/0` is N/A, not 100%. Runtime/model/DB errors are separate from abstentions and must be disclosed, not replaced with fake success results.

## Selection and experiment discipline

Implement dense pgvector, lexical BM25 and hybrid RRF on the same chunks, then measure at least two improvement configurations with hypotheses. They need not improve results to be valid experiments. Compare both retrieval/context and actual agent answers, not only lexical/mock tests. Read at least two dev failures and record causes and measured changes.

The exact selection priority is in `evaluation_policy.json`: grounded-answer mean, correct abstentions, supported-citation coverage, Context Evidence Rate, original-query Hit@3, lower paid API cost, lower end-to-end median latency, then configuration ID. All higher priorities are considered before a tie-break. Missing cost is unknown, not zero. Record actual model/revision/settings, context budgets, candidates, calls, usage and errors; do not claim those have been measured in this preparation step.

After dev selection, freeze the configuration before running baseline and selected agent on the same test. If test results guide changes, disclose that exposure and use a genuinely new test for any independent claim. No retrieval/model result is supplied in this package.

## Validate from the Day 07 root

```powershell
python .\scripts\validate_day7_benchmark.py
```

This checks the imported corpus, private/public case separation, gold source/coverage, counts, absence concordances and frozen fingerprints. It does not run a model, dense search, lexical search, hybrid retrieval or agent evaluation.
