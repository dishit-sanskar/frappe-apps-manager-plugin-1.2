---
name: frappe-requirements-analyst
description: >
  Use when given BRD (Business Requirements Document), To-Be Process, FRS (Functional
  Requirements Specification), SRS or similar requirement documents (.docx preferred,
  .doc or .pdf accepted) that must be turned into an ERPNext implementation. Converts
  the documents to Markdown, extracts every requirement with a stable ID and source
  reference, models To-Be process steps, cross-checks BRD vs FRS vs To-Be for gaps and
  conflicts, and classifies each requirement Standard / Configure / Extend / Build /
  Integrate against ERPNext v16. Produces docs/spec/01-requirements.md and
  docs/spec/02-fit-gap.md. Keywords: BRD, FRS, SRS, To-Be process, As-Is, requirement
  document, business requirements, functional specification, fit-gap, gap analysis,
  requirements register, docx, requirement IDs, process flow.
license: MIT
compatibility: "Claude Code. Frappe/ERPNext v16 on a local bench."
---

# Frappe Requirements Analyst

Turns client requirement documents into a **requirements register** and an
**ERPNext v16 fit-gap**, the two inputs every later phase of
`/frappe-build-from-docs` depends on.

## Expect variation — work from meaning, not layout

Every client, consultant and document version is formatted differently. Nothing in
this skill depends on a particular template, heading wording or label set:

- **Document types are identified by content, not file name.** A set may be a BRD,
  a To-Be Process and an FRS, or a single combined document, an SRS, a "Functional Design", a process
  narrative, slides saved as PDF, or only two of the three. Classify each document
  (or each chapter of a combined one) by what it contains: objectives and scope →
  BRD role; process flow and narrative → To-Be role; per-form fields and rules → FRS
  role. Missing roles become `OQ-` gaps, not blockers.
- **Structure may be missing or inconsistent.** The converter resolves custom heading
  styles and outline levels, and falls back to numbered lines ("3.2 Title") and short
  bold lines when no styles exist — check its warnings and, where it guessed, verify
  section boundaries against the original numbering.
- **Diagrams come in many forms**: pasted images, Visio/EMF pictures (converted to
  PNG), flowcharts drawn with Word shapes (their labels appear in the text as
  "diagram drawn with shapes" — open the original if the order matters), SmartArt,
  or embedded Visio/Excel files (copied to `*_media/embedded/`).
- **Labels and terms vary.** "Validation Rules", "Business Rules", "Checks",
  "Constraints" and "Controls" mean the same thing; so do "Mandatory", "Required" and
  "(M)"; and "Sale Order", "SO" and "Buyer Order". Map by meaning; record client terms
  in the Glossary.
- **Tables may replace prose** (field-list tables with Field / Type / Mandatory /
  Remarks columns, requirement tables with their own IDs, RACI tables). Treat a field
  table exactly like a "fields" block and keep the client's IDs.
- **Reviewer comments and tracked changes** are surfaced by the converter; comments
  are often unresolved questions → `OQ-`; tracked changes mean the text is not final.

## Quick Reference

| Step | Output | Tool |
|---|---|---|
| 1. Convert documents | `docs/spec/00-sources/*.md`, `*_media/`, `INDEX.md` | `scripts/convert_docs.py` |
| 2. Look at figures | Figures checklist filled in | Read tool on each `*_figures.md` entry |
| 3. Extract requirements | `docs/spec/01-requirements.md` | [register-template.md](references/register-template.md) |
| 4. Cross-check | Gaps / conflicts / open questions in the register | Checklist below |
| 5. Fit-gap | `docs/spec/02-fit-gap.md` | [fit-gap-guide.md](references/fit-gap-guide.md) |
| 6. Business flows | `docs/spec/flows.yaml` → flow approval checklist | [flows-yaml.md](references/flows-yaml.md) |

## Step 1 — Convert the documents

