# Final dev source review

All 68 dev cases are complete. Source judgments remain AI-assisted, with no human confirmation inferred.

The final completeness review assigns hybrid D09 grade 1: its answer describes a condition rather than directly stating the assigned value requested. Its cited factual claims remain supported. The initial grade-2 proposal and full case narrative are preserved in DEV_AUDIT_INITIAL.md and dev_agent_review_initial.json; the revision is in dev_review_revision.json. No test result informed the revision.

| Configuration | Mean grounded grade / 2 | Grades 0 / 1 / 2 | Correct abstentions |
|---|---:|---|---:|
| dense_base | 1.867 | 1 / 0 / 14 | 2/2 |
| bm25_base | 2.000 | 0 / 0 / 15 | 2/2 |
| hybrid_base | 1.933 | 0 / 1 / 14 | 2/2 |
| hybrid_context5 | 1.733 | 2 / 0 / 13 | 2/2 |

BM25 uniquely wins the first predeclared criterion. Unknown billing cost and the observed timing differences are not needed to choose a winner. The selection lock is prepared before test; the test comparison must not reselect it.

Dense D10 remains a false abstention after useful evidence was displaced. Larger-context D05 remains a formatting-guard failure, and larger-context D09 remains a model abstention. Source-equivalent context judgments (D09 code/text inference and D14 alternate page-73 passage) remain explicitly documented.

The individual report is report/individual/2A202604103_HoangMinh.md. Raw exact metrics, cached outputs, errors and all 35 preserved pre-recovery case files remain unchanged.
