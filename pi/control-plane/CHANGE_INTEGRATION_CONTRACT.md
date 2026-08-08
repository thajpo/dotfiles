# Pi System Change and Integration Contract

Submission captures one exact working-copy state, records paths, tests,
package identity, provenance, and source fingerprints, then creates an
immutable revision and immutable local change ref. The assigned branch moves
only with an expected-old-object check.

Review binds to one exact change revision and exact package-security state. A
new revision makes earlier review receipts stale. The required reviewer must
be independent of the submitting worker.

The fast path requires one submitted revision, clean submission, current
tests, current package reports, one accepted exact review, an unchanged target,
direct ancestry, and one exact user approval. It never pushes.

The integration path selects immutable input revisions, creates a fresh
integration working copy, resolves conflicts, runs combined tests, submits a
new linked revision, requires independent review, and performs the final
target update only after exact approval. Retries return recorded outcomes and
never integrate a newer object than the approved one.
