"""
Check that every requirement is traced to design, code and tests.

Usage:
    python3 -I check_traceability.py --spec docs/spec --root apps/<app> [--strict]

Reads:
    <spec>/01-requirements.md   requirements register (source of all IDs)
    <spec>/traceability.md      traceability matrix

Rules:
- every ID in the register appears in the matrix (and vice versa)
- Fit and Status use the allowed vocabulary
- Built / Verified rows list at least one artifact (except Standard / Configure fit, delivered
  by ERPNext itself or by generated fixtures),
  and every `backticked` path exists under --root
- Verified rows list at least one test; backticked test paths must exist
- Deferred / Out of Scope rows carry a reason in the Notes column

Exit code 1 when --strict and any error is found.
"""

import argparse
import os
import re
import sys

ID_RE = re.compile(r"\b(?:BR|FR|NFR|PS|INT|RPT|PF|DM)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
FITS = {"standard", "configure", "extend", "build", "integrate", "out of scope"}
STATUSES = {"pending", "designed", "built", "verified", "deferred", "out of scope"}
MATRIX_COLS = ["req id", "requirement", "source", "fit", "erpnext object", "artifacts", "tests", "status", "notes"]


def table_rows(path):
    """Yield (header, cells) for every Markdown table row in a file."""
    header = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|"):
                header = None
                continue
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if header is None:
                header = [c.lower() for c in cells]
                continue
            yield header, cells


def register_ids(path):
    ids = {}
    for header, cells in table_rows(path):
        if not header or not header[0].endswith("id"):
            continue
        m = ID_RE.fullmatch(cells[0])
        if m:
            ids[cells[0]] = cells[1] if len(cells) > 1 else ""
    return ids


def matrix_rows(path):
    rows = {}
    for header, cells in table_rows(path):
        if header[: len(MATRIX_COLS)] != MATRIX_COLS:
            continue
        cells += [""] * (len(header) - len(cells))
        row = dict(zip(header, cells))
        if ID_RE.fullmatch(row["req id"]):
            rows[row["req id"]] = row
    return rows


def paths_in(cell):
    return [p for p in re.findall(r"`([^`]+)`", cell) if "/" in p or "." in p]


def main():
    ap = argparse.ArgumentParser(description="Validate the requirements traceability matrix")
    ap.add_argument("--spec", default="docs/spec", help="Spec folder holding 01-requirements.md and traceability.md")
    ap.add_argument("--root", default=".", help="Folder artifact/test paths are relative to (usually apps/<app>)")
    ap.add_argument("--strict", action="store_true", help="Exit 1 if any error is found")
    args = ap.parse_args()

    reg_path = os.path.join(args.spec, "01-requirements.md")
    mat_path = os.path.join(args.spec, "traceability.md")
    for p in (reg_path, mat_path):
        if not os.path.exists(p):
            print(f"ERROR: {p} not found")
            sys.exit(1)

    register = register_ids(reg_path)
    matrix = matrix_rows(mat_path)
    errors, warnings = [], []

    if not matrix:
        errors.append(f"no matrix rows found - header must start with: | {' | '.join(c.title() for c in MATRIX_COLS)} |")

    for rid in sorted(set(register) - set(matrix)):
        errors.append(f"{rid}: in requirements register but missing from traceability matrix")
    for rid in sorted(set(matrix) - set(register)):
        warnings.append(f"{rid}: in matrix but not in requirements register")

    counts = {}
    for rid, row in sorted(matrix.items()):
        fit, status = row["fit"].lower(), row["status"].lower()
        counts[status] = counts.get(status, 0) + 1
        if fit not in FITS:
            errors.append(f"{rid}: Fit '{row['fit']}' not one of {sorted(FITS)}")
        if status not in STATUSES:
            errors.append(f"{rid}: Status '{row['status']}' not one of {sorted(STATUSES)}")
        if status in ("deferred", "out of scope") and not row["notes"]:
            errors.append(f"{rid}: {row['status']} needs a reason in Notes")
        if status in ("built", "verified"):
            if fit not in ("standard", "configure") and (not row["artifacts"] or row["artifacts"] == "-"):
                errors.append(f"{rid}: {row['status']} but no artifacts listed")
            for p in paths_in(row["artifacts"]):
                if not os.path.exists(os.path.join(args.root, p)):
                    errors.append(f"{rid}: artifact not found: {p}")
        if status == "verified":
            if not row["tests"] or row["tests"] == "-":
                errors.append(f"{rid}: Verified but no tests listed")
            for p in paths_in(row["tests"]):
                if not os.path.exists(os.path.join(args.root, p.split("::")[0])):
                    errors.append(f"{rid}: test not found: {p}")
        if status == "built" and (not row["tests"] or row["tests"] == "-"):
            warnings.append(f"{rid}: Built but no test yet")

    total = len(register)
    status_of = {rid: matrix[rid]["status"].lower() for rid in register if rid in matrix}
    done = sum(1 for s in status_of.values() if s == "verified")
    scoped = total - sum(1 for s in status_of.values() if s in ("deferred", "out of scope"))
    print(f"Requirements: {total} | in matrix: {len(matrix)} | " +
          ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if scoped:
        print(f"Verified coverage of in-scope requirements: {done}/{scoped} ({100 * done // scoped}%)")
    for e in errors:
        print("ERROR  " + e)
    for w in warnings:
        print("WARN   " + w)
    if not errors:
        print("OK - traceability matrix is consistent")
    sys.exit(1 if errors and args.strict else 0)


if __name__ == "__main__":
    main()
