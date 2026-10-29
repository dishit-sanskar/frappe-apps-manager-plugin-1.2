# ERPNext v16 Fit-Gap Guide

Use this to decide **Standard / Configure / Extend / Build / Integrate** for each
requirement. Before classifying anything as *Build*, confirm on the running site
(`bench --site <site> console` → `frappe.get_meta("<DocType>")`, or the Desk) that no
standard DocType already covers it. ERPNext evolves between minor versions; the site
is the source of truth, this table is a starting point.

## Business term → standard ERPNext object

| Requirement talks about… | Start from | App |
|---|---|---|
| Lead, enquiry, opportunity, pipeline | Lead, Opportunity, (CRM app if installed) | ERPNext |
| Quotation, sales order, delivery, sales invoice | Quotation, Sales Order, Delivery Note, Sales Invoice | ERPNext |
| Customer, customer group, credit limit | Customer, Customer Group, credit limit on Customer | ERPNext |
| Purchase request / indent / requisition | Material Request (type Purchase) | ERPNext |
| RFQ, vendor quotation, comparison | Request for Quotation, Supplier Quotation | ERPNext |
| Purchase order, GRN, three-way match | Purchase Order, Purchase Receipt, Purchase Invoice | ERPNext |
| Vendor / supplier registration, scorecard | Supplier, Supplier Scorecard | ERPNext |
| Item master, item variants, UoM, batches, serial nos | Item, Item Variant, UOM, Batch, Serial No / Serial and Batch Bundle | ERPNext |
| Stock transfer, issue, receipt, stock count | Stock Entry, Stock Reconciliation | ERPNext |
| Warehouse, bin location | Warehouse (tree) | ERPNext |
| BOM, work order, job card, routing | BOM, Work Order, Job Card, Routing, Workstation | ERPNext |
| Production / sewing line, machine, work centre, operator count | Workstation (Workstation Type; custom fields for line attributes) | ERPNext |
| Factory calendar, weekly off, holidays | Holiday List (link from Company / Workstation) | ERPNext |
| Operation bulletin, process steps, SMV / standard minutes | Operation, Routing, BOM Operation (time in minutes) | ERPNext |
| Production order, line order | Work Order (+ Job Card per operation) | ERPNext |
| Material requirement explosion, release to store | Material Request (Material Transfer / Manufacture) from Work Order or Production Plan | ERPNext |
| Master production schedule, MRP | Production Plan | ERPNext |
| Gantt / timeline of records | Frappe Gantt view on any DocType with start/end dates | Frappe |
| Drag-and-drop planning board, capacity board | Custom Page (Build) — not standard | custom app |
| Barcodes on bundles, tickets, rolls | Barcode field type + Print Format; scan via Barcode field / scanner input | Frappe |
| Excel export of a schedule or report | Standard report export (Build only if a fixed template is required) | Frappe |
| Quality inspection, QC parameters | Quality Inspection, Quality Inspection Template | ERPNext |
| Chart of accounts, journal, payments | Account, Journal Entry, Payment Entry | ERPNext |
| Cost centre, budget, budget control | Cost Center, Budget, Accounting Dimension | ERPNext |
| Fixed assets, depreciation | Asset, Asset Category, Asset Depreciation Schedule | ERPNext |
| Projects, tasks, timesheets | Project, Task, Timesheet | ERPNext |
| Helpdesk, tickets, SLA | Issue, Service Level Agreement (or Helpdesk app) | ERPNext |
| Employee, attendance, leave, expense claims, payroll | Employee, Attendance, Leave Application, Expense Claim, Salary Slip | HRMS app (separate) |
| India GST, e-invoice, e-way bill | India Compliance app | separate app |

If a requirement depends on a separate app (HRMS, India Compliance, Helpdesk, CRM),
record it as an assumption: **the app must be installed on the bench** — check with
`bench --site <site> list-apps`.

## Mechanism → Fit

| Requirement pattern | Fit | Mechanism | Skill |
|---|---|---|---|
| Use an existing feature as-is | Standard | — | — |
| Turn on / set a value in a Settings DocType | Configure | Settings + fixture | `frappe-fixture-creator` |
| Naming like `PO-.YYYY.-.####` (documents often write `PO-YY-.####`; the date part needs dots: `PO-.YY.-.####`) | Configure | Naming Series / autoname | `frappe-doctype-builder` |
| Multi-level approval, states, transitions | Configure | Workflow (+ Workflow State/Action) | `frappe-workflow-generator` |
| Who can see / edit what | Configure | Role, DocPerm, User Permission | `frappe-permissions` |
| Email / system alert on an event | Configure | Notification | `frappe-core-notifications` |
| Printed document layout | Configure | Print Format (Jinja) | `frappe-print-format-builder` |
| Extra fields on a standard form | Extend | Custom Field (fixture) | `frappe-fixture-creator` |
| Change label / mandatory / hidden on standard field | Extend | Property Setter (fixture) | `frappe-fixture-creator` |
| Validation or calculation on a standard DocType | Extend | `doc_events` in hooks.py | `frappe-document-hooks` |
| Dynamic form behaviour | Extend | Client script in `public/js` + `doctype_js` hook | `frappe-client-script-generator` |
| Override standard controller logic | Extend | `extend_doctype_class` (v16) | `frappe-hooks` |
| Report over existing data | Extend | Script / Query Report | `frappe-report-generator` |
| Dashboard / KPI | Extend | Dashboard Chart, Number Card, Workspace | `frappe-dashboard-builder`, `frappe-workspace-builder` |
| Entity ERPNext has no concept of | Build | New DocType | `frappe-doctype-builder` |
| Line items of a new entity | Build | Child table DocType | `frappe-doctype-builder` |
| Scheduled job (reminders, escalations, sync) | Extend / Integrate | `scheduler_events` | `frappe-background-jobs` |
| External system calls ERPNext | Integrate | Whitelisted API | `frappe-api-handler`, `frappe-rest-api` |
| ERPNext calls external system | Integrate | Webhook / requests in background job | `frappe-webhook-manager`, `frappe-external-api-connector` |
| Load legacy masters / balances | Build (data) | Data Import / patch | `frappe-data-migration-generator`, `frappe-patch-generator` |

## Red flags — stop and raise an Open Question

- The requirement would need editing files inside `apps/frappe` or `apps/erpnext`.
- A new DocType duplicates a standard one ("Vendor Master" vs Supplier).
- An approval with no reject path or no approver role.
- A calculation that contradicts ERPNext accounting (e.g. posting GL without a voucher).
- A report needing data that nothing in the design captures.

## 02-fit-gap.md format

````markdown
# Fit-Gap — <Project Name>

| Fit | Count | % |
|---|---|---|
| Standard | 0 | |
| Configure | 0 | |
| Extend | 0 | |
| Build | 0 | |
| Integrate | 0 | |
| Out of Scope | 0 | |

## Modules (build order)

| # | Module | Requirements | Depends on |
|---|---|---|---|
| 1 | Masters & setup | FR-001..FR-010 | — |
| 2 | Procure to Pay | PS-P2P-*, FR-011..FR-030 | 1 |

## Requirement mapping

| Req ID | Fit | ERPNext Object | Mechanism | Module | Rationale |
|---|---|---|---|---|---|
| FR-001 | Extend | Supplier, Purchase Order | Custom Field `bank_verification_status` + PO `before_submit` check in doc_events | Masters & setup | Standard Supplier has no verification status |
````

Group requirements into **modules** that can be built and tested independently,
ordered by dependency (masters before transactions, transactions before reports).
The build phase works one module at a time in this order.
