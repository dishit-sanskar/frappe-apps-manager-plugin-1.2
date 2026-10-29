---
description: Build an ERPNext v16 app from BRD / To-Be / FRS documents, one feature slice at a time, each approved by the developer's review checklist
argument-hint: <documents folder or files> [--site <site>] [--test-site <site>] [--app <app>] [--from <phase or slice, e.g. 2 or S03>]
---

# Frappe Build From Docs

Turn requirement documents into a working ERPNext v16 custom app on the local bench,
**one feature slice at a time**. A slice is one business document end to end (DocTypes,
fields on standard DocTypes, logic, form UI, prints, reports, permissions, tests). Each
slice is reviewed by the developer item by item; **the next slice starts only when every
item of the current slice is approved.**

Nothing is hidden from the user. Three kinds of checklist, all approved the same way
(tick to approve, comment to reject, say "review done"):

| Gate | File | What the user approves |
|---|---|---|
| 1 Flows | `docs/spec/review/flows-review.md` | each business flow the analyst found: explanation, diagram, steps, who does what, what is standard vs developed, each assumption |
| 2 Plan | `docs/spec/review/plan-review.md` | build order, and per slice what ERPNext already covers and what will be developed |
| Each slice | `docs/spec/slices/S0x-…-review.md` | what was built, item by item |

`docs/spec/PROGRESS.md` always shows where things stand: per slice, what is done and
approved, what is being fixed, what waits for review and what is still to develop.

Arguments: `$ARGUMENTS`

## Roles

You (the main session) are the **orchestrator**: you run every `bench` command, edit
`hooks.py`, run the tools, and dispatch the agents with the Agent tool. Subagents cannot
start other subagents.

| Phase | Agent (`subagent_type`) | Output |
|---|---|---|
| 1 Requirements | `frappe-apps-manager:frappe-business-analyst` | `01-requirements.md`, `02-fit-gap.md` |
| 2 Design | `frappe-apps-manager:frappe-architect` | `design.yaml` (all slices), ADRs only for real decisions |
| 3 Per slice: logic | `frappe-apps-manager:frappe-developer` | controllers, hooks requests, server tests |
| 3 Per slice: UI | `frappe-apps-manager:frappe-ui-ux` | form scripts, layouts, print formats, list views |
| 3 Per slice: verify | `frappe-apps-manager:frappe-tester` | acceptance tests written from the brief, test report |
| 4 Docs | `frappe-apps-manager:frappe-technical-writer` | user and admin guides |

Tools (load the skills before first use): `frappe-requirements-analyst` (document
conversion), `frappe-slice-builder` (`slice_tool.py`, `browser_check.mjs`),
`frappe-traceability` (`trace_tags.py`, `check_traceability.py`). Below,
`T="./env/bin/python <slice-builder-dir>/scripts/slice_tool.py"` and `SPEC=apps/<app>/docs/spec`.

## Phase 0 — Preflight (also on every resume)

From the **bench folder**. Report all failures together.

1. `sites/common_site_config.json` exists; `bench version` shows frappe **16.x** and
   erpnext **16.x** (else stop, point to `frappe-ops-upgrades`).
2. **Dev site** (`--site`, or the only site): ERPNext and the app installed
   (`bench --site <site> list-apps`), `"developer_mode": 1` in its `site_config.json`.
3. **Test site** (`--test-site`, default `test.localhost`): must exist with ERPNext and the
   app installed and `allow_tests` set. If missing, ask the user to create it (it needs the
   database root password, so they run it):
   `! bench new-site test.localhost --admin-password admin --install-app erpnext --install-app <app> && bench --site test.localhost set-config allow_tests true`
   Tests always run on the test site, so the developer's dev site stays clean for review.
4. **Server running**: `bench start` in another terminal (Redis + web). Check with
   `curl -s -o /dev/null -w "%{http_code}" -H "Host: <site>" http://127.0.0.1:8000/api/method/ping`.
5. **Browser check login**: the user must have started Claude with `FRAPPE_PASSWORD` set for
   the dev site's Administrator (or another System Manager given with `--user`). If it is not
   set, ask them to restart Claude with `FRAPPE_PASSWORD=... claude ...`; never guess passwords.
6. **App** (`--app`): `apps/<app>` exists and is a git repo with a clean tree (else ask the
   user to commit or stash). If missing, ask the user to run `! bench new-app <app>`.
