# Billing V2 continuous integration

`/.github/workflows/ci.yml` is the pull-request gate for `main` and also runs
after every push to `main`. It uses Python 3.11 and Node 24, matching the
supported backend image and the current local frontend runtime.

## Jobs

| Job | Blocking contract |
| --- | --- |
| `repository-safety` | Full-history checkout, whitespace checks, sanitized `.env.example`, redacted secret scan, merge-marker check, forbidden tracked-artifact check, and no newly added absolute Windows paths in production source. |
| `backend` | Installs `requirements.txt`, compiles `webapp/backend`, requires at least 432 collected tests, and runs the complete backend suite. |
| `frontend` | Uses `package-lock.json` through `npm ci` and builds the TypeScript/Vite production bundle. |
| `active-e2e` | Starts the test backend on `8001`, Vite on `5174`, waits for backend/frontend/proxy health, installs Chromium only, verifies 14 active tests, and executes them. |
| `legacy-discovery` | Does not execute historical runtime tests. It requires exactly 41 discoverable tests and exactly 10 U4 cases from the tracked sanitized fixture. |

The active Billing V2 suite is a release gate. The separate legacy suite is a
reproducibility and migration inventory; only its discovery and sanitized
fixture contract block CI. See
[`ACTIVE_AND_LEGACY_E2E_STATUS.md`](ACTIVE_AND_LEGACY_E2E_STATUS.md).

## Provider and privacy boundary

CI does not reference GitHub secrets or production credentials. OpenAI,
Gemini, DeepSeek, Claude, Vision, semantic reasoning, and AI fallbacks are
explicitly disabled. Tests use deterministic mocks and tracked synthetic or
public-safe fixtures. A local `.env` is neither tracked nor available in the
runner.

Some production deterministic processors live in separately provisioned
deployment trees. CI uses non-executable import stubs only for the processor
registry audit and four minimal sanitized vendor YAMLs under
`webapp/backend/tests/fixtures/runtime_assets`. The stubs raise if execution is
attempted; they cannot process a document or replace a production parser.

CI must never upload `webapp_data`, PDFs, invoice images, screenshots, crops,
Playwright videos, traces, batches, databases, or provider payloads. On an E2E
failure, only the backend warning log (with access logging disabled) and Vite
startup log are retained for five days.

## Local equivalents

From the repository root, with provider variables disabled in the shell:

```text
python scripts/ci_repository_safety.py --base main
python -m compileall -q webapp/backend
python scripts/ci_verify_discovery.py backend --minimum 432
python -m pytest -q webapp/backend/tests

cd webapp/frontend
npm ci
npm run build
cd ../..

python scripts/ci_verify_discovery.py active --expected 14
python scripts/ci_verify_discovery.py legacy --expected 41 --expected-u4 10
```

To execute active E2E locally, start FastAPI on `8001`, start Vite on `5174`
with `VITE_BACKEND_PORT=8001`, wait for `/api/health` through both ports, then
run from `webapp/frontend`:

```text
npm run test:e2e:active
```

The legacy discovery verifier deliberately points `INNER_VIEW_U4_RUNTIME_MANIFEST`
at a nonexistent path. This proves that discovery uses only
`e2e/fixtures/legacy-u4/fixture_manifest.json`, never an ignored private local
manifest.

## Investigating failures

- Safety failures report only `path:line`, a category, and `[REDACTED]`; inspect
  the referenced file locally rather than printing the value in Actions logs.
- A backend discovery failure means tests fell below the protected baseline;
  additions are allowed without changing the threshold.
- `npm ci` failure normally indicates lockfile drift or an unavailable locked
  dependency.
- Active E2E failures should be reproduced with the same `8001/5174` stack and
  deterministic mocks. Download only the safe service-log artifact.
- Legacy discovery failures should first verify the tracked U4 manifest schema
  and confirm that all four legacy specs remain included.
