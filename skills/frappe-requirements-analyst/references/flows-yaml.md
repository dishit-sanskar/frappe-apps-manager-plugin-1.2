# flows.yaml Reference

`apps/<app>/docs/spec/flows.yaml` describes every business flow found in the documents,
step by step, in words the client uses. The Business Analyst writes it after the register
and fit-gap. `slice_tool.py flows` turns it into `docs/spec/review/flows-review.md`: per
flow, a diagram, a step table, what standard ERPNext covers and what will be developed,
and a checkbox the user approves. **Design starts only when every flow is approved.**

Write for a business reader: no field names or Python, roles as the client names them,
records as ERPNext names them ("Purchase Receipt (GRN)").

```yaml
flows:
  - id: F01                       # F01, F02 ... in To-Be order
    title: Inward receipt of material
    purpose: Receive purchased material, check its quality and stock what passes.
    trigger: Supplier truck arrives at the gate          # what starts the flow
    ends_with: Accepted stock in Inward Warehouse        # the outcome
    roles: [Security, Store Team, QC Team, Purchase Manager]
    source: "To-Be §2 Inward, Figure 3; FRS §4.1"        # where it comes from
    steps:
      - actor: Security
        action: Record vehicle, driver, supplier, POs and weigh slip
        record: Gate Pass (Inward)                       # the ERPNext record used or created
        reqs: [PS-INW-01, FR-020]                        # register IDs this step covers
        # fit: build                                     # optional; else the highest fit of reqs
      - actor: System
        action: Set accepted / rejected quantity from the QC result
        record: Purchase Receipt
        reqs: [PS-INW-04]
        branches:                                        # decisions: instead of "next step"
          - {if: QC passed, to: 3}                       # step number, or end
          - {if: QC failed, to: 4}
      - {actor: Store Team, action: Submit GRN to Inward Warehouse, record: Purchase Receipt,
         reqs: [PS-INW-05], end: true}                   # end: true = flow ends after this step
      - {actor: Store Team, action: Submit GRN to Rejected Warehouse, record: Purchase Receipt,
         reqs: [PS-INW-06]}
    requirements: [RPT-003]       # optional: IDs that belong to the flow but to no single step
    exceptions:                   # special cases, in one line each
      - Partial acceptance is allowed with a mandatory QC remark
    outputs: [Inward Gate Pass print, GRN, Daily Inward Register]
    assumptions:                  # each becomes its own "Confirm:" item to approve
      - {text: Weighbridge readings are typed in by hand, default: manual entry, oq: OQ-007}
      - Sample size is entered manually on the inspection
out_of_scope:                     # agreed exclusions (fit-gap Out of Scope rows are added automatically)
  - Weighbridge device integration
```

Fit values: `standard`, `configure` (shown green: no development), `extend`, `build`,
`integrate` (orange: developed), `out of scope` (grey).

## Rules

- One flow per To-Be process (or per distinct sub-process when one is long, 15+ steps).
- Steps follow the To-Be order; every PS- row of the process belongs to a step.
- Every decision in the To-Be diagram is a step with `branches`.
- Requirements that are not part of a flow (masters, reports, non-functional) need no
  entry: the review lists them under *Other requirements* for the user to confirm.
- Assumptions are open questions answered with a default; list them so the user sees
  and confirms each one.

## After the user's review

`slice_tool.py review-status --gate flows --json` lists rejected items with comments.
Correct `flows.yaml` (and the register / fit-gap when a requirement changes), write
`{"F01": "what changed"}` to a JSON file and run `slice_tool.py flows --fix-notes <file>`.
An approved flow whose content changes must be approved again (its version mark changes).