7. **Permission to commit**: ask once whether each approved slice may be committed
   automatically. Record the answer in `STATUS.md`.
8. **Frappe MCP** (the site's built-in server at `<site-url>/api/v2/mcp`, configured by this
   plugin as `frappe`): call its `search_doctypes` tool with `Sales Invoice`. If the tool is
   missing or fails, tell the user once, then continue (agents fall back to read-only
   `bench console`):
   - restart Claude with `FRAPPE_SITE_URL=http://<site>:8000 FRAPPE_API_KEY=... FRAPPE_API_SECRET=...`
   - the keys belong to a **dedicated dev-site user** (User → API Access → Generate Keys) with
     only the roles the agents need to read; never Administrator, never a production site
   - MCP must be enabled: System Settings → AI Agents → Enable MCP
   A hook allows the read tools, asks before other writes, and denies deletes and schema writes
   (including the same through `call_method`, e.g. `frappe.client.delete` / `frappe.client.insert`
   of a Custom Field).
9. **When to pause during a slice**: ask once (AskUserQuestion) and record it in `STATUS.md`:
   `on-issue` (recommended: only on failing tests, out-of-scope changes, schema requests or open
   questions), `every-task`, `per-group` (after each DocType's tasks), or `slice` (only at the
   review checklist).
10. **Resume**: if `$SPEC/STATUS.md` exists, continue from the first unfinished phase or slice
   (or `--from`). Approved slices are never rebuilt.

`$SPEC/STATUS.md`:

```markdown
# Build Status — <app> (dev <site>, tests <test-site>, ERPNext v16)

Commit approved slices automatically: yes/no
Pause during a slice: on-issue / every-task / per-group / slice

| Step | Status | Started | Finished | Notes |
|---|---|---|---|---|
| 1 Requirements | | | | |
| 1 Flow approval | | | | review rounds: 0 |
| 2 Design | | | | |
| 2 Plan approval | | | | review rounds: 0 |
| S01 <title> | | | | review rounds: 0 |
| ... | | | | |
| 4 Docs & sign-off | | | | |
```

Record start/finish times for every step and every agent call — they show where time goes.

## Phase 1 — Requirements and flow approval

1. Dispatch the Business Analyst with the documents and their roles. It writes the
   register, the fit-gap and `flows.yaml` (every business flow, step by step).
2. **One questionnaire**: show every open question (`OQ-`) with its proposed default,
   grouped by impact. Ask the ones that change scope or design with AskUserQuestion (up to
   4 per call); offer to accept the rest as defaults in one go. Write the answers into the
   register, `flows.yaml` and affected requirements. Do not come back with more questions
   later unless a document is genuinely silent on something new.
3. **Gate 1 — flow approval**: `$T flows --app <app>`, then `$T progress --app <app>`. Tell
   the user, in chat: a table of the flows (title, steps, standard vs to develop, roles),
   the checklist path, and that each flow's explanation and diagram are in the file
   (Markdown preview; the diagrams are Mermaid). **Then stop and wait.**
4. When the user says the review is done: `$T review-status --app <app> --gate flows --json`.
   Rejected items → send the items and comments to the Business Analyst to correct
   `flows.yaml` (and the register / fit-gap when a requirement changes), write `fixes.json`
   (`{"F02": "what changed"}`), run `$T flows --app <app> --fix-notes fixes.json`, stop and
   wait again. All approved → Phase 2.

## Phase 2 — Design and plan approval

Dispatch the Architect with the approved flows: produce `$SPEC/design.yaml` covering **all
slices** (format: `frappe-slice-builder` → `references/design-yaml.md`), ordered by the
To-Be process with dependencies first, foundation first (roles, settings, warehouses,
defaults), each slice with a one-line `summary`. Every requirement belongs to exactly one
slice or is Out of Scope. Then run `$T validate --app <app>` until it passes.

**Gate 2 — plan approval**: `$T plan --app <app>` and `$T progress --app <app>`. Tell the user,
in chat: the slice table (order, title, flows covered, standard vs to develop), new vs
extended DocTypes, roles, workflows, and the checklist path. **Then stop and wait.** On
"review done": `$T review-status --app <app> --gate plan --json`. Rejected items go to the
Architect (change design.yaml, report what changed per item) → validate → `$T plan --app <app>
--fix-notes fixes.json` → wait again. When everything is approved, set each slice
`status: pending` and start Phase 3.

