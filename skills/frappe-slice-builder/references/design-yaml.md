# design.yaml Reference

`apps/<app>/docs/spec/design.yaml` is the single design source. The Architect writes it;
`slice_tool.py generate` turns each slice into DocTypes, fixtures and stubs; every agent
reads the per-slice brief generated from it. To change a schema, change this file and
regenerate — never hand-edit generated DocType JSON.

## Top level

```yaml
version: 1
app: my_app                 # Python package name (apps/my_app/my_app)
roles:                      # custom roles the app adds (standard ERPNext roles need no entry)
  - name: Gate Security
  - name: QC User
  - {name: Portal Vendor, desk_access: false}
slices:                     # build order = list order; each slice depends only on earlier ones
  - ...
```

## Slice

```yaml
- id: S02                   # stable, S01, S02 ...
  title: Gate Pass
  summary: Security records every truck in or out and prints the gate pass.   # one line for the plan review
  status: pending           # pending | approved   (the orchestrator sets approved)
  generate: true            # false = built by hand before design.yaml existed; never regenerated
  depends_on: [S01]
  requirements: [FR-020, FR-021, PS-INW-01]   # IDs from 01-requirements.md
  notes: |                  # design decisions the agents must know (short)
    Informational only: a Gate Pass posts no stock.
  doctypes: [...]           # new DocTypes, child tables first or last - order does not matter
                            # requirements are given an owner below (doctype / custom-field group /
                            # report `requirements:` or `rules:`); the rest are matched by DocType
                            # name in the requirement text, then fall into a "Remaining rules" task
  custom_fields: [...]      # fields added to standard DocTypes
  property_setters: [...]
  workflows: [...]
  standard_permissions:     # permissions on standard DocTypes (applied in code by the Developer)
    Purchase Receipt: {Gate Security: L, QC User: R}
  print_formats:
    - {name: Inward Gate Pass, doctype: Gate Pass, module: My Gate, notes: "A5, logo, signatures"}
  reports:
    - {name: Gate Register, ref_doctype: Gate Pass, module: My Gate, type: Script Report,
       roles: [Stock User, Stock Manager], notes: "submitted passes, filter by date/direction",
       requirements: [RPT-01]}            # optional: requirements the report implements
  review_extra:             # extra end-to-end steps for the developer's review checklist
    - "Create an Inward Gate Pass for a supplier with an open PO, use Get Items, submit, print"
```

## DocType

```yaml
- name: Gate Pass
  module: My Gate            # created (folder + modules.txt) if missing
  submittable: true          # adds amended_from automatically
  autoname: "naming_series:" # or hash | autoincrement | prompt | field:<fieldname> | format:GP-{####}
  naming_series: [GP-IN-.YYYY.-.####, GP-OUT-.YYYY.-.####]   # date parts need dots
  title_field: party_name
  search_fields: [truck_no, party_name]
  sort_field: posting_date   # default creation
  track_changes: true        # default true (audit trail); child tables never track
  permissions:               # letters: R read, W write, C create, S submit, X cancel, A amend,
    Gate Security: RWC       #          D delete, P print, E export, M email, H share, T report, L select
    Stock Manager: RWCSXAP   # System Manager gets full access unless system_manager: false
  requirements: [FR-022]     # optional: requirements this DocType's controller implements
  rules:                     # optional: validation rules -> controller + test stubs, one task
    - {id: FR-020, rule: Posting date cannot be in the future}                 # event: validate
    - {id: FR-021, rule: Truck number is required to submit, event: before_submit, method: validate_truck}
  fields:
    - fieldname: direction
      type: Select
      options: [Inward, Outward]          # list; a blank first option is added for Select
      reqd: true
      in_list_view: true
      in_standard_filter: true
    - column                              # Column Break
    - {fieldname: posting_date, type: Date, label: Date, default: Today, reqd: true}
    - section: Vehicle                    # Section Break with label
    - {fieldname: truck_no, type: Data, label: Truck / Vehicle No., reqd: true}
    - {section: Items, collapsible: true}
    - {fieldname: items, type: Table, options: Gate Pass Item, reqd: true}
    - {fieldname: purchase_orders, type: Table MultiSelect, options: Gate Pass Purchase Order}
    - tab: Sign-off                       # Tab Break
    - {fieldname: approved_by, type: Link, options: User}
```

