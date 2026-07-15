# PHASE 3.9C private smoke status

- Run ID: `20260714T234316Z-289416d4`
- Anonymous categories: digital invoice, scanned/photo receipt, handwriting-heavy document
- Extraction pass count: 2/3
- Isolated verification pass count: 1/3
- Provider outputs passing schema validation: 3
- Machine-adjudicated count: 0
- Exception count: 1
- Failed/incomplete count: 2
- Arithmetic pass count: 1
- Property-resolution count: 1
- Responsibility-resolution count: 0
- GL-ready count: 0
- Average runtime: unavailable because the run stopped before aggregate finalization
- Total estimated cost: unavailable because the run stopped before aggregate finalization
- Dataset hash status: unchanged and verified
- Verification independence: `isolated_same_family`
- Private identifiers in this report: none

## Gate result

The smoke gate is blocked. Repeated structured-output validation failures left
two documents incomplete, and a subsequent bounded retry failed authentication
with HTTP 401. The completed partial document remained non-gold and did flow
through the central accounting decision and readiness authorities. No pilot-20
run was started.

Before retrying, validate the rotated private credential and make the provider
response schema stable for extraction and isolated verification. A retry must
use a new immutable run ID; existing private outputs must not be overwritten.
