# Requirements Register Template

Copy into `docs/spec/01-requirements.md`. Keep the table headers exactly as shown —
`frappe-traceability/scripts/check_traceability.py` reads the first column of every
table whose header ends in `ID`.

````markdown
# Requirements Register — <Project Name>

Sources: see `00-sources/INDEX.md`
ERPNext target: v16 · Custom app: `<app_name>` · Site: `<site_name>`
Last updated: <YYYY-MM-DD> · Phase: 1 (Requirements)

## Document Control

| Document | File | Version | Issue date | Author | Approver | Module | Client process IDs |
|---|---|---|---|---|---|---|---|
| BRD | `00-sources/<file>.md` | V 1.0 | <DD-MM-YYYY> | <role> | <role> | <module> | |
| To-Be Process | `00-sources/<file>.md` | V 1.2 | <DD-MM-YYYY> | <role> | <role> | <module> | 1.1.1, 1.2.1 |
| FRS | `00-sources/<file>.md` | V 1.0 | <DD-MM-YYYY> | <role> | <role> | <module> | 1.1.1, 1.2.1 |

The *Document* column is the role the file plays (one file may appear on several rows
if it combines roles). Referenced but not provided: <e.g. Quality FRS — see OQ-00n>

## Summary

| Type | Count |
|---|---|
| Business (BR) | 0 |
| Process steps (PS) | 0 |
| Functional (FR) | 0 |
| Reports (RPT) | 0 |
| Print formats (PF) | 0 |
| Integrations (INT) | 0 |
| Data migration (DM) | 0 |
| Non-functional (NFR) | 0 |
| Open questions (OQ) | 0 |

## Roles

| Role | Description | Source | ERPNext role (Phase 2) |
|---|---|---|---|
| Purchase User | Raises purchase requests | BRD §2.3 | Purchase User (standard) |

## Glossary

| Client term | Meaning | ERPNext term | Source |
|---|---|---|---|
| Sale Order | Confirmed buyer order | Sales Order | FRS §2.4 |
| Production Line | Line of machines and operators | Workstation | BRD §1.2 |

## Entities

One row per form / record / report the documents describe (from specification
sections, field tables and mockups). Phase 2 decides the ERPNext object for each.

| Entity | Kind | FRS section | BRD section | Figures | Requirement IDs | Notes |
|---|---|---|---|---|---|---|
| Approval Matrix | Master + child table | §2.1 | §1.1 | FRS fig 3 | FR-001..FR-012 | |
| Purchase Planning Settings | Settings (single) | §2.3 | §1.4 | FRS fig 5 | FR-020..FR-031 | |

**Kind**: Master, Transaction, Settings (single), Child table, Report, Dashboard,
Print format, Page/Tool.

## Processes

| Process ID | Name | Trigger | Outcome | Source |
|---|---|---|---|---|
| P2P | Procure to Pay | Material shortage | Supplier paid | ToBe §3, Fig 2 |

## Business Requirements

| Req ID | Requirement | Source | Source ID | Priority | Status |
|---|---|---|---|---|---|
| BR-001 | Reduce purchase approval time to under 2 working days | BRD §1.4 | OBJ-2 | Must | Confirmed |

## Process Steps

| Req ID | Process | Step | Actor | Input → Output | Rule / Decision | Source |
|---|---|---|---|---|---|---|
| PS-P2P-01 | P2P | Raise purchase request | Purchase User | Item, qty → Material Request | — | ToBe Fig 2 step 1 |
| PS-P2P-02 | P2P | Approve request | Department Head | Material Request → Approved MR | If amount > 50,000 INR also Finance Head | ToBe Fig 2 step 2 |

## Functional Requirements

| Req ID | Requirement | Acceptance Criteria | Source | Source ID | Priority | Status |
|---|---|---|---|---|---|---|
| FR-001 | The system shall record bank-account verification status on Supplier | 1) Status field (Pending/Verified/Rejected) on Supplier; 2) Purchase Order cannot be submitted for a supplier not Verified; 3) only Accounts Manager can set Verified | FRS §4.1 | FR-01 | Must | Confirmed |

## Reports

| Req ID | Report | Audience | Columns | Filters | Source | Status |
|---|---|---|---|---|---|---|

## Print Formats

| Req ID | Print format | For record | Content / layout | Figures | Source | Status |
|---|---|---|---|---|---|---|

## Integrations

| Req ID | System | Direction | Trigger / Frequency | Data | Error handling | Source | Status |
|---|---|---|---|---|---|---|---|

## Data Migration

| Req ID | Data set | From | Volume | Cut-over rule | Source | Status |
|---|---|---|---|---|---|---|

## Non-Functional Requirements

| Req ID | Requirement | Measure | Source | Status |
|---|---|---|---|---|

## Gaps, Conflicts & Open Questions

| OQ ID | Type | Related IDs | Question / Finding | Proposed default | Answer | Status |
|---|---|---|---|---|---|---|
| OQ-001 | Conflict | BR-004, FR-012 | BRD says approval above 50,000; FRS says 25,000 | Use FRS value 25,000 | | Open |
````

**Status values** for requirements: `Confirmed` (stated clearly in a document),
`Assumed` (our proposed default, awaiting confirmation), `Withdrawn`.
**OQ Type**: `Gap`, `Conflict`, `Ambiguity`, `Assumption`, `External dependency`
(a document or system referenced but not provided). **OQ Status**: `Open`,
`Answered`, `Accepted default`.