**Existing project** (built before this flow): the Architect writes design.yaml for what
exists too, marking those slices `generate: false`. They still go through the review
checklist below, so the developer approves them like any other slice.

## Phase 3 — Slices, strictly one at a time

For the next slice `S` (first not `approved` in design.yaml):

1. **Start**: `BASE=$(git -C apps/<app> rev-parse HEAD)`; update STATUS. Tell the user what
   this slice will deliver: its summary, the flow steps it covers, and its *To be developed*
   list from the plan.
2. **Generate** (skip if `generate: false`): `$T generate --app <app> --slice S`. Apply its
   `TODO hooks.py` lines to `hooks.py` yourself. Then
   `bench --site <site> migrate && bench --site <test-site> migrate`.
3. **Brief**: `$T brief --app <app> --slice S` → `$SPEC/slices/S-<title>.md`.
4. **Build — small tasks, checkpoint after each.** `$T tasks --app <app> --slice S --mode <pause mode>`
   splits the slice into tasks (one DocType controller, the logic on one standard DocType, one
   report, permissions, and the UI side of each); show the task list in one line per task. Then
   run the developer and UI/UX lines **in parallel**, each as a loop:
   1. `$T next --app <app> --slice S --owner developer --json` (or `--owner ui-ux`). It takes a git
      checkpoint, arms the guard hook, and writes the task prompt. `"task": null` → this line is
      finished (`done: true`) or waiting on the other line.
   2. Dispatch the owner agent with: *"Bench `<bench>`, dev site `<site>`, test site `<test-site>`.
      Your task is in `<prompt path>`: read it and do only that task."* When `continue_agent` is
      true and the previous agent of this line is still available, send the same message to it
      with SendMessage instead of starting a new agent. Otherwise always start a new agent, even
      in the same group: `next` says false once the previous agent has done 2 tasks or sat idle
      over 5 minutes, when resuming it costs more than a fresh start (`agent_reason` says why).
      Pass `model: "sonnet"` when the result says `"model": "sonnet"`.
   3. `$T checkpoint --app <app> --slice S --task <T> --json`. It verifies the task itself (files
      in scope, a passing test run recorded by the guard, `# REQ:` tags, no `TODO(rule` stubs left)
      rather than trusting the agent's report.
      - The result's `message` is written for the user: what the task finished, its tests and
        requirements, what is carried forward (handoff notes, hooks changes still to apply,
        review-checklist items) and which tasks come next. **At every pause show `message`
        verbatim** and put its first two lines in the AskUserQuestion question; do not rewrite
        it into your own summary.
      - `continue` → apply `hooks` requests to `hooks.py` once **no** task is running (the guard
        blocks edits to files outside running tasks), print one line ("S03 T02 Logic on Purchase
        Receipt: done, 7/7 tests") and loop.
      - `pause` with issues → fix within the task: send the issues to the same agent (SendMessage)
        once, then checkpoint again. Still failing, or the issue needs a decision (schema request,
        open question) → ask the user with AskUserQuestion: **Continue** (`--accept`), **Fix with
        my comment** (send it to the agent), **Undo the task** (run the `restore` command, then
        `next` starts it again), **Stop here**. Schema requests go to the Architect +
        regenerate, then the task continues.
      - `pause` without issues (mode `every-task` / `per-group`) → one line and the same
        AskUserQuestion, without the undo option unless asked.
   Keep only the one-line results in the conversation: details stay in `$SPEC/slices/S-tasks/`.
   Hand-built slices (`generate: false`) and fixes after review skip the task loop: dispatch the
   owner agent with the brief and the review items as before.
5. **Integrate**: apply the agents' *Requests* (usually `hooks.py`), then
   `bench --site <site> migrate && bench --site <test-site> migrate`, `bench --site <site> clear-cache`.
   Run `bench build --app <app>` only if files under `public/` changed.
6. **Verify — Tester**: dispatch with the brief: *"Write acceptance tests from the brief's
   requirements and acceptance criteria, not from the code, in
   `<app>/tests/acceptance/test_<slice>.py`; run them and the slice's other tests on the test
   site; report failures with the requirement ID. Do not fix product code."*
7. **Fix** (at most 2 rounds): send each failure to the agent that owns the failing file,
   re-run the failing tests. Still failing after 2 rounds → stop and ask the user.
8. **Browser check**: `$T routes --app <app> --slice S > $SPEC/slices/S-routes.txt`, then
   `node <slice-builder-dir>/scripts/browser_check.mjs --base http://<site>:8000 --routes-file $SPEC/slices/S-routes.txt --out $SPEC/slices/S-browser.json --shots $SPEC/slices/S-shots`.
   Failures go back to the UI/UX agent (form scripts) or Developer (server errors) as in step 7.
9. **Trace**: `./env/bin/python <traceability-dir>/scripts/trace_tags.py --app <app>`.
10. **Review checklist**:
    `$T review --app <app> --slice S --tests "<passed/failed summary>" --browser $SPEC/slices/S-browser.json --base-commit $BASE --base-url http://<site>:8000`.
    Tell the developer, in a few lines: the checklist path, the site URL, what the slice
    contains, and how to review (tick to approve, write a comment to reject, then say
    "review done"). Refresh `$T progress --app <app>`. **Then stop and wait.** Do not start
    the next slice.
11. **When the developer says the review is done**: `$T review-status --app <app> --slice S --json`.
    - **Rejected or unreviewed items** → for each rejected item, send its text and the
      developer's comment to the owning agent (Developer: logic, permissions, server errors;
      UI/UX: layout, form behaviour, prints; Architect + regenerate: field or schema changes in
      design.yaml). Then repeat steps 5–9 as needed, write `fixes.json` (`{"A1": "what was
      changed"}`) and run `$T review ... --fix-notes fixes.json` for the next round. Approved
      items stay approved. Refresh progress. Stop and wait again. Unreviewed items are simply
      asked about again.
    - **All approved** → set the slice `status: approved` in design.yaml, re-run `trace_tags.py`,
      update STATUS (rounds, times). After the **first** approved slice, write
      `$SPEC/conventions.md`: the patterns it established (file layout, test factories,
      permission helper, form-script structure, naming) for later slices to copy. Commit
      if allowed: `git -C apps/<app> add -A && git -C apps/<app> commit -m "feat: <slice title> (S)"`.
      **Learn from the slice:** `$T lessons --app <app> --slice S` logs every rejected review item
      to the company knowledge base (`$FRAPPE_KB_DIR`, default `~/.claude/frappe-kb`) and prints
      the new ones; turn those that apply beyond this project into one-line rules in
      `$FRAPPE_KB_DIR/lessons.md` (edit or merge existing rules, keep it under 60 lines). Then
      `$T archive --app <app> --slice S` stores the approved slice in the pattern library, so task
      prompts in later projects point to it. Refresh progress, then go to the next slice.

