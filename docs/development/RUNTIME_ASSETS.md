# Runtime asset provisioning

Production accounting requires private/runtime-specific assets that are not
committed: `config/vendors/`, `Output/Template.xlsx`, `Gl Codes/`,
`Properties/`, and `Vendors/`. They may contain business identities, mappings,
documents, or licensed workbook structure and must be provisioned through the
deployment secret/data channel.

Tests set `INNER_VIEW_TEST_ASSET_ROOT` to
`webapp/backend/tests/fixtures/runtime_assets`. That directory contains only a
sanitized minimal GL chart. Tests that write workbooks use an injected writer
and do not need the production template. No private property or vendor mapping
is required for Phase 2.5 contract tests.

For a local non-test run, leave `INNER_VIEW_TEST_ASSET_ROOT` unset and place
the runtime directories at the repository root. For an isolated test run:

```powershell
$env:INNER_VIEW_TEST_ASSET_ROOT = "$PWD\webapp\backend\tests\fixtures\runtime_assets"
python -m pytest -q webapp/backend/tests
```

Canonical business regression fixtures may require separately approved,
sanitized reference packs. Missing private assets must produce an explicit
skip or provisioning error, never inferred accounting data.
