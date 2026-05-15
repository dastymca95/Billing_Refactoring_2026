# Utility Old Scripts Analysis Report

Phase: U1 - Utility Vendor Deterministic Processor Expansion  
Workspace: `C:\Users\Dasty\PycharmProjects\Billing_Refactoring_2026`  
Date: 2026-05-13

## Scope

Reviewed `Old Scripts/` as read-only reference material for utility vendor behavior. These scripts were not modified and no credentials, hardcoded paths, or Dropbox token values were copied into new code or reports.

## Safety Findings

- Multiple old scripts contain embedded Dropbox credential variables and local machine paths. Those patterns are obsolete and must not be carried forward.
- Several scripts write directly to local output folders and Dropbox paths. The webapp path must continue using current batch-local staging, dry-run support, cancellation, and configured Dropbox support only when explicitly enabled.
- Old scripts are useful for parsing patterns, not for direct reuse.

## Script Notes

| Old script | Matching vendor | Useful business logic | Risk / obsolete behavior | U1 action |
| --- | --- | --- | --- | --- |
| `Alabama_Power.py` | Alabama Power | Account format similar to `#####-#####`; service/billing period and due-date parsing; utility invoice number composed from account and billing month/year. | Hardcoded Dropbox and local folders. | Marked `partial_reference_old_script`; modern processor still needed. |
| `CDE Light Band.py` | CDE Lightband | Electric subtotal/connect charge parsing; service address/property hints; connection fee handling. | Hardcoded Dropbox/local paths; property logic too local to copy blindly. | Marked `partial_reference_old_script`; use as parser reference. |
| `CPWS.py` | Columbia Power and Water System | Existing legacy CPWS parsing patterns; power/water charge grouping. | Legacy path/output behavior. | Existing modern webapp processor is active; keep old script as reference only. |
| `EPB_Fiber.py` | EPB Fiber Optics | Account/invoice/date detection; previous balance/payment exclusion; internet/fiber semantics; property hints from account/service address. | Embedded Dropbox credentials and local paths. | Marked `partial_reference_old_script`; modern EPB deterministic processor needed. |
| `Hardin CWD2.py` | Hardin County Water District No. 2 | Water bill parse patterns and property/location hints. | Legacy output/Dropbox behavior. | Existing modern webapp processor is active. |
| `Henderson Bills.py` | The City of Henderson | Electric bill parse patterns; service period/account logic. | Legacy paths and direct output assumptions. | Marked `partial_reference_old_script`. |
| `HWEA Test.py` | Hopkinsville Water Environment Authority | Water/sewer invoice parse history; useful regression reference. | Legacy script conventions; should not supersede current processor. | Existing modern webapp processor is active. |
| `Nolin REC.py` | Nolin RECC Smarthub | Master vs single account recognition; per-account invoice behavior; property/location hints. | Old Dropbox/local output assumptions. | Marked `partial_reference_old_script`; modern Nolin processor needed. |
| `Pennyrile Bills.py` | Pennyrile Electric | Electric charge parsing, account/date rules, property/location behavior. | Legacy implementation exists beside modern processor. | Existing modern webapp processor is active. |
| `Shelbyville Power.py` | Shelbyville Power System | Electric charge parsing, billing period/date rules, property hints. | Legacy output behavior. | Existing modern webapp processor is active. |
| `Apartments.com.py` | Marketing / Advertising | Not a utility processor. | Out of U1 scope. | Ignored for U1. |
| `Resman.py` | Shared ResMan utility helper | Template writing conventions may be historically useful. | Not vendor-specific; may contain old path assumptions. | Reference only; no direct copy. |

## Extracted Reusable Patterns

- Utility invoice numbers should generally be generated from account number plus service/billing month and two-digit year when no explicit vendor rule says otherwise.
- Payment rows, autopay rows, amount enclosed, and previous balance rows must be excluded from expense lines unless a vendor rule explicitly allows balance-forward treatment.
- Connection/reconnection fees are separate charge lines and map to GL `6956`.
- Late fees are not connection fees; they should use the underlying service/vendor default GL or be reviewed.
- Fiber/internet lines should not be treated as electric solely because EPB files live under the power folder.
- Community/master billing behavior must be vendor-specific, especially for Kentucky Utilities, Knoxville Utilities Board, and Nolin-style master bills.

## Not Carried Forward

- Embedded Dropbox credentials or token values.
- Hardcoded local user paths.
- Direct writes to `Output/Template.xlsx`.
- Direct Dropbox uploads during tests.
- Vendor-specific property guesses that bypass Unit Info Clean / property reference validation.
