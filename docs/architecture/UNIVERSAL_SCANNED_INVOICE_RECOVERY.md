# Universal scanned-invoice recovery hardening
## Scope

This hardening addresses scanned and handwritten invoices whose payable facts
are arranged in dense tables or matrices. It is document-structure logic. It
does not contain supplier, property, invoice-number, GL, or fixture-specific
rules, and it does not change the authority boundaries of
`AccountingDecisionEngine` or `AccountingReadiness`.

## Failure modes found

- Exact duplicate uploads were charged and processed repeatedly.
- Multi-page page totals could be mistaken for the document total.
- A provider could collapse several visible matrix headers into one aggregate
  line and still satisfy the former schema.
- A small unexplained difference could be treated as reconciled even though no
  explicit tax, shipping, fee, discount, or credit supported the adjustment.
- Proportional distribution could rewrite observed source amounts.
- Vision repair retries mutated the request object used to calculate the cache
  key, so a valid repaired result could become unreachable from the original
  request identity.
- Provider response character limits were smaller than the configured token
  budget for large structured outputs.
- `files_supported` counted processing attempts rather than fully supported
  source files.

## Active recovery flow

1. Hash source files and process exact content only once. Preserve duplicate
   filenames as provenance aliases.
2. Extract multi-page scans page by page so page-scoped totals remain explicit.
3. Expand a collapsed matrix row only when headers and numeric component tokens
   have a unique exact arithmetic interpretation.
4. Preserve any ambiguous row as source evidence, but reject a matrix response
   when zero component rows can be reconciled.
5. For a detected aggregate matrix fallback, render three overlapping table
   bands without modifying the PDF source and run a bounded visual recovery.
6. For documents within the configured page limit, compare a whole-document
   recovery against the page merge. Adopt it only when invoice identity and
   explicit total agree and semantic component specificity improves.
7. Escalate to the configured strong Vision profile only after the economical
   profile returns invalid structure. The strong route is not enabled globally.
8. Preserve explicit page/document totals. A component mismatch remains visible
   and review-blocking.
9. Send all extracted facts and GL candidates through the existing central
   accounting pipeline. Only `AccountingDecisionEngine` selects the final GL;
   only `AccountingReadiness` authorizes export.

## Deliberate behavior retirement

The former behavior that marked a difference within three percent as
reconciled under `distribute_proportionally` has been retired. An unexplained
difference is never tax merely because it is small. Source row amounts remain
unchanged unless explicit tax, shipping, or fee evidence exactly accounts for
the allocation. This is a safety correction, not removal of supported explicit
tax distribution.

## Latest-batch verification

Batch `batch_20260717_083219_430` after hardening:

- source files: 10;
- unique contents: 6;
- exact duplicates skipped with provenance: 4;
- files supported: 10;
- provider/processing failures: 0;
- invoices produced: 7;
- exported rows with blank GL: 0;
- invoice `22-3197`: explicit total 7,235.00, observed components 7,185.00,
  mismatch 50.00, `valid=false`, `reconciled=false`, `export_blocked=true`;
- invoice `22-3127`: explicit total and rows reconcile at 8,710.00, but the
  hard-to-read first-page matrix remains represented by a review fallback and
  required-field blockers. No component amounts were invented.

The batch is now reviewable and safe, not falsely complete. Human review remains
required where the source itself is ambiguous or a required accounting field
cannot be proven.
