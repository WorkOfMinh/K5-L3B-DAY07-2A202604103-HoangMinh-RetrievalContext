# Dev retrieval comparison

Measured ranking/context preparation only. No answer-generating model, test evaluation or final selection was run.

| Method | Exact Hit@1 | Exact Evidence Hit@3 | Packed-context proxy | Median lookup |
|---|---:|---:|---:|---:|
| dense | 10/15 | 13/15 | 13/15 | 2.94 ms |
| bm25 | 10/15 | 13/15 | 13/15 | 0.80 ms |
| hybrid_rrf | 11/15 | 13/15 | 13/15 | 3.92 ms |

Each method ran on all 17 dev queries; only 15 answerable cases enter ranking/context denominators. The two insufficient cases do not have abstention results yet.

Hybrid uses RRF over dense/BM25 top-10 candidates, not summed raw similarity scores. All methods apply the same explicitly supplied chapter scope.

The context budget is 3500 characters and at most three whole chunks. Candidate scanning can backfill lower-ranked chunks if a higher-ranked block exceeds budget; traces record selected/excluded IDs. No source chunk is truncated.

Do not select a winner from this retrieval-only table: the predeclared primary selection criterion requires real-agent answer quality. Source-equivalence and final context/answer/citation audits remain pending.