Any other DocField property (`depends_on`, `mandatory_depends_on`, `read_only_depends_on`,
`fetch_from`, `fetch_if_empty`, `read_only`, `hidden`, `allow_on_submit`, `no_copy`,
`print_hide`, `bold`, `precision`, `non_negative`, `unique`, `set_only_once`, `description`,
`ignore_user_permissions`, `in_global_search`, `columns`, ...) is passed through unchanged.
`true`/`false` become `1`/`0`. `label` defaults to the fieldname in title case.

`rules`: `id` must be one of the slice's requirements; `event` is `validate` (default),
`before_save`, `before_submit`, `on_submit`, `on_cancel`, `after_insert` or `on_update` (write
`event:`, not `on:` — YAML reads a bare `on` as true); `method` defaults to `validate_` plus the
first words of the rule. `generate` writes the event methods calling one method per rule, each
with a `# TODO(rule FR-020): ...` line, and one skipped test per rule; stubs are written only when
the file does not exist yet.

Child table: `istable: true` (no permissions). A **Table MultiSelect** child must have
exactly one Link field. Single (settings): `issingle: true`.

## Custom fields and property setters

```yaml
custom_fields:
  - doctype: Purchase Receipt
    insert_after: supplier                # first field goes after this; the rest follow in order
    module: My Inward                     # fixture filter module (default: first module of the slice)
    requirements: [FR-030]                # optional: logic on this DocType (overrides/, services/)
    rules:                                # optional, same format as on a DocType (no stubs generated)
      - {id: FR-031, rule: A Gate Pass must be linked before submit, event: before_submit}
    fields:
      - {section: Gate}
      - {fieldname: gate_pass, type: Link, options: Gate Pass, label: Gate Pass}   # becomes custom_gate_pass
property_setters:
  - {doctype: Item, field: has_batch_no, property: default, value: "1", type: Check}
```

## Workflow

```yaml
workflows:
  - name: My GRN Approval
    doctype: Purchase Receipt
    state_field: workflow_state           # add it as a custom field in the same slice
    states:
      - {state: Draft, doc_status: 0, allow_edit: Stock User}
      - {state: Pending QC, doc_status: 0, allow_edit: QC User, style: Warning}
      - {state: Approved, doc_status: 1, allow_edit: Stock Manager, style: Success}
    transitions:
      - {from: Draft, action: Send to QC, to: Pending QC, allowed: Stock User}
      - {from: Pending QC, action: Approve, to: Approved, allowed: QC User, condition: "doc.custom_qc_complete"}
```

## What `generate` writes

| Output | Kind | Owner afterwards |
|---|---|---|
| `<module>/doctype/<dt>/<dt>.json` | managed: rewritten from design.yaml; refuses to overwrite hand edits | design.yaml |
| `<dt>.py`, `<dt>.js`, `test_<dt>.py` | stub, written once | Developer (`.py`), UI/UX (`.js`) |
| `fixtures/custom_field.json`, `property_setter.json`, `role.json`, `workflow*.json` | merged by record name | design.yaml |
| `<module>/report/<r>/` (`.json`, `.py`, `.js`) | stub, written once | Developer (`.py`), UI/UX (`.js`) |
| `<module>/print_format/<pf>/<pf>.json` | stub, written once | UI/UX |
| `docs/spec/generated.json` | manifest of managed files and records | tool |

`generate` never edits `hooks.py`; it prints the entries the slice needs (fixtures filters)
as `TODO` lines for the orchestrator.