## Phase 4 — Finish

1. Full test run on the test site: `bench --site <test-site> run-tests --app <app>`.
2. `trace_tags.py --app <app> --check` — every in-scope requirement Verified.
3. Dispatch the Technical Writer: `user-guide/<role>.md` per role following the To-Be
   processes, `admin-guide.md` (setup, settings, fixtures, scheduled jobs).
4. Write `$SPEC/coverage-report.md`: checker output, deferred items with reasons, assumptions
   still unconfirmed by the client, review rounds per slice, time per step from STATUS, and
   `$T metrics --app <app>` (minutes, tool calls, test runs and `apps/erpnext` reads per agent
   run — the place to look when a slice was slow).

## Rules

- Never edit `apps/frappe` or `apps/erpnext`. Never run `reinstall`, `drop-site`, `restore`
  or `/frappe-db-reset`. An always-on hook refuses these and other destructive commands (SQL
  DROP/TRUNCATE/DELETE, `rm -r` on sites/apps/env, `git reset --hard`, `git push --force`) for
  every agent, and asks the user for approval when this session runs one.
- Schema changes go through design.yaml and `generate`; never hand-edit generated DocType JSON,
  and never create schema documents through the Frappe MCP tools.
- Never tick a task done by hand: only `checkpoint` (or `checkpoint --accept` after the user
  decides) does that. Never run git commands that move HEAD while a task is running.
- Only the user's checklists approve flows, the plan and slices. Never tick review items yourself.
- Run `$T progress --app <app>` after every step that changes state, so `PROGRESS.md` is current.
- Requirement IDs stay in code comments, never in anything users see (the lint hook enforces it).
- Keep the user posted with one-line updates during a slice ("S03 GRN & QC: generated,
  agents building", "S03: 41/41 tests, browser check OK, checklist ready").
