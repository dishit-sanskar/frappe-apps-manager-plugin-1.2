"""
PostToolUse hook: check a file Claude just wrote inside a Frappe app.

Reads the hook JSON on stdin, checks tool_input.file_path, and exits 2 with the problems on
stderr (which Claude sees and fixes) or 0 silently. Only files under an `apps/` folder are
checked, so it stays quiet everywhere else.

Checks:
- .py   syntax (compile)
- .js   syntax (node --check, when Node is installed); frm.set_query("x", "<field>") where
        <field> is a Table MultiSelect of the DocType next to the script (the query must be
        set on the multiselect field itself: frm.set_query("<field>", ...))
- .json valid JSON; DocType JSON has name, module and fieldname/fieldtype on every field
- requirement IDs (FR-012, PS-INW-02 ...) in user-facing text: DocType labels/descriptions,
  _("...") / __("...") strings
"""

import json
import os
import re
import shutil
import subprocess
import sys

REQ_ID = re.compile(r"\b(?:BR|FR|NFR|PS|INT|RPT|PF|DM)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
TRANSLATED = re.compile(r"""(?<![\w.])__?\(\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.S)


def doctype_json_beside(path):
    folder = os.path.dirname(path)
    candidate = os.path.join(folder, os.path.basename(folder) + ".json")
    if os.path.exists(candidate):
        try:
            return json.load(open(candidate, encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def check_py(path, text, problems):
    try:
        compile(text, path, "exec")
    except SyntaxError as e:
        problems.append(f"Python syntax error line {e.lineno}: {e.msg}")
    for m in TRANSLATED.finditer(text):
        if REQ_ID.search(m.group(2)):
            problems.append(f"requirement ID in user-facing text: _({m.group(1)}{m.group(2)[:80]}{m.group(1)}) - "
                            "keep IDs in comments (# REQ: ...)")


def check_js(path, text, problems):
    if shutil.which("node"):
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            err = [l for l in r.stderr.splitlines() if l.strip()]
            problems.append("JavaScript syntax error: " + " | ".join(err[:4]))
    meta = doctype_json_beside(path)
    if meta:
        multiselect = {f["fieldname"] for f in meta.get("fields", []) if f.get("fieldtype") == "Table MultiSelect"}
        for m in re.finditer(r"set_query\(\s*[\"'](\w+)[\"']\s*,\s*[\"'](\w+)[\"']", text):
            if m.group(2) in multiselect:
                problems.append(f'frm.set_query("{m.group(1)}", "{m.group(2)}", ...): {m.group(2)} is a Table MultiSelect, '
                                f'which has no grid - this throws when the form loads. Use frm.set_query("{m.group(2)}", () => ...)')
    for m in TRANSLATED.finditer(text):
        if REQ_ID.search(m.group(2)):
            problems.append(f"requirement ID in user-facing text: __({m.group(1)}{m.group(2)[:80]}{m.group(1)}) - "
                            "keep IDs in comments (// REQ: ...)")


def check_json(path, text, problems):
    try:
        data = json.loads(text)
    except ValueError as e:
        problems.append(f"invalid JSON: {e}")
        return
    if isinstance(data, dict) and data.get("doctype") == "DocType":
        for key in ("name", "module"):
            if not data.get(key):
                problems.append(f"DocType JSON missing {key}")
        for i, f in enumerate(data.get("fields", [])):
            if not f.get("fieldname") or not f.get("fieldtype"):
                problems.append(f"DocType field #{i + 1} needs fieldname and fieldtype")
            for k in ("label", "description"):
                if REQ_ID.search(str(f.get(k) or "")):
                    problems.append(f"requirement ID in user-facing {k} of field {f.get('fieldname')}: {f.get(k)!r}")
        if REQ_ID.search(str(data.get("description") or "")):
            problems.append("requirement ID in the DocType description (shown to users)")
    records = data if isinstance(data, list) else []
    for rec in records:
        if isinstance(rec, dict) and rec.get("doctype") == "Custom Field":
            for k in ("label", "description"):
                if REQ_ID.search(str(rec.get(k) or "")):
                    problems.append(f"requirement ID in user-facing {k} of custom field {rec.get('name')}")


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path or "/apps/" not in path.replace(os.sep, "/") or not os.path.isfile(path):
        return 0
    if "/node_modules/" in path or "/docs/" in path.replace(os.sep, "/"):
        return 0
    try:
        text = open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return 0
    problems = []
    if path.endswith(".py"):
        check_py(path, text, problems)
    elif path.endswith(".js"):
        check_js(path, text, problems)
    elif path.endswith(".json"):
        check_json(path, text, problems)
    if problems:
        print(f"Frappe lint - {path}:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
