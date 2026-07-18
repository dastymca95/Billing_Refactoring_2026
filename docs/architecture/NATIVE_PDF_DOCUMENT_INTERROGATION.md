# Native PDF document interrogation

## Purpose

InnerView keeps the inexpensive path for digital PDFs and deterministic
processors. Difficult scanned PDFs with weak OCR can instead enter a bounded
native-document route. The route exists because model identity alone does not
determine extraction quality: input resolution, document representation,
prompt contract, output schema, retries, page reconciliation, and validation
all materially affect the result.

## Routing

The native route is eligible only when all of the following are true:

- the source is a scanned PDF;
- native PDF extraction is explicitly enabled;
- the configured Vision provider is OpenAI;
- the document is within the configured page and byte limits;
- OCR quality is low, or a multi-page scan lacks critical date/total evidence.

Digital PDFs continue through text extraction. Stable deterministic processors
continue without an AI call. The native route is therefore an escalation, not
a blanket replacement that would charge every page.

## Contract and privacy

The original file is read without modification from the batch input directory.
Only its basename, SHA-256 fingerprint, byte count, and in-memory PDF data are
passed to the provider boundary. Absolute paths are not included in requests,
cache identities, logs, exceptions, or API responses. Cache identities contain
a fingerprint of binary data rather than the private data URL.

The provider returns observed facts only. Raw descriptions, normalized text,
and generated item meanings remain separate. It does not select the final GL;
all candidates still flow through AccountingDecisionEngine. It does not decide
readiness; AccountingReadiness remains the export authority.

## Completeness gates

The structured response records source page, row label/location, activity,
raw description, generated description, amounts, unresolved visual regions,
and page reconciliation. It is rejected before accounting when:

- visible structure was replaced with an aggregate-total fallback;
- a matrix row collapsed multiple billable headers into one line;
- no payable line item or invoice total was recovered;
- line components, page totals, adders, and invoice total differ by more than
  one cent;
- a page claims reconciliation while reporting a non-zero difference;
- source text and generated description are conflated.

An explicit Date of Service may be used as the invoice-date fallback only with
recorded provenance. Visible `Upon Receipt` terms produce a same-day due date
and are not silently overwritten by a generic Net 30 rule.

## Operational controls

The route has independent settings for model, high-detail PDF processing,
reasoning effort, maximum bytes, maximum output tokens, timeout, cost rates,
and cache version. Provider bodies and authorization headers are never logged.
Failures fall back to the existing rendered-image route and remain visible as
review warnings; no failure authorizes export.
