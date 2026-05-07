# Phase 2D — download filename evidence
Captured: 2026-05-03T19:54:33.489751+00:00
Batch under test: batch_20260502_170939_992

## 1. Default — no export_name set
- export_name input: None
- export_name stored: None
- HTTP status: 200
- Content-Disposition: `attachment; filename="HWEA_ResMan_Import.xlsx"`

## 2. Operator names the export
- export_name input: 'Richmond Utilities March 2026 Import'
- export_name stored: 'Richmond Utilities March 2026 Import.xlsx'
- HTTP status: 200
- Content-Disposition: `attachment; filename*=utf-8''Richmond%20Utilities%20March%202026%20Import.xlsx`

## 3. Path traversal attempt
- export_name input: '../../etc/passwd.xlsx'
- export_name stored: 'passwd.xlsx'
- HTTP status: 200
- Content-Disposition: `attachment; filename="passwd.xlsx"`

## 4. Illegal characters
- export_name input: 'foo<>:|*?".bar'
- export_name stored: 'foo_.xlsx'
- HTTP status: 200
- Content-Disposition: `attachment; filename="foo_.xlsx"`

## 5. Wrong extension
- export_name input: 'whatever.csv'
- export_name stored: 'whatever.xlsx'
- HTTP status: 200
- Content-Disposition: `attachment; filename="whatever.xlsx"`

---
Restored: export_name cleared on test batch.