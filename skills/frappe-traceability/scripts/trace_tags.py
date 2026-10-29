"""
Build docs/spec/traceability.md from REQ tags in the code, design.yaml and slice reviews.

Usage (from the bench folder, with the bench's Python for PyYAML):
    ./env/bin/python <skill-dir>/scripts/trace_tags.py --app my_app [--check]

Sources:
    01-requirements.md        every requirement ID and its text
    02-fit-gap.md             Fit, ERPNext Object and rationale per requirement
    design.yaml               which slice each requirement belongs to
    slices/*-review.md        whether that slice's review is fully approved
    code under apps/<app>     comments such as  # REQ: FR-012, FR-013  /  // REQ: PS-INW-02

Status per requirement:
    Out of Scope / Deferred   Fit is Out of Scope in the fit-gap (reason from its rationale)
    Verified                  has a test tag (or Fit Standard) and its slice review is all approved
    Built                     has a tag in non-test code (or Fit Standard/Configure with its slice generated)
    Designed                  assigned to a slice in design.yaml
    Pending                   none of the above

The matrix is regenerated, never edited by hand. --check also runs check_traceability.py.
"""

import argparse
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:  # design.yaml is optional for the matrix; slices are then unknown
    yaml = None

REQ_ID = re.compile(r"\b(?:BR|FR|NFR|PS|INT|RPT|PF|DM)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
TAG = re.compile(r"REQ:\s*([A-Z][A-Z0-9 ,.\-]*)")
CODE_EXT = (".py", ".js", ".html", ".vue", ".ts")
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "docs", "dist", "build"}


def tables(path):
    header = None
    for line in open(path, encoding="utf-8"):
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


def register(spec):
    rows = {}
    path = os.path.join(spec, "01-requirements.md")
    for header, cells in tables(path):
        if cells and REQ_ID.fullmatch(cells[0]):
            row = dict(zip(header, cells))
            rows[cells[0]] = {"text": row.get("requirement") or row.get("step") or row.get("report")
                              or row.get("print format") or row.get("data set") or "",
                              "source": row.get("source", "")}
    return rows


def fit_gap(spec):
    rows = {}
    path = os.path.join(spec, "02-fit-gap.md")
    if not os.path.exists(path):
        return rows
    for header, cells in tables(path):
        if cells and REQ_ID.fullmatch(cells[0]) and "fit" in header:
            row = dict(zip(header, cells))
            rows[cells[0]] = {"fit": row.get("fit", ""), "object": row.get("erpnext object", ""),
                              "rationale": row.get("rationale", "")}
    return rows


def slices(spec):
    path = os.path.join(spec, "design.yaml")
    if not yaml or not os.path.exists(path):
        return {}, {}
    design = yaml.safe_load(open(path, encoding="utf-8")) or {}
    owner, approved = {}, {}
    for s in design.get("slices") or []:
        for rid in s.get("requirements") or []:
            owner[rid] = s["id"]
        approved[s["id"]] = s.get("status") == "approved" or review_approved(spec, s)
    return owner, approved


def review_approved(spec, s):
    folder = os.path.join(spec, "slices")
    if not os.path.isdir(folder):
        return False
    files = [f for f in os.listdir(folder) if f.startswith(f"{s['id']}-") and f.endswith("-review.md")]
    if not files:
        return False
    text = open(os.path.join(folder, files[0]), encoding="utf-8").read()
    items = re.findall(r"^- \[( |x|X)\] \*\*[A-Z]\d+\*\*", text, re.M)
    comments = [c for c in re.findall(r"^[ \t]+- Comment:[ \t]*([^\n]*)$", text, re.M) if c.strip()]
    return bool(items) and all(i.lower() == "x" for i in items) and not comments


def scan(app_path):
    artifacts, tests = {}, {}
    for root, dirs, files in os.walk(app_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith(CODE_EXT):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, app_path)
            is_test = name.startswith("test_") or "/tests/" in "/" + rel.replace(os.sep, "/") + "/" and name != "__init__.py"
            try:
                text = open(full, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for m in TAG.finditer(text):
                for rid in REQ_ID.findall(m.group(1)):
                    (tests if is_test else artifacts).setdefault(rid, set()).add(rel)
    return artifacts, tests


def cell(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def main():
    ap = argparse.ArgumentParser(description="Generate the traceability matrix from REQ tags")
    ap.add_argument("--app", required=True)
    ap.add_argument("--bench", default=".")
    ap.add_argument("--check", action="store_true", help="run check_traceability.py on the result")
    args = ap.parse_args()
    app_path = os.path.join(args.bench, "apps", args.app)
    spec = os.path.join(app_path, "docs", "spec")
    if not os.path.exists(os.path.join(spec, "01-requirements.md")):
        sys.exit(f"ERROR: {spec}/01-requirements.md not found")

    reg, fg = register(spec), fit_gap(spec)
    owner, approved = slices(spec)
    artifacts, tests = scan(app_path)
    counts = {}
    lines = ["# Traceability Matrix", "",
             "Generated by `trace_tags.py` from REQ tags in the code, design.yaml and slice reviews - do not edit by hand.", "",
             "| Req ID | Requirement | Source | Fit | ERPNext Object | Artifacts | Tests | Status | Notes |",
             "|---|---|---|---|---|---|---|---|---|"]
    for rid in sorted(reg, key=lambda r: (r.split("-")[0], [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", r)])):
        fit = fg.get(rid, {}).get("fit") or "Build"
        obj = fg.get(rid, {}).get("object", "")
        arts, tsts = sorted(artifacts.get(rid, ())), sorted(tests.get(rid, ()))
        sid = owner.get(rid)
        notes = f"slice {sid}" if sid else ""
        if fit.lower() == "out of scope":
            status, notes = "Out of Scope", cell(fg.get(rid, {}).get("rationale")) or "out of scope per fit-gap"
        elif sid and approved.get(sid) and (tsts or fit.lower() in ("standard", "configure")):
            status = "Verified"
            if not tsts:
                notes += " · verified in developer review"
        elif arts or (sid and approved.get(sid)) or (fit.lower() == "standard" and sid):
            status = "Built"
        elif sid:
            status = "Designed"
        else:
            status = "Pending"
        counts[status] = counts.get(status, 0) + 1
        test_cell = ", ".join(f"`{t}`" for t in tsts) or (f"review {sid}" if status == "Verified" else "-")
        lines.append(f"| {rid} | {cell(reg[rid]['text'])[:160]} | {cell(reg[rid]['source'])} | {fit} | {cell(obj)} | "
                     f"{', '.join(f'`{a}`' for a in arts) or '-'} | {test_cell} | {status} | {notes} |")
    unknown = sorted((set(artifacts) | set(tests)) - set(reg))
    if unknown:
        lines += ["", "Tags in code with no matching requirement: " + ", ".join(unknown)]
    with open(os.path.join(spec, "traceability.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"traceability.md: {len(reg)} requirements - " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if unknown:
        print("WARN   tags with unknown IDs: " + ", ".join(unknown))
    if args.check:
        checker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_traceability.py")
        sys.exit(subprocess.run([sys.executable, checker, "--spec", spec, "--root", app_path]).returncode)


if __name__ == "__main__":
    main()