`.docx` is the preferred format: it converts without any extra software and keeps
headings, numbered lists, tables, comments and embedded diagrams. `.doc` needs
LibreOffice. `.pdf` uses `pdftotext` (poppler-utils): text comes through, but
diagrams are only flagged by page and must be read visually.

`<skill-dir>` below is this skill's base directory, shown when the skill loads.

```bash
python3 -I <skill-dir>/scripts/convert_docs.py \
  --out docs/spec/00-sources path/to/BRD.docx path/to/ToBe.docx path/to/FRS.docx
# or pass a folder: ... --out docs/spec/00-sources path/to/requirements/
```

Then read `docs/spec/00-sources/INDEX.md`. Act on every warning:
- **figures** → each document with figures gets a `<doc>_figures.md` checklist (figure,
  file or PDF page, section). Open **every** figure with the Read tool, fill in *Kind*
  and *Requirements taken from it*. Skip only logos and cover art (mark Kind = Logo).
  See [Reading figures](#reading-figures).
- **section described only by figures** → the requirements for that section exist
  only in the image (typical for reports). Extract columns, filters, grouping and
  colour rules from it; if the image is not enough, raise an `OQ-`.
- **empty sections** → a heading with no content is a **gap**: raise an `OQ-` (e.g.
  "FRS §3.3 has a heading but no specification").
- **PDF pages with figures** → read those pages with the Read tool (`pages: "12"`).
  PDF bullets usually come through as `●` / `○` / `•`, with indentation for nesting.
- **scanned / no text PDF** → read the PDF directly with the Read tool, `pages` in
  ranges of ≤ 20.
- **headings inferred** → the document had no heading styles, so headings were guessed
  from numbering and bold lines; check them against the original numbering so source
  references stay precise.
- **tracked changes / reviewer comments / embedded files** → see
  [Expect variation](#expect-variation--work-from-meaning-not-layout).

Numbered lists keep their real numbers, so references such as "see point 7" or
"the downstream flow (point 20)" point at the matching list item — resolve them and
cite both places.

Never edit or delete the user's original documents.

### Document control first

Before extracting anything, record each document's **version, issue date, author,
approver, module and the client's process IDs** (from a version table, cover page,
header/footer or file name — wherever they appear) in the register's *Document
Control* table. When two
documents disagree, the later-dated one is the default, but the conflict is still an
`OQ-` for the client to confirm. Note references to documents you were not given
(e.g. "tolerances are defined in the Quality FRS") as `OQ-` of type
*External dependency*.

### Reading figures

| Figure kind | What to extract | Goes into |
|---|---|---|
| Process flowchart (To-Be / As-Is) | Every box in order, every decision with each branch label and where it loops back, actors, documents | Process Steps (`PS-`); the As-Is only for context and pain points |
| Form mockup / screenshot | Tabs, sections, field labels and order, child-table columns, buttons (e.g. *Notify Approver*), status badges and colours, read-only vs editable, sample values | Entity spec (*Figures* column) and FR acceptance criteria; sample values become test data |
| Report / dashboard mockup | Columns, filters, grouping, totals, colour rules, chart type, drill-downs | `RPT-` rows |
| Print format mockup | Header/footer, fields, tables, barcode/QR, page size | `PF-` rows |
| Symbols legend | Meaning of shapes used in the flowcharts | Use when reading the flowcharts |

A mockup is a requirement only where the text does not contradict it; if text and
mockup disagree, raise an `OQ-`.

## Step 2 — Extract requirements

First skim every converted document (headings, `INDEX.md`, figure lists) to see the
whole scope, then extract section by section, writing rows to the register as you go.
Work per document *role* (see [Expect variation](#expect-variation--work-from-meaning-not-layout)):

| Document role | What to extract | ID prefix |
|---|---|---|
| BRD | Business objectives, business rules, scope in / out, stakeholders, KPIs | `BR-` |
| To-Be Process | Each process, its steps, actor (role), trigger, inputs/outputs, decisions, approvals, SLAs, exceptions | `PS-<PROC>-` (e.g. `PS-P2P-07`) |
| FRS | Functional requirements, field lists, validations, calculations, notifications, print formats | `FR-` |
| Any | Reports / dashboards | `RPT-` |
| Any | Print formats (job cards, tickets, plan sheets) | `PF-` |
| Any | Integrations with external systems | `INT-` |
| Any | Data migration / opening balances / masters to load | `DM-` |
| Any | Performance, security, audit, availability, localisation | `NFR-` |

Rules:
1. **One requirement = one testable statement.** Split "shall create and approve and
   email" into three rows.
2. **Keep the client's own IDs** in the *Source ID* column; never renumber them away.
   Our ID is for traceability across phases and never changes once assigned — new
   requirements get the next number, removed ones are marked `Withdrawn`, not deleted.
3. **Cite the source precisely**: `FRS §4.2.3`, `BRD p.12`, `ToBe Fig 3 step 5`.
4. **Quote, don't paraphrase, numbers and rules** (thresholds, rates, days, roles).
5. **Capture actors as roles**, not people ("Finance Head", not "Ramesh").
6. Write each requirement as *"The system shall …"* plus acceptance criteria the
   Tester can verify without asking anyone.
7. **Status words in the documents are signals.** "(proposed)", "to be confirmed",
   "value to be confirmed", "final … to be confirmed with the client", "TBD" → the
   requirement is `Assumed` and gets an `OQ-`. Nothing marked proposed is built
   silently.
8. **Glossary.** Client terms often differ from ERPNext names ("Sale Order" → Sales
   Order, "Production Line" → Workstation, "Production Order" → Work Order). Record each in
   the register's *Glossary* and use the ERPNext name from Phase 2 onward.

Use the exact table formats in [register-template.md](references/register-template.md) —
`check_traceability.py` parses them.

### Specification content → Frappe

FRS documents are usually written one section per form, record or report. Capture
each as a row in the register's *Entities* table. Within a section, recognise each
kind of content **by meaning** — the labels below are typical wordings, not required
ones, and the same content may arrive as prose, bullets or a table:

| Content (typical wording) | Frappe meaning | Requirement rows |
|---|---|---|
| Fields — "Header fields", "Other fields", "Field list", a Field/Type/Mandatory table | DocType fields (type inferred from values: date, Yes/No → Check, list of values → Select, another record → Link) | one FR per field group |
| Required — "Mandatory fields", "Required", "(M)", a Mandatory = Y column | `reqd: 1` | one FR listing them |
| Repeating rows — "Band rows", "detail rows", "line items", "one row per …" | Child table DocType | one FR for the child table |
| Auto-filled — "Auto-fetch", "defaults from", "pulled from the Sale Order" | `fetch_from` or server-side set on link change | one FR per source |
| Calculations — "Formula", "Derivation", "Computed as", worked examples | Controller `validate` calculation | one FR per formula, with a worked example from the document as acceptance criterion |
| Rules — "Validation Rules", "Business Rules", "Checks", "Constraints", "cannot / must / only if" sentences | `validate` / `before_submit` checks | **one FR per rule** (each becomes a test) |
| Approval Workflow | Workflow (states, transitions, roles) | one PS per transition |
| Status | Status field / workflow states, list-view indicators | one FR |
| Naming series | `autoname` / naming series — convert to Frappe syntax: `ABC-YY-.####` → `ABC-.YY.-.####` | one FR |
| Audit trail | `track_changes: 1` (plus Version log); "changes affect future records only" → values copied onto each transaction when it is created | one NFR |
| Permissions / Access | DocPerm per role (read / write / create / submit) | one FR |
| Alerts | Notification or `frappe.publish_realtime` / email | one FR per alert |
| Integration ("reads … feeds …") | Link fields in, downstream DocTypes out | one INT or FR per direction |
| Single / settings record | `issingle: 1` | — |
| Legacy migration | Data Import / patch | `DM-` row |

## Step 3 — Cross-check the documents

Record every finding in the register's *Gaps, Conflicts & Open Questions* table with
an `OQ-` ID, the requirement IDs involved and a proposed default answer.

- [ ] Compare the tables of contents: every business-level section (masters, processes, transactions, reports, print formats) has a matching functional specification, and vice versa
- [ ] Every BRD objective is realised by at least one FR or process step (else: gap)
- [ ] Every FR traces back to a BRD objective or process step (else: possible scope creep)
- [ ] Every To-Be step that creates/changes data has an FR describing fields and validations
- [ ] Every approval in the To-Be has: approver role, condition/threshold, reject path, escalation
- [ ] Numbers agree across documents (thresholds, tax rates, SLA days, approval limits)
- [ ] Every role named anywhere appears in the roles list
- [ ] Every report has columns, filters, and audience
- [ ] Every integration has direction, trigger, frequency, payload, error handling
- [ ] Every print format / email has its layout or content defined
- [ ] Every form mockup agrees with its section text (fields, statuses, buttons)
- [ ] Every flowchart box and decision maps to a process step, and every To-Be text step appears in the flowchart
- [ ] Data migration: which masters and open transactions, from where, cut-over date
- [ ] Multi-company, multi-currency, localisation (e.g. India GST) explicitly stated or asked

Ambiguity is normal. **Do not invent answers**: propose a default, mark it
`Assumed`, and let the user confirm at the phase gate.

## Step 4 — Fit-gap against ERPNext v16

For every requirement decide the **Fit** using
[fit-gap-guide.md](references/fit-gap-guide.md):

| Fit | Meaning | Delivered as |
|---|---|---|
| **Standard** | ERPNext does this out of the box | Nothing to build; UAT step only |
| **Configure** | Settings, roles, permissions, Workflow, Notification, Print Format, naming | Fixtures exported from the custom app |
| **Extend** | Custom fields, property setters, client scripts, doc_events, reports on standard DocTypes | Code + fixtures in the custom app |
| **Build** | New DocType / page / report with no standard equivalent | New DocType, page or report in the custom app |
| **Integrate** | External system exchange | API / webhook / scheduled job in the custom app |
| **Out of Scope** | Excluded by BRD or deferred by the user | Reason in Notes |

Prefer the lowest-effort fit that genuinely meets the requirement. Never build a
new DocType for something ERPNext already models (Customer, Supplier, Item, Sales
Order, Purchase Order, Material Request, Employee, Project, Task, Asset, …) —
extend it instead. Never modify ERPNext/Frappe core files.

## Step 5 — Business flows for the user's approval

Write `docs/spec/flows.yaml` following [flows-yaml.md](references/flows-yaml.md): one
flow per To-Be process, step by step, each step with who does it, the ERPNext record and
the requirement IDs, decisions as branches, assumptions listed for confirmation. The
orchestrator renders it as `docs/spec/review/flows-review.md`, where the user approves
each flow before any design work. Write it for a business reader.

## Done when

- `docs/spec/00-sources/INDEX.md` has no unaddressed warnings, and every `*_figures.md`
  row has *Kind* and *Requirements taken from it* filled in
- `docs/spec/01-requirements.md` lists every requirement, process, role and open question
- `docs/spec/02-fit-gap.md` assigns a Fit and ERPNext object to every requirement
- `docs/spec/flows.yaml` has every To-Be process as a flow, and every PS- row in a step
- Summary for the user: counts by type and by Fit, number of open questions, top risks

## See Also

- `frappe-traceability` — the matrix that carries these IDs through design, code and tests
- `frappe-agent-interpreter` — mapping individual vague requirements to Frappe mechanisms
- `frappe-workflow-generator`, `frappe-permissions` — for approval steps and roles found here
