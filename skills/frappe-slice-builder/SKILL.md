---
name: frappe-slice-builder
description: >
  Use when building a Frappe/ERPNext app one feature slice at a time from a design file:
  writing or changing docs/spec/design.yaml, generating DocTypes / custom fields / roles /
  workflows / report and print stubs from it, producing the per-slice brief agents work
  from, running the headless-browser check on desk pages, and producing and reading the
  developer's review checklist that gates each slice. Used by /frappe-build-from-docs.
  Keywords: feature slice, design.yaml, generate doctype, scaffold doctype from spec,
  slice brief, review checklist, approve slice, browser check, console errors, form not
  loading, headless chrome, vertical slice.
license: MIT
compatibility: "Claude Code. Frappe/ERPNext v16 on a local bench; Node 22+ and Chrome/Chromium for the browser check."
---

# Frappe Slice Builder

A **slice** is one business document end to end: its DocType(s) and child tables, the
fields it adds to standard DocTypes, its logic, form UI, prints, reports, permissions and
tests. Slices are built in order, and the next slice starts only after the developer has
approved every item of the current slice's review checklist.

`<skill-dir>` below is this skill's base directory, shown when the skill loads. Run
everything from the bench folder. `slice_tool.py` needs PyYAML, so use the bench's Python.

```bash
T="./env/bin/python <skill-dir>/scripts/slice_tool.py"
```

## Tools

| Step | Command | Result |
|---|---|---|
| Flow approval (gate 1) | `$T flows --app <app> [--fix-notes f.json]` | `docs/spec/review/flows-review.md` from `flows.yaml`: diagram, steps, standard vs developed, one item per flow and per assumption |
| Plan approval (gate 2) | `$T plan --app <app> [--fix-notes f.json]` | `docs/spec/review/plan-review.md`: build order, and per slice what ERPNext already covers and what is developed |
| Read a gate | `$T review-status --app <app> --gate flows\|plan [--json]` | same output as a slice review |
| Progress page | `$T progress --app <app>` | `docs/spec/PROGRESS.md`: gates, and per slice done / being fixed / waiting for review / to develop |
| Check the design | `$T validate --app <app>` | errors (broken links, bad naming series, requirement IDs in labels ...) |
| Generate a slice | `$T generate --app <app> --slice S03` | DocType JSON, fixtures, stubs; `TODO hooks.py ...` lines for the orchestrator |
| Brief for agents | `$T brief --app <app> --slice S03` | `docs/spec/slices/S03-<title>.md` |
| Split into tasks | `$T tasks --app <app> --slice S03 [--mode on-issue\|every-task\|per-group\|slice]` | `docs/spec/slices/S03-tasks.yaml`: one task per DocType controller, standard-DocType logic, report, permissions, and UI part; at most 4 requirements each |
| Start the next task | `$T next --app <app> --slice S03 [--owner developer\|ui-ux] [--json]` | git checkpoint (`refs/frappe-checkpoints/S03/T01-start`), guard armed (`.claude/frappe-active-task.json`), prompt `S03-tasks/T01-prompt.md`; says whether to continue the same agent and which model |
| Check a finished task | `$T checkpoint --app <app> --slice S03 --task T01 [--accept] [--json]` | verifies files in scope, a passing test run, `# REQ:` tags, no `TODO(rule` stubs; `continue` or `pause` with issues, requests and an undo command |
| Learn from a review | `$T lessons --app <app> --slice S03` | rejected items appended to `$FRAPPE_KB_DIR/review-log.jsonl`; new ones printed to turn into rules in `lessons.md` |
| Keep an approved slice | `$T archive --app <app> --slice S03` | brief, design and patch in `$FRAPPE_KB_DIR/patterns/`; later task prompts list similar slices |
| Where time went | `$T metrics --app <app> [--slice S03]` | minutes, tool calls, writes, test runs, `apps/erpnext` reads per agent run (from the SubagentStop hook) |
| Pages to check | `$T routes --app <app> --slice S03 > routes.txt` | desk URLs for the browser check |
| Browser check | `FRAPPE_PASSWORD=... node <skill-dir>/scripts/browser_check.mjs --base http://<site>:8000 --routes-file routes.txt --out browser.json --shots shots/` | exit 1 on console errors, failed requests, error dialogs, or pages that did not render |
| Review checklist | `$T review --app <app> --slice S03 --tests "<summary>" --browser browser.json --base-commit <sha> --base-url http://<site>:8000` | `docs/spec/slices/S03-<title>-review.md` |
| Read the review | `$T review-status --app <app> --slice S03 [--json]` | approved / rejected (with comments) / not reviewed; exit 0 only when all approved |
| Next round | `$T review ... --fix-notes fixes.json` | approved items stay ticked; rejected items reset with the old comment and `Fixed:` note |

The design format is in [references/design-yaml.md](references/design-yaml.md).

## Rules

- **Schema lives in design.yaml.** To change a field, change design.yaml and regenerate.
  `generate` refuses to overwrite DocType JSON edited by hand (`--force` only when the
  user agrees to lose that edit).
- **Stubs are written once** and then belong to their owner (controller → Developer, form
  script → UI/UX). Regenerating never touches them.
- **hooks.py is edited by the orchestrator only**, using the `TODO` lines from `generate`
  and the *Requests* in agents' returns.
- **Requirement IDs go in comments** (`# REQ: FR-012`), never in labels, descriptions or
  messages. `validate` and the lint hook both reject IDs in user-facing text.
- **A slice is approved only by the developer's checklist**, never by tests alone. A
  comment on any item means *not approved*, even if the box is ticked.
- **Automated results are re-confirmed every round** (section G is never carried over).
- **Tasks are checked by the script, not the agent.** A task is done only when `checkpoint`
  passes or the user accepts it (`--accept`). While a task runs, the plugin's guard hook blocks
  writes outside its files, other apps, migrate / install / build and git commands, stops the
  agent at its write budget, and after 3 identical failing test runs.
- **Company knowledge** lives in `$FRAPPE_KB_DIR` (default `~/.claude/frappe-kb`; point it at a
  shared git repo for the team): `lessons.md` (rules from past reviews, included in every task
  prompt), `review-log.jsonl`, `patterns/`.
- **Changed content needs a new approval.** Flow and plan items carry a version mark of
  their content; if the flow or slice changes, its approval is reset.

## Writing review_extra steps

Add 2–5 end-to-end steps per slice that a person can follow in the browser, with real
actions and an expected result, for example:
"Create an Inward Gate Pass for a supplier with an open PO → Get Items fills the table →
Submit → Print shows truck no., items and signatures."

## See Also

- `frappe-traceability` — `trace_tags.py` builds the matrix from `# REQ:` tags and slice approval
- `frappe-requirements-analyst` — produces the requirements the slices reference
