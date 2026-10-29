---
name: frappe-traceability
description: >
  Use when maintaining a requirements traceability matrix (RTM) for a Frappe/ERPNext
  project built from BRD / FRS / To-Be documents — linking every requirement ID to
  its ERPNext object, design, code artifacts and tests, and checking coverage before
  sign-off. Includes a checker script that verifies every requirement is traced and
  every listed file and test exists. Keywords: traceability, RTM, requirement
  coverage, requirement to test mapping, sign-off, UAT, acceptance, coverage report,
  which requirements are done, requirement status.
license: MIT
compatibility: "Claude Code. Frappe/ERPNext v16."
---

# Frappe Traceability

The traceability matrix at `docs/spec/traceability.md` is the single answer to
*"is every requirement in the documents built and tested?"*. Every phase of
`/frappe-build-from-docs` updates it; nobody marks a row done without evidence.

## Matrix format

Header must be exactly this (the checker parses it):

```markdown
| Req ID | Requirement | Source | Fit | ERPNext Object | Artifacts | Tests | Status | Notes |
|---|---|---|---|---|---|---|---|---|
| FR-001 | Bank verification status on Supplier | FRS §4.1 | Extend | Supplier, Purchase Order | `my_app/fixtures/custom_field.json`, `my_app/overrides/purchase_order.py` | `my_app/tests/test_supplier_verification.py::test_po_blocked_for_unverified` | Verified | |
| PS-P2P-02 | Two-level MR approval | ToBe Fig 2 | Configure | Material Request | `my_app/fixtures/workflow.json` | `my_app/tests/test_mr_workflow.py` | Built | |
| BR-007 | Multi-currency invoicing | BRD §2.1 | Standard | Sales Invoice | - | UAT-007 | Designed | Standard; covered by UAT script |
| FR-044 | SMS on dispatch | FRS §6.3 | Out of Scope | - | - | - | Deferred | Phase 2 per client (OQ-012) |
```

- **Artifacts / Tests**: backticked paths relative to the app folder (`apps/<app>`).
  `file.py::test_name` is allowed for tests. Non-file evidence (UAT script steps) is
  written without backticks, e.g. UAT-007.
- **Fit**: `Standard`, `Configure`, `Extend`, `Build`, `Integrate`, `Out of Scope`.
- **Status** lifecycle:

| Status | Meaning (as computed by `trace_tags.py`) |
|---|---|
| Pending | Not yet assigned to a slice in design.yaml |
| Designed | Assigned to a slice; no tagged code yet |
| Built | Tagged in product code (`# REQ: <id>`), or its slice is approved without code (Standard) |
| Verified | Tagged in a test and its slice review is fully approved — or Standard / Configure with an approved slice review |
| Out of Scope | Fit is Out of Scope in `02-fit-gap.md` (reason from its rationale) |

## Generate the matrix (feature-slice builds)

In `/frappe-build-from-docs` the matrix is **generated, never edited by hand**. Code and tests
carry comments such as `# REQ: FR-012, FR-013` or `// REQ: PS-INW-02`; the script combines them
with `design.yaml` (which slice owns each requirement), the slice review checklists (is the
slice approved?) and `02-fit-gap.md` (Fit, ERPNext object):

```bash
./env/bin/python <skill-dir>/scripts/trace_tags.py --app <app> --check
```

It rewrites `docs/spec/traceability.md`, lists tags that name unknown requirement IDs, and
with `--check` runs the checker below.

## Check coverage

`<skill-dir>` is this skill's base directory, shown when the skill loads. Run from
the bench folder:

```bash
python3 -I <skill-dir>/scripts/check_traceability.py \
  --spec apps/<app>/docs/spec --root apps/<app> --strict
```

It reports requirements missing from the matrix, invalid Fit/Status values, missing
artifact or test files, and verified coverage of in-scope requirements. The script
checks that files exist; it does not run tests — the Tester sets `Verified` only from
an actual passing `bench run-tests` result or a UAT step the user has accepted.

## Rules

1. Rows are never deleted. Withdrawn requirements become `Out of Scope` with a reason.
2. Requirement IDs come only from `docs/spec/01-requirements.md`.
3. Tag code and tests in the same step that writes them (generated matrix), or update the
   matrix in that step (hand-maintained matrix) — not later.
4. Coverage figures given to the user come from the checker output, not estimates.
5. `Standard` requirements still need a UAT step so the client can verify them.

## Final coverage report

At sign-off write `docs/spec/coverage-report.md`: checker output, counts by Fit and
Status, list of Deferred items with reasons, list of `Assumed` requirements the
client has not yet confirmed, and the test run summary.

## See Also

- `frappe-requirements-analyst` — creates the register this matrix is built from
- `frappe-test-planner`, `frappe-tdd-tests` — tests that move rows to Verified
