"""
Feature-slice tooling for Frappe apps driven by docs/spec/design.yaml.

Run with the bench's Python (it has PyYAML), from the bench folder:

    ./env/bin/python <skill-dir>/scripts/slice_tool.py <command> --app geofresh [options]

Commands:
    validate                 check design.yaml (structure, links, IDs, user-facing text)
    generate  --slice S02    write DocTypes, fixtures and stubs for one slice
    brief     --slice S02    write docs/spec/slices/<slice>.md, the agents' single input
    routes    --slice S02    print the desk URLs to open in the browser check
    review    --slice S02    write / refresh the developer review checklist
    review-status --slice S02 | --gate flows|plan [--json]
                             summarise a checklist; exit 0 only when everything is approved
    flows                    write docs/spec/review/flows-review.md from flows.yaml (gate 1:
                             each business flow explained and approved)
    plan                     write docs/spec/review/plan-review.md from design.yaml (gate 2:
                             per slice, what standard ERPNext covers and what is developed)
    progress                 write docs/spec/PROGRESS.md (done / being fixed / to develop)
    tasks     --slice S02 [--mode on-issue|every-task|per-group|slice]
                             split a slice into small agent tasks (docs/spec/slices/<slice>-tasks.yaml)
    next      --slice S02 [--owner developer|ui-ux] [--json]
                             start the next ready task: git checkpoint, guard state, task prompt
    checkpoint --slice S02 --task T03 [--accept] [--json]
                             verify a finished task (files, tests, REQ tags, stubs) and say continue / pause
    lessons   --slice S02    log rejected review items to the company knowledge base
    archive   --slice S02    store an approved slice in the company pattern library
    metrics   [--slice S02]  time, tool calls and test runs per agent run

Paths: the app folder is apps/<app>; the spec folder is apps/<app>/docs/spec.

Generated files fall in two groups:
- managed (DocType JSON): rewritten from design.yaml on every generate, but never over a
  file someone edited by hand since the last generate (hash check; --force overrides)
- create-once (controllers, form scripts, tests, report and print format stubs): written
  only when missing, then owned by the agents
Fixtures (custom fields, property setters, roles, workflows) are merged record by record.
"""

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import yaml

REQ_ID = re.compile(r"\b(?:BR|FR|NFR|PS|INT|RPT|PF|DM)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
FIELDTYPES = {
    "Attach", "Attach Image", "Autocomplete", "Barcode", "Button", "Check", "Code", "Color",
    "Column Break", "Currency", "Data", "Date", "Datetime", "Duration", "Dynamic Link", "Float",
    "Fold", "Geolocation", "Heading", "HTML", "HTML Editor", "Icon", "Image", "Int", "JSON",
    "Link", "Long Text", "Markdown Editor", "Password", "Percent", "Phone", "Rating",
    "Read Only", "Section Break", "Select", "Signature", "Small Text", "Tab Break", "Table",
    "Table MultiSelect", "Text", "Text Editor", "Time",
}
NO_VALUE = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Fold"}
PERM_LETTERS = {
    "R": "read", "W": "write", "C": "create", "S": "submit", "X": "cancel", "A": "amend",
    "D": "delete", "P": "print", "E": "export", "M": "email", "H": "share", "T": "report",
    "L": "select",
}
NAMING_RULE = {
    "naming_series:": "By Naming Series", "hash": "Random", "autoincrement": "Autoincrement",
    "prompt": "Set by user",
}
# keys of a field entry that are not DocField properties
FIELD_META = {"type", "options", "section", "column", "tab", "req"}
SELF_STAMP = "Administrator"


# ---------------------------------------------------------------- helpers

def scrub(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def slug(text):
    return scrub(text).replace("_", "-")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def dump_json(obj):
    return json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


class Ctx:
    def __init__(self, args):
        self.app = args.app
        self.bench = os.path.abspath(args.bench)
        self.app_path = os.path.join(self.bench, "apps", args.app)
        self.pkg = os.path.join(self.app_path, args.app)
        self.spec = os.path.join(self.app_path, "docs", "spec")
        self.design_path = args.design or os.path.join(self.spec, "design.yaml")
        if not os.path.isdir(self.pkg):
            sys.exit(f"ERROR: app package not found: {self.pkg} (run from the bench folder or pass --bench)")
        self.design = {}
        if os.path.exists(self.design_path):
            with open(self.design_path, encoding="utf-8") as f:
                self.design = yaml.safe_load(f) or {}
        self.manifest_path = os.path.join(self.spec, "generated.json")
        self.manifest = {"files": {}, "records": {}}
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path, encoding="utf-8") as f:
                self.manifest.update(json.load(f))
        self.written, self.skipped, self.warnings = [], [], []

    def require_design(self):
        if not os.path.exists(self.design_path):
            sys.exit(f"ERROR: design file not found: {self.design_path}")

    def slices(self):
        return self.design.get("slices") or []

    def slice(self, slice_id):
        for s in self.slices():
            if s.get("id") == slice_id:
                return s
        sys.exit(f"ERROR: slice {slice_id} not in design.yaml (have: {', '.join(s.get('id', '?') for s in self.slices())})")

    def all_doctypes(self):
        return {d["name"]: d for s in self.slices() for d in s.get("doctypes") or []}

    def rel(self, path):
        return os.path.relpath(path, self.app_path)

    def save_manifest(self):
        os.makedirs(self.spec, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            f.write(dump_json(self.manifest))


# ---------------------------------------------------------------- field conversion

def expand_fields(entries, owner):
    """design.yaml field list -> DocField dicts (section/column/tab shorthands expanded)."""
    out, counters = [], {}

    def auto(kind, label=None):
        base = f"{scrub(label)}_{kind}" if label else f"{kind}_{counters.setdefault(kind, 0) + 1}"
        counters[kind] = counters.get(kind, 0) + 1
        name = base
        while any(f["fieldname"] == name for f in out):
            name = f"{base}_{counters[kind]}"
            counters[kind] += 1
        return name

    for entry in entries or []:
        if entry == "column" or (isinstance(entry, dict) and "column" in entry and "fieldname" not in entry):
            label = entry.get("column") if isinstance(entry, dict) and isinstance(entry.get("column"), str) else None
            out.append({"fieldname": auto("column_break"), "fieldtype": "Column Break", **({"label": label} if label else {})})
            continue
        if isinstance(entry, dict) and "section" in entry and "fieldname" not in entry:
            f = {"fieldname": auto("section", entry["section"]), "fieldtype": "Section Break"}
            if entry["section"]:
                f["label"] = entry["section"]
            f.update({k: v for k, v in entry.items() if k != "section"})
            out.append(f)
            continue
        if isinstance(entry, dict) and "tab" in entry and "fieldname" not in entry:
            out.append({"fieldname": auto("tab", entry["tab"]), "fieldtype": "Tab Break", "label": entry["tab"],
                        **{k: v for k, v in entry.items() if k != "tab"}})
            continue
        if not isinstance(entry, dict) or "fieldname" not in entry:
            raise ValueError(f"{owner}: field entry needs a fieldname: {entry!r}")
        f = {"fieldname": entry["fieldname"], "fieldtype": entry.get("type", "Data")}
        if "label" not in entry and f["fieldtype"] not in NO_VALUE:
            f["label"] = entry["fieldname"].replace("_", " ").title()
        options = entry.get("options")
        if isinstance(options, list):
            joined = "\n".join(str(o) for o in options)
            options = joined if (f["fieldtype"] != "Select" or entry.get("no_blank")) else "\n" + joined
        if options is not None:
            f["options"] = options
        for k, v in entry.items():
            if k in FIELD_META or k in ("fieldname", "no_blank"):
                continue
            f[k] = (1 if v else 0) if isinstance(v, bool) else v
        out.append(f)
    return out


def perms(spec, submittable):
    rows = []
    for role, letters in (spec or {}).items():
        row = {"role": role}
        for ch in str(letters).upper():
            if ch not in PERM_LETTERS:
                raise ValueError(f"unknown permission letter {ch!r} for role {role}")
            row[PERM_LETTERS[ch]] = 1
        if not submittable:
            for k in ("submit", "cancel", "amend"):
                row.pop(k, None)
        rows.append(row)
    return rows


def doctype_json(dt, existing=None):
    istable, issingle = bool(dt.get("istable")), bool(dt.get("issingle"))
    submittable = bool(dt.get("submittable"))
    fields = expand_fields(dt.get("fields"), dt["name"])
    names = {f["fieldname"] for f in fields}
    autoname = dt.get("autoname")
    if autoname == "naming_series:" and "naming_series" not in names:
        series = dt.get("naming_series") or []
        fields.insert(0, {"fieldname": "naming_series", "fieldtype": "Select", "label": "Series",
                          "options": "\n".join(series), "reqd": 1, "no_copy": 1, "print_hide": 1,
                          **({"default": series[0]} if series else {})})
    if submittable and "amended_from" not in names:
        fields.append({"fieldname": "amended_from", "fieldtype": "Link", "label": "Amended From",
                       "options": dt["name"], "read_only": 1, "no_copy": 1, "print_hide": 1, "search_index": 1})
    permissions = [] if istable else perms(
        ({"System Manager": "RWCDSXAPEMHT"} if dt.get("system_manager", True) else {}) | (dt.get("permissions") or {}),
        submittable)
    doc = {
        "actions": [], "allow_rename": 1 if dt.get("allow_rename") else 0,
        "creation": (existing or {}).get("creation") or now(),
        "doctype": "DocType", "engine": "InnoDB",
        "field_order": [f["fieldname"] for f in fields],
        "fields": fields, "index_web_pages_for_search": 0, "links": [],
        "modified": now(), "modified_by": SELF_STAMP, "module": dt["module"], "name": dt["name"],
        "owner": SELF_STAMP, "permissions": permissions, "row_format": "Dynamic",
        "sort_field": dt.get("sort_field", "creation"), "sort_order": dt.get("sort_order", "DESC"),
        "states": [],
    }
    if istable:
        doc.update({"istable": 1, "editable_grid": 0 if dt.get("editable_grid") is False else 1})
    else:
        doc["track_changes"] = 0 if dt.get("track_changes") is False else 1
    if issingle:
        doc["issingle"] = 1
    if submittable:
        doc["is_submittable"] = 1
    if autoname:
        doc["autoname"] = autoname
        doc["naming_rule"] = NAMING_RULE.get(autoname.lower() if autoname != "naming_series:" else autoname) or (
            "By fieldname" if autoname.startswith("field:") else "Expression" if autoname.startswith("format:") else "Expression (old style)")
    for key in ("title_field", "description", "quick_entry", "image_field", "is_tree", "nsm_parent_field",
                "allow_import", "show_title_field_in_link", "default_view", "document_type", "max_attachments"):
        if key in dt:
            doc[key] = (1 if dt[key] else 0) if isinstance(dt[key], bool) else dt[key]
    if dt.get("search_fields"):
        doc["search_fields"] = ",".join(dt["search_fields"]) if isinstance(dt["search_fields"], list) else dt["search_fields"]
    return doc


# ---------------------------------------------------------------- validation

def validate(ctx, register_ids=None):
    errors, warnings = [], []
    design = ctx.design
    if design.get("app") and design["app"] != ctx.app:
        errors.append(f"design.yaml app is {design['app']!r}, not {ctx.app!r}")
    ids = [s.get("id") for s in ctx.slices()]
    if len(ids) != len(set(ids)):
        errors.append("duplicate slice ids")
    roles = {r["name"] if isinstance(r, dict) else r for r in design.get("roles") or []}
    doctypes = ctx.all_doctypes()
    seen_dt = set()
    for s in ctx.slices():
        sid = s.get("id", "?")
        for key in ("id", "title"):
            if not s.get(key):
                errors.append(f"slice {sid}: missing {key}")
        for dep in s.get("depends_on") or []:
            if dep not in ids:
                errors.append(f"slice {sid}: depends_on unknown slice {dep}")
            elif ids.index(dep) > ids.index(sid):
                errors.append(f"slice {sid}: depends on {dep}, which comes later in the order")
        for rid in s.get("requirements") or []:
            if not REQ_ID.fullmatch(rid):
                errors.append(f"slice {sid}: {rid!r} is not a requirement ID")
            elif register_ids is not None and rid not in register_ids:
                errors.append(f"slice {sid}: {rid} not in 01-requirements.md")
        for dt in s.get("doctypes") or []:
            name = dt.get("name")
            if name in seen_dt:
                errors.append(f"DocType {name} defined twice")
            seen_dt.add(name)
            if not dt.get("module"):
                errors.append(f"{name}: module missing")
            try:
                fields = expand_fields(dt.get("fields"), name)
            except ValueError as e:
                errors.append(str(e))
                continue
            fnames = [f["fieldname"] for f in fields]
            for dup in {n for n in fnames if fnames.count(n) > 1}:
                errors.append(f"{name}: duplicate fieldname {dup}")
            for f in fields:
                where = f"{name}.{f['fieldname']}"
                if f["fieldtype"] not in FIELDTYPES:
                    errors.append(f"{where}: unknown fieldtype {f['fieldtype']!r}")
                if not re.fullmatch(r"[a-z][a-z0-9_]*", f["fieldname"]):
                    errors.append(f"{where}: fieldname must be lower_snake_case")
                if f["fieldtype"] in ("Link", "Table", "Table MultiSelect", "Select", "Dynamic Link") and not f.get("options"):
                    errors.append(f"{where}: {f['fieldtype']} needs options")
                if f["fieldtype"] in ("Table", "Table MultiSelect"):
                    child = doctypes.get(f.get("options"))
                    if not child:
                        warnings.append(f"{where}: child table {f.get('options')!r} not defined in design.yaml")
                    elif not child.get("istable"):
                        errors.append(f"{where}: {f['options']} must be istable: true")
                    elif f["fieldtype"] == "Table MultiSelect":
                        links = [c for c in expand_fields(child.get("fields"), child["name"]) if c["fieldtype"] == "Link"]
                        if len(links) != 1:
                            errors.append(f"{where}: Table MultiSelect child {f['options']} needs exactly one Link field")
                for text_key in ("label", "description"):
                    if REQ_ID.search(str(f.get(text_key) or "")):
                        errors.append(f"{where}: requirement ID in user-facing {text_key} - keep IDs in code comments")
            for role in (dt.get("permissions") or {}):
                if roles and role not in roles:
                    warnings.append(f"{name}: role {role!r} not in design roles (fine if it is a standard role)")
            try:
                perms(dt.get("permissions"), dt.get("submittable"))
            except ValueError as e:
                errors.append(f"{name}: {e}")
            for rule in dt.get("rules") or []:
                errors += rule_errors(rule, name, s.get("requirements") or [])
            if dt.get("autoname") == "naming_series:" and not dt.get("naming_series"):
                warnings.append(f"{name}: autoname naming_series: without naming_series list")
            for series in dt.get("naming_series") or []:
                if re.search(r"(?<!\.)\b(YYYY|YY|MM|DD)\b(?!\.)", series):
                    errors.append(f"{name}: naming series {series!r}: date parts need dots, e.g. .YY.")
        for group in s.get("custom_fields") or []:
            if not group.get("doctype"):
                errors.append(f"slice {sid}: custom_fields group without doctype")
            if not group.get("insert_after") and not any(isinstance(f, dict) and f.get("insert_after") for f in group.get("fields") or []):
                warnings.append(f"custom fields on {group.get('doctype')}: no insert_after - they go to the end of the form")
            for rule in group.get("rules") or []:
                errors += rule_errors(rule, group.get("doctype"), s.get("requirements") or [])
            for f in group.get("fields") or []:
                if isinstance(f, dict) and REQ_ID.search(str(f.get("label", "")) + str(f.get("description", ""))):
                    errors.append(f"{group.get('doctype')}.{f.get('fieldname')}: requirement ID in user-facing text")
        for wf in s.get("workflows") or []:
            states = {st.get("state") for st in wf.get("states") or []}
            for t in wf.get("transitions") or []:
                for end in ("from", "to"):
                    if t.get(end) not in states:
                        errors.append(f"workflow {wf.get('name')}: transition {end} state {t.get(end)!r} not in states")
    return errors, warnings


RULE_STOPWORDS = {"a", "an", "the", "be", "is", "are", "to", "of", "in", "on", "for", "must", "should", "can", "only"}
RULE_EVENTS = ("validate", "before_save", "before_submit", "on_submit", "on_cancel", "after_insert", "on_update")


def rule_event(rule):
    return rule.get("event") or "validate"


def rule_errors(rule, where, slice_reqs):
    errors = []
    if not isinstance(rule, dict) or not rule.get("id") or not rule.get("rule"):
        return [f"{where}: each rule needs id and rule, got {rule!r}"]
    if True in rule or "on" in rule:
        errors.append(f"{where}: rule {rule['id']}: write `event:` instead of `on:` (YAML reads a bare on as true)")
    if rule["id"] not in slice_reqs:
        errors.append(f"{where}: rule {rule['id']} is not in the slice's requirements")
    if rule_event(rule) not in RULE_EVENTS:
        errors.append(f"{where}: rule {rule['id']}: event must be one of {', '.join(RULE_EVENTS)}")
    if rule.get("method") and not re.fullmatch(r"[a-z][a-z0-9_]*", rule["method"]):
        errors.append(f"{where}: rule {rule['id']}: method must be lower_snake_case")
    return errors


def rule_method(rule):
    if rule.get("method"):
        return rule["method"]
    words = [w for w in scrub(rule["rule"]).split("_") if w and w not in RULE_STOPWORDS]
    return "validate_" + "_".join(words[:4])


# ---------------------------------------------------------------- writing

def write_managed(ctx, path, content, force):
    rel = ctx.rel(path)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            current = f.read()
        known = ctx.manifest["files"].get(rel)
        if known != sha(current) and not force:
            ctx.skipped.append(f"{rel} (edited by hand since last generate - change design.yaml or use --force)")
            return
        if strip_volatile(current) == strip_volatile(content):
            return  # same schema: keep file (and its modified stamp) untouched
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    ctx.manifest["files"][rel] = sha(content)
    ctx.written.append(rel)


def strip_volatile(text):
    try:
        d = json.loads(text)
    except ValueError:
        return text
    d.pop("modified", None)
    d.pop("creation", None)
    return json.dumps(d, sort_keys=True)


def write_once(ctx, path, content):
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    ctx.written.append(ctx.rel(path))


def ensure_module(ctx, module):
    mod = os.path.join(ctx.pkg, scrub(module))
    write_once(ctx, os.path.join(mod, "__init__.py"), "")
    modules_txt = os.path.join(ctx.pkg, "modules.txt")
    lines = open(modules_txt, encoding="utf-8").read().splitlines() if os.path.exists(modules_txt) else []
    if module not in [l.strip() for l in lines]:
        with open(modules_txt, "a", encoding="utf-8") as f:
            f.write(("" if not lines or open(modules_txt).read().endswith("\n") else "\n") + module + "\n")
        ctx.written.append(ctx.rel(modules_txt))
    return mod


CONTROLLER = '''# Copyright (c) {year}, {publisher} and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class {cls}(Document):
	pass
'''
FORM_JS = '''// Copyright (c) {year}, {publisher} and contributors
// For license information, please see license.txt

frappe.ui.form.on("{name}", {{
	refresh(frm) {{}},
}});
'''
TEST_PY = '''# Copyright (c) {year}, {publisher} and contributors
# See license.txt

from frappe.tests import IntegrationTestCase


class Test{cls}(IntegrationTestCase):
	pass
'''
RULE_MARK = "TODO(rule"  # left in stubs until the rule is implemented; checkpoint refuses a task that still has one


def controller_stub(fmt, rules):
    if not rules:
        return CONTROLLER.format(**fmt)
    head = CONTROLLER.format(**fmt).rsplit("\tpass\n", 1)[0]
    body = []
    for event in RULE_EVENTS:
        here = [r for r in rules if rule_event(r) == event]
        if here:
            body += [f"\tdef {event}(self):"] + [f"\t\tself.{rule_method(r)}()" for r in here] + [""]
    for r in rules:
        body += [f"\tdef {rule_method(r)}(self):", f"\t\t# {RULE_MARK} {r['id']}): {r['rule']}", "\t\tpass", ""]
    return head + "\n".join(body).rstrip() + "\n"


def test_stub(fmt, rules):
    if not rules:
        return TEST_PY.format(**fmt)
    head = TEST_PY.format(**fmt).rsplit("\tpass\n", 1)[0]
    body = []
    for r in rules:
        body += [f"\tdef test_{rule_method(r)}(self):", f"\t\t# {RULE_MARK} {r['id']}): {r['rule']}",
                 "\t\tself.skipTest(\"not written yet\")", ""]
    return head + "\n".join(body).rstrip() + "\n"


REPORT_PY = '''# Copyright (c) {year}, {publisher} and contributors
# For license information, please see license.txt

from frappe import _


def execute(filters=None):
	return get_columns(), get_data(filters or {{}})


def get_columns():
	return []


def get_data(filters):
	return []
'''
REPORT_JS = '''// Copyright (c) {year}, {publisher} and contributors
// For license information, please see license.txt

frappe.query_reports["{name}"] = {{
	filters: [],
}};
'''


def class_name(name):
    return re.sub(r"[^A-Za-z0-9]", "", name.title())


def publisher(ctx):
    hooks = os.path.join(ctx.pkg, "hooks.py")
    m = re.search(r'app_publisher\s*=\s*"([^"]*)"', open(hooks).read()) if os.path.exists(hooks) else None
    return m.group(1) if m else ctx.app


def merge_fixture(ctx, fixture, records, key="name"):
    path = os.path.join(ctx.pkg, "fixtures", f"{fixture}.json")
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
    index = {r.get(key): i for i, r in enumerate(data)}
    changed = False
    for rec in records:
        i = index.get(rec[key])
        if i is None:
            data.append(rec)
            changed = True
        else:
            merged = {**data[i], **rec}
            if strip_volatile(json.dumps(merged)) != strip_volatile(json.dumps(data[i])):
                data[i] = merged
                changed = True
        ctx.manifest["records"].setdefault(fixture, [])
        if rec[key] not in ctx.manifest["records"][fixture]:
            ctx.manifest["records"][fixture].append(rec[key])
    if changed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(dump_json(data))
        ctx.written.append(ctx.rel(path))


def generate(ctx, slice_id, force=False):
    s = ctx.slice(slice_id)
    if s.get("generate") is False:
        sys.exit(f"ERROR: slice {slice_id} has generate: false (hand-built before design.yaml) - nothing to generate")
    year, pub = datetime.date.today().year, publisher(ctx)
    modules = set()

    for dt in s.get("doctypes") or []:
        mod = ensure_module(ctx, dt["module"])
        modules.add(dt["module"])
        write_once(ctx, os.path.join(mod, "doctype", "__init__.py"), "")
        folder = os.path.join(mod, "doctype", scrub(dt["name"]))
        write_once(ctx, os.path.join(folder, "__init__.py"), "")
        json_path = os.path.join(folder, scrub(dt["name"]) + ".json")
        existing = json.load(open(json_path, encoding="utf-8")) if os.path.exists(json_path) else None
        write_managed(ctx, json_path, dump_json(doctype_json(dt, existing)), force)
        fmt = {"year": year, "publisher": pub, "cls": class_name(dt["name"]), "name": dt["name"]}
        write_once(ctx, os.path.join(folder, scrub(dt["name"]) + ".py"), controller_stub(fmt, dt.get("rules")))
        if not dt.get("istable"):
            write_once(ctx, os.path.join(folder, scrub(dt["name"]) + ".js"), FORM_JS.format(**fmt))
            write_once(ctx, os.path.join(folder, "test_" + scrub(dt["name"]) + ".py"), test_stub(fmt, dt.get("rules")))

    custom_fields, prop_setters = [], []
    for group in s.get("custom_fields") or []:
        module = group.get("module") or s.get("module") or next(iter(modules), None)
        fields = expand_fields(group.get("fields"), group["doctype"])
        previous = group.get("insert_after")
        for f in fields:
            if not f["fieldname"].startswith("custom_"):
                f["fieldname"] = "custom_" + f["fieldname"]
            f.setdefault("insert_after", previous)
            previous = f["fieldname"]
            custom_fields.append({"doctype": "Custom Field", "dt": group["doctype"],
                                  "name": f"{group['doctype']}-{f['fieldname']}", "module": module,
                                  "is_system_generated": 0, "modified": now(), **f})
    for p in s.get("property_setters") or []:
        prop_setters.append({
            "doctype": "Property Setter", "doc_type": p["doctype"], "doctype_or_field": "DocField" if p.get("field") else "DocType",
            "field_name": p.get("field"), "property": p["property"], "property_type": p.get("type", "Data"),
            "value": str(p["value"]), "module": p.get("module") or next(iter(modules), None),
            "name": f"{p['doctype']}-{p.get('field') or 'main'}-{p['property']}", "is_system_generated": 0,
            "modified": now(),
        })
    if custom_fields:
        merge_fixture(ctx, "custom_field", custom_fields)
    if prop_setters:
        merge_fixture(ctx, "property_setter", prop_setters)

    slice_roles = {r for dt in s.get("doctypes") or [] for r in (dt.get("permissions") or {})}
    slice_roles |= {r for wf in s.get("workflows") or [] for t in wf.get("transitions") or [] for r in [t.get("allowed")] if r}
    roles = [r if isinstance(r, dict) else {"name": r} for r in ctx.design.get("roles") or []]
    roles = [r for r in roles if r["name"] in slice_roles]
    if roles:
        merge_fixture(ctx, "role", [{"doctype": "Role", "name": r["name"], "role_name": r["name"],
                                     "desk_access": 0 if r.get("desk_access") is False else 1,
                                     "modified": now()} for r in roles])

    for wf in s.get("workflows") or []:
        states = [{"state": st["state"], "doc_status": str(st.get("doc_status", 0)),
                   "allow_edit": st.get("allow_edit"), "update_field": st.get("update_field"),
                   "update_value": st.get("update_value"), "is_optional_state": 0}
                  for st in wf.get("states") or []]
        transitions = [{"state": t["from"], "action": t["action"], "next_state": t["to"],
                        "allowed": t["allowed"], "allow_self_approval": 0 if t.get("allow_self_approval") is False else 1,
                        "condition": t.get("condition")} for t in wf.get("transitions") or []]
        merge_fixture(ctx, "workflow", [{
            "doctype": "Workflow", "name": wf["name"], "workflow_name": wf["name"], "document_type": wf["doctype"],
            "is_active": 1, "override_status": 1 if wf.get("override_status") else 0,
            "send_email_alert": 1 if wf.get("send_email_alert") else 0,
            "workflow_state_field": wf.get("state_field", "workflow_state"),
            "states": states, "transitions": transitions, "modified": now()}])
        merge_fixture(ctx, "workflow_state", [{"doctype": "Workflow State", "name": st["state"],
                                               "workflow_state_name": st["state"], "style": st.get("style", ""),
                                               "modified": now()} for st in wf.get("states") or []])
        merge_fixture(ctx, "workflow_action_master", [{"doctype": "Workflow Action Master", "name": t["action"],
                                                       "workflow_action_name": t["action"], "modified": now()}
                                                      for t in wf.get("transitions") or []])

    for pf in s.get("print_formats") or []:
        mod = ensure_module(ctx, pf["module"])
        folder = os.path.join(mod, "print_format", scrub(pf["name"]))
        write_once(ctx, os.path.join(mod, "print_format", "__init__.py"), "")
        write_once(ctx, os.path.join(folder, "__init__.py"), "")
        write_once(ctx, os.path.join(folder, scrub(pf["name"]) + ".json"), dump_json({
            "creation": now(), "custom_format": 1, "disabled": 0, "doc_type": pf["doctype"], "docstatus": 0,
            "doctype": "Print Format", "html": "", "modified": now(), "modified_by": SELF_STAMP,
            "module": pf["module"], "name": pf["name"], "owner": SELF_STAMP, "print_format_type": "Jinja",
            "standard": "Yes", "font_size": 12, "margin_top": 15.0, "margin_bottom": 15.0,
            "margin_left": 15.0, "margin_right": 15.0, "page_number": "Hide"}))

    for rp in s.get("reports") or []:
        mod = ensure_module(ctx, rp["module"])
        folder = os.path.join(mod, "report", scrub(rp["name"]))
        write_once(ctx, os.path.join(mod, "report", "__init__.py"), "")
        write_once(ctx, os.path.join(folder, "__init__.py"), "")
        fmt = {"year": year, "publisher": pub, "name": rp["name"]}
        rtype = rp.get("type", "Script Report")
        write_once(ctx, os.path.join(folder, scrub(rp["name"]) + ".json"), dump_json({
            "add_total_row": 0, "columns": [], "creation": now(), "disabled": 0, "docstatus": 0, "doctype": "Report",
            "filters": [], "is_standard": "Yes", "modified": now(), "modified_by": SELF_STAMP, "module": rp["module"],
            "name": rp["name"], "owner": SELF_STAMP, "prepared_report": 0, "ref_doctype": rp["ref_doctype"],
            "report_name": rp["name"], "report_type": rtype,
            "roles": [{"role": r} for r in rp.get("roles") or ["System Manager"]]}))
        if rtype == "Script Report":
            write_once(ctx, os.path.join(folder, scrub(rp["name"]) + ".py"), REPORT_PY.format(**fmt))
            write_once(ctx, os.path.join(folder, scrub(rp["name"]) + ".js"), REPORT_JS.format(**fmt))

    check_hooks(ctx, s)
    ctx.save_manifest()


def check_hooks(ctx, s):
    """Report hooks.py entries the slice needs. The orchestrator edits hooks.py, not this tool."""
    hooks_path = os.path.join(ctx.pkg, "hooks.py")
    hooks = open(hooks_path, encoding="utf-8").read() if os.path.exists(hooks_path) else ""
    needed = []
    if s.get("custom_fields") and not re.search(r'["\']Custom Field["\']', hooks):
        needed.append('fixtures: {"dt": "Custom Field", "filters": [["module", "in", [<app modules>]]]}')
    if s.get("property_setters") and not re.search(r'["\']Property Setter["\']', hooks):
        needed.append('fixtures: {"dt": "Property Setter", "filters": [["module", "in", [<app modules>]]]}')
    if s.get("workflows"):
        for dt in ("Workflow", "Workflow State", "Workflow Action Master"):
            if not re.search(rf'["\']{dt}["\']', hooks):
                needed.append(f'fixtures: {{"dt": "{dt}", "filters": [["name", "in", [...]]]}}')
    roles = [r if isinstance(r, str) else r["name"] for r in ctx.design.get("roles") or []]
    missing_roles = [r for r in roles if r not in hooks]
    if missing_roles and not re.search(r'["\']Role["\']', hooks):
        needed.append('fixtures: {"dt": "Role", "filters": [["name", "in", [<design roles>]]]}')
    for n in needed:
        ctx.warnings.append("hooks.py needs " + n)


# ---------------------------------------------------------------- register lookup

def md_tables(path):
    """Yield (lower-case header, cells) for every row of every markdown table in a file."""
    header = None
    if not os.path.exists(path):
        return
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


def register_rows(ctx):
    """Req ID -> {'requirement': str, 'acceptance': str, 'source': str} from 01-requirements.md."""
    rows = {}
    for header, cells in md_tables(os.path.join(ctx.spec, "01-requirements.md")):
        if cells and REQ_ID.fullmatch(cells[0]):
            row = dict(zip(header, cells))
            text = row.get("requirement") or row.get("step") or row.get("report") or row.get("print format") or row.get("data set") or ""
            if row.get("rule / decision") and row["rule / decision"] not in ("—", "-", ""):
                text += f" (rule: {row['rule / decision']})"
            rows[cells[0]] = {"requirement": text, "acceptance": row.get("acceptance criteria", ""),
                              "source": row.get("source", ""), "actor": row.get("actor", "")}
    return rows


FIT_ORDER = ["standard", "configure", "extend", "integrate", "build"]
EXISTS = {"standard", "configure"}


def fit_gap(ctx):
    """Req ID -> {'fit': lower-case fit, 'object': ERPNext object, 'mechanism': str} from 02-fit-gap.md."""
    rows = {}
    for header, cells in md_tables(os.path.join(ctx.spec, "02-fit-gap.md")):
        if cells and REQ_ID.fullmatch(cells[0]) and "fit" in header:
            row = dict(zip(header, cells))
            fit = row.get("fit", "").strip().lower()
            fit = "build" if fit.startswith("build") else "integrate" if fit.startswith("integrate") else fit
            rows[cells[0]] = {"fit": fit, "object": row.get("erpnext object", ""),
                              "mechanism": row.get("mechanism", ""), "rationale": row.get("rationale", "")}
    return rows


# ---------------------------------------------------------------- brief

def brief(ctx, slice_id):
    s = ctx.slice(slice_id)
    reg = register_rows(ctx)
    out = [f"# Slice {s['id']} - {s['title']}", "",
           f"App `{ctx.app}` · depends on: {', '.join(s.get('depends_on') or []) or 'nothing'} · "
           f"design source: `docs/spec/design.yaml`", "",
           "This brief is the single input for every agent working on this slice. Do not read the full "
           "requirements register unless something here is unclear.", "", "## Requirements", "",
           "| Req ID | Requirement | Acceptance criteria | Source |", "|---|---|---|---|"]
    for rid in s.get("requirements") or []:
        r = reg.get(rid, {})
        out.append(f"| {rid} | {r.get('requirement', '(not in register)')} | {r.get('acceptance', '')} | {r.get('source', '')} |")
    out += ["", "## Data model (generated from design.yaml - change design.yaml, never the JSON)", ""]
    for dt in s.get("doctypes") or []:
        kind = "child table" if dt.get("istable") else "single" if dt.get("issingle") else "submittable" if dt.get("submittable") else "document"
        out += [f"### {dt['name']} ({kind}, module {dt['module']})", ""]
        if dt.get("autoname"):
            series = f" - series: {', '.join(f'`{x}`' for x in dt.get('naming_series') or [])}" if dt.get("naming_series") else ""
            out += [f"Naming: `{dt['autoname']}`{series}", ""]
        out += ["| Field | Label | Type | Options | Flags |", "|---|---|---|---|---|"]
        for f in expand_fields(dt.get("fields"), dt["name"]):
            if f["fieldtype"] in ("Column Break",):
                continue
            flags = [k for k in ("reqd", "read_only", "hidden", "in_list_view", "unique", "allow_on_submit") if f.get(k)]
            flags += [f"{k}: `{f[k]}`" for k in ("depends_on", "mandatory_depends_on", "fetch_from", "default") if f.get(k)]
            opts = str(f.get("options", "")).strip().replace("\n", " / ")
            out.append(f"| `{f['fieldname']}` | {f.get('label', '')} | {f['fieldtype']} | {opts} | {', '.join(flags)} |")
        if dt.get("rules"):
            out += ["", "Rules (stubs are in the controller and test; replace each `# TODO(rule ...)` with `# REQ: <id>` and the code):", ""]
            out += [f"- {r['id']} `{rule_method(r)}` on {rule_event(r)}: {r['rule']}" for r in dt["rules"]]
        if dt.get("permissions"):
            out += ["", "Permissions: " + "; ".join(f"{r} = {p}" for r, p in dt["permissions"].items())
                    + " (R read, W write, C create, S submit, X cancel, A amend, D delete)"]
        out.append("")
    for group in s.get("custom_fields") or []:
        out += [f"### Custom fields on {group['doctype']}", ""]
        for f in expand_fields(group.get("fields"), group["doctype"]):
            if f["fieldtype"] != "Column Break":
                name = f["fieldname"] if f["fieldname"].startswith("custom_") else "custom_" + f["fieldname"]
                out.append(f"- `{name}` {f.get('label', '')} ({f['fieldtype']}{', ' + str(f.get('options')) if f.get('options') else ''})")
        if group.get("rules"):
            out += ["", f"Rules on {group['doctype']} (Developer, in `overrides/` or `services/`):", ""]
            out += [f"- {r['id']} on {rule_event(r)}: {r['rule']}" for r in group["rules"]]
        out.append("")
    for wf in s.get("workflows") or []:
        out += [f"### Workflow {wf['name']} on {wf['doctype']}", ""]
        out += [f"- {t['from']} --[{t['action']} by {t['allowed']}{', if ' + t['condition'] if t.get('condition') else ''}]--> {t['to']}"
                for t in wf.get("transitions") or []]
        out.append("")
    if s.get("standard_permissions"):
        out += ["### Permissions on standard DocTypes (Developer applies these in code)", ""]
        out += [f"- {dt}: " + "; ".join(f"{r} = {p}" for r, p in roles.items()) for dt, roles in s["standard_permissions"].items()]
        out.append("")
    for pf in s.get("print_formats") or []:
        out.append(f"- Print format **{pf['name']}** for {pf['doctype']}" + (f": {pf['notes']}" if pf.get("notes") else ""))
    for rp in s.get("reports") or []:
        out.append(f"- Report **{rp['name']}** ({rp.get('type', 'Script Report')}) on {rp['ref_doctype']}" + (f": {rp['notes']}" if rp.get("notes") else ""))
    if s.get("notes"):
        out += ["", "## Design notes", "", s["notes"].strip()]
    out += ["", "## File ownership", "",
            "| Owner | Files |", "|---|---|",
            "| generator (orchestrator) | DocType `.json`, `fixtures/custom_field.json`, `property_setter.json`, `role.json`, `workflow*.json` |",
            "| Orchestrator | `hooks.py`, `modules.txt`, all `bench` commands except running tests |",
            "| Developer | DocType `.py` controllers, `overrides/`, `services/`, server tests `test_*.py` next to DocTypes, `patches/` |",
            "| UI/UX | DocType `.js`, `public/js/*.js`, `*_list.js`, print format `.json`/`.html`, `templates/`, workspace |",
            "| Tester | `tests/acceptance/test_<slice>.py`, browser check results |", "",
            "Need a change in a file you do not own? Put it in your return under *Requests*.", "",
            "## Conventions", "",
            "- Tag code and tests with the requirement they satisfy in a comment: `# REQ: FR-012` / `// REQ: FR-012`.",
            "  Never put requirement IDs in labels, descriptions, messages or anything else users see.",
            "- Run only this slice's tests, on the test site: `bench --site <test-site> run-tests --doctype \"<DocType>\"`.",
            "- Follow `docs/spec/conventions.md` if it exists (patterns from earlier approved slices).", "",
            "## Return format (every agent)", "",
            "```", "Files changed: <paths>", "Requirements covered: <IDs>", "Tests: <added / passing>",
            "Requests: <changes needed in files you do not own, e.g. hooks.py entries>",
            "Review items: <things the developer should check by hand, one line each>",
            "Open issues: <anything unresolved>", "```", ""]
    path = os.path.join(ctx.spec, "slices", f"{s['id']}-{slug(s['title'])}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"Brief written: {ctx.rel(path)}")


# ---------------------------------------------------------------- routes

def routes(ctx, slice_id):
    s = ctx.slice(slice_id)
    out = []
    for dt in s.get("doctypes") or []:
        if dt.get("istable"):
            continue
        out.append(f"/app/{slug(dt['name'])}" + ("" if dt.get("issingle") else ""))
        if not dt.get("issingle"):
            out.append(f"/app/{slug(dt['name'])}/new")
    for group in s.get("custom_fields") or []:
        out.append(f"/app/{slug(group['doctype'])}/new")
    for wf in s.get("workflows") or []:
        out.append(f"/app/{slug(wf['doctype'])}/new")
    for rp in s.get("reports") or []:
        out.append(f"/app/query-report/{rp['name']}" if rp.get("type", "Script Report") != "Report Builder"
                   else f"/app/{slug(rp['ref_doctype'])}/view/report")
    seen = []
    for r in out:
        if r not in seen:
            seen.append(r)
    return seen


# ---------------------------------------------------------------- review checklist

ITEM_RE = re.compile(r"^- \[( |x|X)\] \*\*([A-Z][A-Z0-9]*(?:\.\d+)?)\*\*")
COMMENT_RE = re.compile(r"^\s+- Comment:\s?(.*)$")
HASH_RE = re.compile(r"\s*<!--.*?-->")
GATES = {"flows": "flows-review.md", "plan": "plan-review.md"}
FIT_LABEL = {"standard": "Standard ERPNext", "configure": "ERPNext setting", "extend": "Develop (extends ERPNext)",
             "build": "Develop (new)", "integrate": "Develop (integration)", "out of scope": "Out of scope"}


def review_path(ctx, s):
    return os.path.join(ctx.spec, "slices", f"{s['id']}-{slug(s['title'])}-review.md")


def gate_path(ctx, gate):
    return os.path.join(ctx.spec, "review", GATES[gate])


def parse_review(path):
    """item_id -> {'checked': bool, 'comment': str, 'text': str}"""
    items, current, in_comment = {}, None, False
    if not os.path.exists(path):
        return items
    for line in open(path, encoding="utf-8"):
        m = ITEM_RE.match(line)
        if m:
            current = m.group(2)
            items[current] = {"checked": m.group(1).lower() == "x", "comment": "", "text": line.strip()}
            in_comment = False
            continue
        if current is None:
            continue
        c = COMMENT_RE.match(line)
        if c:
            items[current]["comment"] = c.group(1).strip()
            in_comment = True
        elif in_comment and line.startswith("    ") and line.strip() and not line.strip().startswith("- "):
            items[current]["comment"] = (items[current]["comment"] + " " + line.strip()).strip()
        elif line.startswith("#") or (line.strip().startswith("- ") and not line.startswith("  ")):
            in_comment = False
    return items


def item_state(item):
    if item["comment"]:
        return "rejected"
    return "approved" if item["checked"] else "pending"


def item_title(item):
    """Readable text of a parsed item: no checkbox, id, links or version marker."""
    text = HASH_RE.sub("", item["text"].split("** ", 1)[-1])
    return re.sub(r"\s+https?://\S+", "", text).strip()


def short(text, n=160):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def version(obj):
    """Short hash of an object; part of an item's text so any change to it needs a new approval."""
    return sha(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str))[:8]


def next_round(path):
    """Archive the current checklist as <name>-rN.md and return the new round number."""
    if not os.path.exists(path):
        return 1
    m = re.search(r"round (\d+)", open(path, encoding="utf-8").readline())
    round_no = int(m.group(1)) + 1 if m else 2
    os.replace(path, re.sub(r"\.md$", f"-r{round_no - 1}.md", path))
    return round_no


def write_checklist(ctx, path, title, intro, after_approval, sections, fix_notes_path=None, never_keep=()):
    """Write a review checklist. sections: [(heading, explanation lines, [(item_id, text)])].

    An approval carries over to the next round only for the same item with the same text.
    """
    previous = parse_review(path)
    round_no = next_round(path)
    fixes = json.load(open(fix_notes_path, encoding="utf-8")) if fix_notes_path else {}
    counts = {"approved": 0, "pending": 0}
    body = []
    for heading, explanation, entries in sections:
        if not entries:
            continue
        body += [f"## {heading}", ""]
        if explanation:
            body += explanation + [""]
        for item_id, text in entries:
            line = f"**{item_id}** {text}"
            prev = previous.get(item_id)
            same_item = prev and prev["text"].split("** ", 1)[-1].strip() == line.split("\n")[0].split("** ", 1)[-1].strip()
            keep = bool(same_item and item_state(prev) == "approved" and not item_id.startswith(tuple(never_keep)))
            body.append(f"- [{'x' if keep else ' '}] {line}")
            if prev and prev["comment"]:
                body.append(f"    - Round {round_no - 1} comment: {prev['comment']}")
                if fixes.get(item_id):
                    body.append(f"    - Fixed: {fixes[item_id]}")
            body.append("    - Comment:")
            counts["approved" if keep else "pending"] += 1
        body.append("")
    head = [f"# {title} (round {round_no})", "", f"Generated {datetime.datetime.now():%Y-%m-%d %H:%M}.", ""]
    head += intro + ["", "**How to review**", "",
                     "- Approve an item: change `[ ]` to `[x]` and leave *Comment* empty.",
                     "- Reject an item: write what is wrong after `Comment:` (the tick does not matter then).",
                     f"- Save the file and tell Claude \"review done\". Rejected items are corrected and come back in the next round;",
                     f"  approved items stay approved. {after_approval}", "",
                     f"Items: {sum(counts.values())} · already approved: {counts['approved']}", ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + body))
    print(f"Checklist written: {ctx.rel(path)} (round {round_no}, {sum(counts.values())} items, "
          f"{counts['approved']} carried over as approved)")


# ---------------------------------------------------------------- slice review

def review(ctx, slice_id, base_url, tests_summary, browser_json, fix_notes_path, base_commit):
    s = ctx.slice(slice_id)
    reg = register_rows(ctx)
    base = base_url.rstrip("/")
    sections = []  # (letter, title, [text])

    forms = []
    for dt in s.get("doctypes") or []:
        if dt.get("istable"):
            continue
        labels = [f.get("label") for f in expand_fields(dt.get("fields"), dt["name"])
                  if f["fieldtype"] not in NO_VALUE and f.get("label")]
        url = f"{base}/app/{slug(dt['name'])}" + ("" if dt.get("issingle") else "/new")
        children = [f.get("options") for f in expand_fields(dt.get("fields"), dt["name"]) if f["fieldtype"] in ("Table", "Table MultiSelect")]
        forms.append(f"**{dt['name']}** form opens and lays out sensibly: {url}  \n    Fields: {', '.join(labels)}"
                     + (f"  \n    Tables: {', '.join(children)}" if children else ""))
        if not dt.get("issingle"):
            forms.append(f"**{dt['name']}** list view shows useful columns and filters: {base}/app/{slug(dt['name'])}")
    for group in s.get("custom_fields") or []:
        labels = [f.get("label") for f in expand_fields(group.get("fields"), group["doctype"]) if f["fieldtype"] not in NO_VALUE]
        forms.append(f"New fields on **{group['doctype']}** appear in the right place: {', '.join(l for l in labels if l)} "
                     f"({base}/app/{slug(group['doctype'])}/new)")
    for p in s.get("property_setters") or []:
        forms.append(f"**{p['doctype']}**{'.' + p['field'] if p.get('field') else ''}: {p['property']} changed to `{p['value']}`")
    sections.append(("A", "Forms and fields", forms))

    rules = []
    for rid in s.get("requirements") or []:
        r = reg.get(rid, {})
        text = r.get("requirement") or "(see register)"
        acc = f"  \n    Check: {r['acceptance']}" if r.get("acceptance") else ""
        rules.append(f"{text}{acc}  \n    <!-- {rid} -->")
    sections.append(("B", "Rules and behaviour (try each one)", rules))

    flows = []
    for wf in s.get("workflows") or []:
        steps = "; ".join(f"{t['from']} → {t['to']} via '{t['action']}' ({t['allowed']})" for t in wf.get("transitions") or [])
        flows.append(f"Workflow **{wf['name']}** on {wf['doctype']} moves correctly: {steps}")
    sections.append(("C", "Workflows", flows))

    outputs = [f"Print format **{pf['name']}** prints correctly from a submitted {pf['doctype']} (Print menu)"
               for pf in s.get("print_formats") or []]
    outputs += [f"Report **{rp['name']}** shows the right rows, columns and filters: {base}/app/query-report/{rp['name']}"
                for rp in s.get("reports") or []]
    sections.append(("D", "Prints and reports", outputs))

    access = []
    for dt in s.get("doctypes") or []:
        if dt.get("permissions"):
            access.append(f"**{dt['name']}** access per role: " + "; ".join(f"{r} = {p}" for r, p in dt["permissions"].items())
                          + " (log in as a user with each role, or use Role Permission Manager)")
    for dt, roles in (s.get("standard_permissions") or {}).items():
        access.append(f"**{dt}** access per role: " + "; ".join(f"{r} = {p}" for r, p in roles.items()))
    sections.append(("E", "Permissions", access))

    demo = [str(x) for x in s.get("review_extra") or []]
    sections.append(("F", "End-to-end walkthrough", demo))

    auto = []
    if tests_summary:
        auto.append(f"Automated tests: {tests_summary}")
    if browser_json and os.path.exists(browser_json):
        b = json.load(open(browser_json, encoding="utf-8"))
        bad = [r for r in b.get("results", []) if r.get("errors")]
        auto.append(f"Browser check: {len(b.get('results', [])) - len(bad)}/{len(b.get('results', []))} pages without console errors"
                    + ("" if not bad else " - failing: " + ", ".join(r["route"] for r in bad))
                    + (f" (screenshots: {b.get('screenshots')})" if b.get("screenshots") else ""))
    if base_commit:
        try:
            files = subprocess.run(["git", "-C", ctx.app_path, "diff", "--stat", base_commit, "--", "."],
                                   capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
            untracked = subprocess.run(["git", "-C", ctx.app_path, "ls-files", "--others", "--exclude-standard"],
                                       capture_output=True, text=True, timeout=30).stdout.split()
            summary = files[-1].strip() if files else "no tracked changes"
            auto.append(f"Code changes since `{base_commit[:8]}` reviewed (`git diff {base_commit[:8]}` in apps/{ctx.app}): "
                        f"{summary}{f', plus {len(untracked)} new file(s)' if untracked else ''}")
        except (OSError, subprocess.SubprocessError):
            pass
    sections.append(("G", "Automated results and code", auto))

    write_checklist(
        ctx, review_path(ctx, s), f"Review - Slice {s['id']} {s['title']}",
        [f"Log in at {base} and check each item."],
        "The next slice starts only when every item is approved.",
        [(f"{letter}. {title}", [], [(f"{letter}{i}", t) for i, t in enumerate(entries, 1)])
         for letter, title, entries in sections],
        fix_notes_path, never_keep=("G",))  # automated results are re-confirmed every round


# ---------------------------------------------------------------- gate 1: business flows

def load_flows(ctx):
    path = os.path.join(ctx.spec, "flows.yaml")
    if not os.path.exists(path):
        sys.exit(f"ERROR: {ctx.rel(path)} not found - the Business Analyst writes it "
                 "(frappe-requirements-analyst → references/flows-yaml.md)")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def step_fit(step, fg):
    fit = str(step.get("fit") or "").strip().lower()
    if fit:
        return fit
    fits = [fg.get(r, {}).get("fit") for r in step.get("reqs") or []]
    fits = [f for f in fits if f in FIT_ORDER]
    return max(fits, key=FIT_ORDER.index) if fits else ""


def flow_reqs(fl):
    ids = list(fl.get("requirements") or [])
    for st in fl.get("steps") or []:
        ids += [r for r in st.get("reqs") or [] if r not in ids]
    return ids


def check_flows(data, reg):
    errors, seen = [], set()
    for i, fl in enumerate(data.get("flows") or [], 1):
        fid = str(fl.get("id") or f"#{i}")
        if not re.fullmatch(r"F\d+", fid):
            errors.append(f"flow {fid}: id must look like F01")
        if fid in seen:
            errors.append(f"flow {fid}: duplicate id")
        seen.add(fid)
        for key in ("title", "purpose", "steps"):
            if not fl.get(key):
                errors.append(f"{fid}: '{key}' is missing")
        steps = fl.get("steps") or []
        for n, st in enumerate(steps, 1):
            if not st.get("actor") or not st.get("action"):
                errors.append(f"{fid} step {n}: needs actor and action")
            fit = str(st.get("fit") or "").strip().lower()
            if fit and fit not in FIT_LABEL:
                errors.append(f"{fid} step {n}: fit '{fit}' must be one of {', '.join(FIT_LABEL)}")
            for b in st.get("branches") or []:
                to = b.get("to")
                if not b.get("if") or not (to == "end" or (isinstance(to, int) and 1 <= to <= len(steps))):
                    errors.append(f"{fid} step {n}: each branch needs 'if' and 'to' (a step number 1-{len(steps)} or end)")
        for rid in flow_reqs(fl):
            if reg and rid not in reg:
                errors.append(f"{fid}: {rid} is not in 01-requirements.md")
    if not data.get("flows"):
        errors.append("flows.yaml has no flows")
    return errors


def mermaid(fl, fg):
    def lbl(text):
        return short(text, 90).replace('"', "#quot;")
    steps = fl.get("steps") or []
    out = ["```mermaid", "flowchart TD", f'  start(["{lbl(fl.get("trigger") or "Start")}"])']
    groups = {}
    for n, st in enumerate(steps, 1):
        rec = f"<br/><i>{lbl(st['record'])}</i>" if st.get("record") else ""
        out.append(f'  s{n}["{n}. {lbl(st["actor"])}: {lbl(st["action"])}{rec}"]')
        fit = step_fit(st, fg)
        groups.setdefault("exists" if fit in EXISTS else "oos" if fit == "out of scope" else "develop" if fit else "", []).append(f"s{n}")
    out.append(f'  done(["{lbl(fl.get("ends_with") or "End")}"])')
    if steps:
        out.append("  start --> s1")
    for n, st in enumerate(steps, 1):
        if st.get("branches"):
            for b in st["branches"]:
                out.append(f'  s{n} -->|"{lbl(b["if"])}"| {"done" if b["to"] == "end" else "s" + str(b["to"])}')
        elif st.get("end") or n == len(steps):
            out.append(f"  s{n} --> done")
        else:
            out.append(f"  s{n} --> s{n + 1}")
    out += ["  classDef exists fill:#e6f4ea,stroke:#1e8e3e,color:#000",
            "  classDef develop fill:#fef3e0,stroke:#e37400,color:#000",
            "  classDef oos fill:#f1f3f4,stroke:#9aa0a6,stroke-dasharray:4,color:#000"]
    out += [f"  class {','.join(ids)} {cls}" for cls, ids in groups.items() if cls]
    return out + ["```"]


def assumption_text(a):
    if isinstance(a, dict):
        return a.get("text", "") + (f" (if not confirmed we use: {a['default']})" if a.get("default") else "")
    return str(a)


def flows_review(ctx, fix_notes_path):
    data, reg, fg = load_flows(ctx), register_rows(ctx), fit_gap(ctx)
    errors = check_flows(data, reg)
    if errors:
        print("\n".join("ERROR  " + e for e in errors))
        sys.exit("Fix flows.yaml first")
    flows = data["flows"]
    summary = ["| Flow | Title | Steps | Standard ERPNext / setting | To develop | Roles |", "|---|---|---|---|---|---|"]
    sections = []
    for fl in flows:
        steps = fl.get("steps") or []
        fits = [step_fit(st, fg) for st in steps]
        n_exists = sum(1 for f in fits if f in EXISTS)
        n_dev = sum(1 for f in fits if f and f not in EXISTS and f != "out of scope")
        summary.append(f"| {fl['id']} | {fl['title']} | {len(steps)} | {n_exists} | {n_dev} | {', '.join(fl.get('roles') or [])} |")
        ex = [f"**Purpose:** {fl['purpose']}", "",
              f"**Starts when:** {fl.get('trigger', '—')} · **Ends with:** {fl.get('ends_with', '—')}  ",
              f"**Who is involved:** {', '.join(fl.get('roles') or []) or '—'}  ",
              f"**Source:** {fl.get('source', '—')}", ""]
        ex += mermaid(fl, fg) + [""]
        ex += ["| # | Who | What they do | Record in ERPNext | How | Requirements |", "|---|---|---|---|---|---|"]
        for n, (st, fit) in enumerate(zip(steps, fits), 1):
            branches = "; ".join(f"if {b['if']} → {'end' if b['to'] == 'end' else 'step ' + str(b['to'])}" for b in st.get("branches") or [])
            action = st["action"] + (f" ({branches})" if branches else "")
            ex.append(f"| {n} | {st['actor']} | {short(action, 220)} | {st.get('record', '—')} | {FIT_LABEL.get(fit, '—')} | "
                      f"{', '.join(st.get('reqs') or [])} |")
        if fl.get("exceptions"):
            ex += ["", "**Exceptions and special cases**", ""] + [f"- {x}" for x in fl["exceptions"]]
        if fl.get("outputs"):
            ex += ["", f"**Documents, prints and reports produced:** {', '.join(fl['outputs'])}"]
        exists = [f"{n}. {short(st['action'], 90)}" for n, (st, f) in enumerate(zip(steps, fits), 1) if f in EXISTS]
        dev = [f"{n}. {short(st['action'], 90)}" + (f" → {st['record']}" if st.get("record") else "")
               for n, (st, f) in enumerate(zip(steps, fits), 1) if f and f not in EXISTS and f != "out of scope"]
        ex += ["", "**Already in ERPNext (no development):** " + ("; ".join(exists) or "none"),
               "", "**To be developed:** " + ("; ".join(dev) or "nothing — standard ERPNext covers this flow"),
               "", f"**Requirements covered ({len(flow_reqs(fl))}):** {', '.join(flow_reqs(fl))}"]
        items = [(fl["id"], f"Flow **{fl['title']}** works as explained above: steps, order, who does what, and what is "
                            f"standard vs developed <!-- v:{version(fl)} -->")]
        items += [(f"{fl['id']}.{i}", f"Confirm: {assumption_text(a)} <!-- v:{version(a)} -->")
                  for i, a in enumerate(fl.get("assumptions") or [], 1)]
        sections.append((f"{fl['id']} {fl['title']}", ex, items))

    in_flows = {rid for fl in flows for rid in flow_reqs(fl)}
    outside = [rid for rid in reg if rid not in in_flows and fg.get(rid, {}).get("fit") != "out of scope"]
    if outside:
        ex = ["Requirements the documents state that are not a step of any flow (masters, reports, rules, non-functional ...).", ""]
        ex += [f"- **{rid}** {short(reg[rid]['requirement'])} — {FIT_LABEL.get(fg.get(rid, {}).get('fit'), 'not mapped yet')}"
               for rid in outside]
        sections.append(("Other requirements", ex, [("R1", f"This list is complete and correct <!-- v:{version(outside)} -->")]))
    oos = [str(x) for x in data.get("out_of_scope") or []]
    oos += [f"{rid} {short(reg.get(rid, {}).get('requirement', ''))} — {short(fg[rid].get('rationale'), 120)}"
            for rid in fg if fg[rid]["fit"] == "out of scope"]
    if oos:
        sections.append(("Out of scope", [f"- {x}" for x in oos],
                         [("X1", f"Leaving these out is agreed <!-- v:{version(oos)} -->")]))
    legend = ["These are the business flows found in the documents, with how each step will run in ERPNext. "
              "In the diagrams, **green** steps use standard ERPNext or a setting, **orange** steps are developed, "
              "grey steps are out of scope.", ""] + summary
    write_checklist(ctx, gate_path(ctx, "flows"), "Business flows - approval", legend,
                    "Design starts only when every flow is approved.", sections, fix_notes_path)


# ---------------------------------------------------------------- gate 2: slice plan

def slice_scope(s, reg, fg):
    """(already in ERPNext, to develop, business rules to develop) as readable lines."""
    exists, build, rules = [], [], []
    for rid in s.get("requirements") or []:
        fit = fg.get(rid, {}).get("fit", "")
        text = f"{short(reg.get(rid, {}).get('requirement') or '(not in register)', 200)} ({rid})"
        if fit in EXISTS:
            obj = fg[rid].get("object")
            exists.append(text + (f" — {obj}" if obj else "") + (" — setting" if fit == "configure" else ""))
        elif fit != "out of scope":
            rules.append(text)
    for dt in s.get("doctypes") or []:
        fields = [f for f in expand_fields(dt.get("fields"), dt["name"]) if f["fieldtype"] not in NO_VALUE]
        kind = ("table inside a form" if dt.get("istable") else "settings page" if dt.get("issingle")
                else "submittable document" if dt.get("submittable") else "document")
        series = f", numbered {', '.join(dt['naming_series'])}" if dt.get("naming_series") else ""
        build.append(f"New {kind} **{dt['name']}** ({len(fields)} fields{series})")
    for g in s.get("custom_fields") or []:
        labels = [f.get("label") or f["fieldname"] for f in expand_fields(g.get("fields"), g["doctype"]) if f["fieldtype"] not in NO_VALUE]
        build.append(f"{len(labels)} new field(s) on standard **{g['doctype']}**: {', '.join(labels)}")
    for p in s.get("property_setters") or []:
        build.append(f"Change on standard **{p['doctype']}**{'.' + p['field'] if p.get('field') else ''}: {p['property']} → `{p['value']}`")
    for wf in s.get("workflows") or []:
        states = " → ".join(st["state"] for st in wf.get("states") or [])
        build.append(f"Approval workflow **{wf['name']}** on {wf['doctype']}: {states}")
    build += [f"Print format **{pf['name']}** for {pf['doctype']}" + (f" ({pf['notes']})" if pf.get("notes") else "")
              for pf in s.get("print_formats") or []]
    build += [f"Report **{rp['name']}** on {rp['ref_doctype']}" + (f" ({rp['notes']})" if rp.get("notes") else "")
              for rp in s.get("reports") or []]
    for dt, roles in (s.get("standard_permissions") or {}).items():
        build.append(f"Access on standard **{dt}**: " + "; ".join(f"{r} = {p}" for r, p in roles.items()))
    return exists, build, rules


def slice_flows(ctx, s):
    """'F01 Title: steps 1, 2' lines for the flow steps whose requirements are in the slice."""
    path = os.path.join(ctx.spec, "flows.yaml")
    if not os.path.exists(path):
        return []
    reqs, out = set(s.get("requirements") or []), []
    for fl in load_flows(ctx).get("flows") or []:
        steps = [str(n) for n, st in enumerate(fl.get("steps") or [], 1) if reqs & set(st.get("reqs") or [])]
        if steps:
            out.append(f"{fl['id']} {fl['title']}: step{'s' if len(steps) > 1 else ''} {', '.join(steps)}")
        elif reqs & set(fl.get("requirements") or []):
            out.append(f"{fl['id']} {fl['title']}")
    return out


def plan_review(ctx, fix_notes_path):
    ctx.require_design()
    errors, _ = validate(ctx, set(register_rows(ctx)) or None)
    if errors:
        print("\n".join("ERROR  " + e for e in errors))
        sys.exit("Fix design.yaml first (run validate)")
    reg, fg = register_rows(ctx), fit_gap(ctx)
    summary = ["| # | Slice | Business flows | Depends on | Standard ERPNext / setting | To develop | Status |",
               "|---|---|---|---|---|---|---|"]
    sections = []
    for i, s in enumerate(ctx.slices(), 1):
        exists, build, rules = slice_scope(s, reg, fg)
        flows = slice_flows(ctx, s)
        summary.append(f"| {i} | {s['id']} {s['title']} | {', '.join(f.split(' ', 1)[0] for f in flows) or '—'} | "
                       f"{', '.join(s.get('depends_on') or []) or '—'} | {len(exists)} | {len(build) + len(rules)} | "
                       f"{s.get('status', 'pending')} |")
        ex = [f"**What it delivers:** {s.get('summary') or short((s.get('notes') or '').strip().split(chr(10))[0], 300) or '—'}  ",
              f"**Business flows:** {'; '.join(flows) or '—'}  ",
              f"**Depends on:** {', '.join(s.get('depends_on') or []) or 'nothing'}"
              + (" · built before this plan, reviewed as is" if s.get("generate") is False else ""), "",
              f"**Already in ERPNext — no development ({len(exists)}):**", ""]
        ex += [f"- {x}" for x in exists] or ["- none"]
        ex += ["", f"**To be developed ({len(build) + len(rules)}):**", ""]
        ex += [f"- {x}" for x in build]
        if rules:
            ex += ["- Business rules and behaviour:"] + [f"    - {x}" for x in rules]
        if not build and not rules:
            ex.append("- nothing — configuration only")
        if s.get("review_extra"):
            ex += ["", "**How you will check it once built:**", ""] + [f"- {x}" for x in s["review_extra"]]
        scope = {k: v for k, v in s.items() if k != "status"}
        sections.append((f"{s['id']} {s['title']}", ex,
                         [(s["id"], f"Slice **{s['title']}**: the scope above is right <!-- v:{version(scope)} -->")]))
    order = [s["id"] for s in ctx.slices()]
    sections.insert(0, ("Build order", ["Slices are built and reviewed one at a time in this order; each needs only earlier ones.", ""]
                        + summary, [("O1", f"The build order is right <!-- v:{version(order)} -->")]))
    write_checklist(ctx, gate_path(ctx, "plan"), "Build plan - approval",
                    ["What will be built, slice by slice: what standard ERPNext already covers and what is developed."],
                    "Building starts only when the whole plan is approved.", sections, fix_notes_path)


# ---------------------------------------------------------------- progress page

def checklist_counts(path):
    items = parse_review(path)
    states = [item_state(v) for v in items.values()]
    return items, {k: states.count(k) for k in ("approved", "rejected", "pending")}


def progress(ctx):
    reg, fg = register_rows(ctx), fit_gap(ctx)
    out = [f"# Progress - {ctx.app}", "", f"Updated {datetime.datetime.now():%Y-%m-%d %H:%M}. "
           "Claude refreshes this page after every step.", "", "## Approvals before building", "",
           "| Gate | Checklist | Status |", "|---|---|---|"]
    for gate, label in (("flows", "Business flows"), ("plan", "Build plan")):
        path = gate_path(ctx, gate)
        if os.path.exists(path):
            items, c = checklist_counts(path)
            status = ("Approved" if c["approved"] == len(items) and items else
                      f"{c['approved']}/{len(items)} approved, {c['rejected']} to correct, {c['pending']} waiting for you")
        else:
            status = "Not ready yet"
        out.append(f"| {label} | `{ctx.rel(path)}` | {status} |")
    if not ctx.slices():
        out += ["", "Slices appear here once the design is written."]
    else:
        out += ["", "## Slices", "", "| Slice | Status | Done | Open |", "|---|---|---|---|"]
        details = []
        for s in ctx.slices():
            exists, build, rules = slice_scope(s, reg, fg)
            path = review_path(ctx, s)
            brief_file = os.path.join(ctx.spec, "slices", f"{s['id']}-{slug(s['title'])}.md")
            items, c = checklist_counts(path) if os.path.exists(path) else ({}, {})
            details += ["", f"### {s['id']} {s['title']}", ""]
            if s.get("status") == "approved":
                folder = os.path.dirname(path)
                rounds = 1 + len([f for f in os.listdir(folder) if f.startswith(os.path.basename(path)[:-3] + "-r")]) if os.path.isdir(folder) else 1
                out.append(f"| {s['id']} {s['title']} | Approved ({rounds} review round{'s' if rounds > 1 else ''}) | "
                           f"{str(len(items)) + ' checks' if items else 'all'} | — |")
                details += ["**Done:**", ""] + ([f"- {x}" for x in build + rules + exists] or ["- configuration only"])
            elif items:
                m = re.search(r"round (\d+)", open(path, encoding="utf-8").readline())
                out.append(f"| {s['id']} {s['title']} | In review (round {m.group(1) if m else 1}) | "
                           f"{c['approved']}/{len(items)} approved | {c['rejected']} to fix, {c['pending']} to review |")
                groups = {"approved": [], "rejected": [], "pending": []}
                for k, v in items.items():
                    st = item_state(v)
                    groups[st].append(f"- {k} {short(item_title(v), 140)}" + (f" — *{v['comment']}*" if st == "rejected" else ""))
                for st, label in (("approved", "Done and approved"), ("rejected", "Being fixed (your comments)"),
                                  ("pending", "Waiting for your review")):
                    if groups[st]:
                        details += [f"**{label}:**", ""] + groups[st] + [""]
                details.append(f"Checklist: `{ctx.rel(path)}`")
            else:
                started = os.path.exists(brief_file)
                out.append(f"| {s['id']} {s['title']} | {'Being built' if started else 'Not started'} | — | "
                           f"{len(build) + len(rules)} to develop, {len(exists)} standard |")
                details += ["**To be developed:**", ""] + ([f"- {x}" for x in build + rules] or ["- configuration only"])
                if exists:
                    details += ["", "**Standard ERPNext (set up and checked, not developed):**", ""] + [f"- {x}" for x in exists]
        out += details
    path = os.path.join(ctx.spec, "PROGRESS.md")
    os.makedirs(ctx.spec, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"Progress page written: {ctx.rel(path)}")


# ---------------------------------------------------------------- review status

def review_status(ctx, slice_id, gate, as_json):
    path = gate_path(ctx, gate) if gate else review_path(ctx, ctx.slice(slice_id))
    if not os.path.exists(path):
        sys.exit(f"ERROR: no review checklist yet: {ctx.rel(path)}")
    items = parse_review(path)
    states = {k: item_state(v) for k, v in items.items()}
    result = {
        "checklist": gate or slice_id, "file": ctx.rel(path), "total": len(items),
        "approved": sum(1 for v in states.values() if v == "approved"),
        "rejected": [{"item": k, "comment": items[k]["comment"], "text": items[k]["text"]} for k, v in states.items() if v == "rejected"],
        "pending": [k for k, v in states.items() if v == "pending"],
    }
    result["all_approved"] = result["total"] > 0 and result["approved"] == result["total"]
    if as_json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
    else:
        print(f"{result['file']}: {result['approved']}/{result['total']} approved, "
              f"{len(result['rejected'])} rejected, {len(result['pending'])} not reviewed")
        for r in result["rejected"]:
            print(f"  REJECTED {r['item']}: {r['comment']}")
        if result["pending"]:
            print("  NOT REVIEWED: " + ", ".join(result["pending"]))
        print("ALL APPROVED" if result["all_approved"] else "NOT APPROVED YET")
    sys.exit(0 if result["all_approved"] else 1)


# ---------------------------------------------------------------- tasks and checkpoints

MAX_TASK_REQS = 4
MODES = ("on-issue", "every-task", "per-group", "slice")
DEV_SHARED = ["{pkg}/overrides/*", "{pkg}/services/*", "{pkg}/tests/*"]
PERM_FILES = ["{pkg}/permissions.py", "{pkg}/setup/*", "{pkg}/install.py", "{pkg}/patches/*", "{pkg}/patches.txt"]
BLOCKED_BASH = r"\bbench\b.*\b(migrate|reinstall|drop-site|restore|new-site|install-app|uninstall-app|build)\b|\bgit\b\s+(commit|reset|checkout|stash|clean|restore|rebase|push)\b"


def kb_dir():
    """Company knowledge base shared across projects: lessons from reviews and approved slices."""
    return os.path.expanduser(os.environ.get("FRAPPE_KB_DIR") or "~/.claude/frappe-kb")


def tasks_path(ctx, s):
    return os.path.join(ctx.spec, "slices", f"{s['id']}-tasks.yaml")


def task_dir(ctx, s):
    return os.path.join(ctx.spec, "slices", f"{s['id']}-tasks")


def active_path(ctx):
    return os.path.join(ctx.bench, ".claude", "frappe-active-task.json")


def load_yaml(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def save_yaml(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120)


def git(ctx, *cmd, env=None):
    r = subprocess.run(["git", "-C", ctx.app_path, *cmd], capture_output=True, text=True, timeout=60, env=env)
    return r.stdout.strip() if r.returncode == 0 else None


def snapshot(ctx, ref):
    """Commit the whole working tree (tracked and new files) to refs/frappe-checkpoints/<ref>
    without touching HEAD, the index or any file. Returns the commit sha, or None."""
    index = os.path.join(ctx.app_path, ".git", "frappe-checkpoint-index")
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        head = git(ctx, "rev-parse", "--verify", "-q", "HEAD")
        if head:
            git(ctx, "read-tree", "HEAD", env=env)
        git(ctx, "add", "-A", env=env)
        tree = git(ctx, "write-tree", env=env)
        if not tree:
            return None
        commit = git(ctx, "commit-tree", tree, *(["-p", head] if head else []), "-m", f"checkpoint {ref}", env=env)
        if commit:
            git(ctx, "update-ref", f"refs/frappe-checkpoints/{ref}", commit)
        return commit
    finally:
        if os.path.exists(index):
            os.remove(index)


def requirement_owner(reg, rid, names):
    text = (reg.get(rid, {}).get("requirement") or "").lower()
    return next((n for n in names if n.lower() in text), None)


def derive_tasks(ctx, s):
    """Split a slice into small tasks: one per DocType controller, logic on a standard DocType,
    permissions, report, and the UI side of each. Requirements are assigned by the design's
    `requirements`/`rules` keys, else by the DocType name appearing in the requirement text."""
    reg = register_rows(ctx)
    pkg = ctx.app
    slice_reqs = list(s.get("requirements") or [])
    doctypes = [d for d in s.get("doctypes") or [] if not d.get("istable")]
    children = {d["name"]: d for d in s.get("doctypes") or [] if d.get("istable")}
    standard = []
    for group in s.get("custom_fields") or []:
        standard.append(group["doctype"])
    standard += [wf["doctype"] for wf in s.get("workflows") or []]
    standard = [d for i, d in enumerate(standard) if d not in standard[:i] and d not in ctx.all_doctypes()]
    groups = {g["doctype"]: g for g in s.get("custom_fields") or []}
    claimed = set()

    def claim(ids):
        ids = [i for i in ids if i in slice_reqs and i not in claimed]
        claimed.update(ids)
        return ids

    def folder(dt):
        return f"{pkg}/{scrub(dt['module'])}/doctype/{scrub(dt['name'])}"

    planned = []  # (owner, title, group, complexity, reqs, rules, files, after)
    for dt in doctypes:
        rules = dt.get("rules") or []
        explicit = claim([r["id"] for r in rules] + list(dt.get("requirements") or []))
        planned.append(["developer", f"{dt['name']} controller", dt["name"], "high", explicit, rules,
                        [f"{folder(dt)}/{scrub(dt['name'])}.py", f"{folder(dt)}/test_{scrub(dt['name'])}.py"]
                        + [f"{folder(children[f['options']])}/{scrub(f['options'])}.py"
                           for f in expand_fields(dt.get("fields"), dt["name"]) if f.get("options") in children], []])
    for name in standard:
        g = groups.get(name, {})
        rules = g.get("rules") or []
        planned.append(["developer", f"Logic on {name}", name, "high",
                        claim([r["id"] for r in rules] + list(g.get("requirements") or [])), rules,
                        [p.format(pkg=pkg) for p in DEV_SHARED], []])
    for rp in s.get("reports") or []:
        folder_rp = f"{pkg}/{scrub(rp['module'])}/report/{scrub(rp['name'])}"
        planned.append(["developer", f"Report {rp['name']} (data)", rp["name"], "high",
                        claim(rp.get("requirements") or []), [], [f"{folder_rp}/{scrub(rp['name'])}.py"],
                        [rp["ref_doctype"]]])
    # requirements without an explicit owner: match by name in the requirement text
    names = [d["name"] for d in doctypes] + standard + [r["name"] for r in s.get("reports") or []]
    for rid in slice_reqs:
        if rid in claimed:
            continue
        owner = requirement_owner(reg, rid, sorted(names, key=len, reverse=True))
        for p in planned:
            if owner and p[2] == owner:
                p[4].append(rid)
                claimed.add(rid)
                break
    if s.get("standard_permissions"):
        planned.append(["developer", "Permissions on standard DocTypes", "permissions", "low", [], [],
                        [p.format(pkg=pkg) for p in PERM_FILES], []])
    rest = [r for r in slice_reqs if r not in claimed]
    if rest:
        planned.append(["developer", "Remaining rules", "remaining", "high", rest, [],
                        [p.format(pkg=pkg) for p in DEV_SHARED], ["*"]])
    for dt in doctypes:
        planned.append(["ui-ux", f"{dt['name']} form and list view", dt["name"], "low", [], [],
                        [f"{folder(dt)}/{scrub(dt['name'])}.js", f"{folder(dt)}/{scrub(dt['name'])}_list.js"], []])
    for name in standard:
        planned.append(["ui-ux", f"{name} form script", name, "low", [], [], [f"{pkg}/public/js/{scrub(name)}*.js"], []])
    for pf in s.get("print_formats") or []:
        planned.append(["ui-ux", f"Print format {pf['name']}", pf["doctype"], "low", [], [],
                        [f"{pkg}/{scrub(pf['module'])}/print_format/{scrub(pf['name'])}/*", f"{pkg}/templates/*"], []])
    for rp in s.get("reports") or []:
        folder_rp = f"{pkg}/{scrub(rp['module'])}/report/{scrub(rp['name'])}"
        planned.append(["ui-ux", f"Report {rp['name']} (filters)", rp["name"], "low", [], [],
                        [f"{folder_rp}/{scrub(rp['name'])}.js"], []])

    tasks, by_group = [], {}
    for owner, title, group, complexity, reqs, rules, files, after in planned:
        if owner == "developer" and not reqs and not rules and group not in ("permissions",):
            continue  # nothing to implement: the generated stub is enough
        chunks = [reqs[i:i + MAX_TASK_REQS] for i in range(0, len(reqs), MAX_TASK_REQS)] or [[]]
        for n, chunk in enumerate(chunks, 1):
            tid = f"T{len(tasks) + 1:02d}"
            deps = [t["id"] for t in tasks if t["owner"] == owner and t["group"] == group]
            if after == ["*"]:
                deps = [t["id"] for t in tasks if t["owner"] == "developer"]
            else:
                deps += [t["id"] for g in after for t in by_group.get(g, []) if t["owner"] == "developer"]
            tasks.append({
                "id": tid, "title": title + (f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""),
                "owner": owner, "group": group, "complexity": complexity,
                "requirements": chunk,
                "rules": [{"id": r["id"], "rule": r["rule"], "event": rule_event(r), "method": rule_method(r)}
                          for r in rules if r["id"] in chunk or not chunk],
                "files": files, "depends_on": sorted(set(deps)), "status": "pending",
                "max_writes": 15 + 5 * len(chunk),
            })
            by_group.setdefault(group, []).append(tasks[-1])
    return tasks


def make_tasks(ctx, slice_id, mode, force):
    s = ctx.slice(slice_id)
    path = tasks_path(ctx, s)
    if os.path.exists(path) and not force:
        data = load_yaml(path, {})
        print(f"Tasks already exist: {ctx.rel(path)} ({len(data.get('tasks') or [])} tasks, mode {data.get('mode')}). "
              "Use --force to rebuild (task progress is lost).")
        return
    tasks = derive_tasks(ctx, s)
    save_yaml(path, {"slice": s["id"], "mode": mode, "base_commit": git(ctx, "rev-parse", "--verify", "-q", "HEAD"),
                     "created": f"{datetime.datetime.now():%Y-%m-%d %H:%M}", "tasks": tasks})
    print(f"Tasks written: {ctx.rel(path)} (mode {mode})")
    for t in tasks:
        print(f"  {t['id']} [{t['owner']}, {t['complexity']}] {t['title']}: {', '.join(t['requirements']) or '-'}"
              + (f" after {', '.join(t['depends_on'])}" if t["depends_on"] else ""))


def python_signatures(path):
    import ast
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError, ValueError):
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            out.append(f"def {node.name}({', '.join(a.arg for a in node.args.args)})")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
            out.append(f"class {node.name}: {', '.join(methods)}")
    return out


def slice_files_so_far(ctx, base):
    changed = (git(ctx, "diff", "--name-only", base) or "").split() if base else []
    changed += (git(ctx, "ls-files", "--others", "--exclude-standard") or "").split()
    return sorted({f for f in changed if not f.startswith("docs/")})


def matching_patterns(s, limit=3):
    index = os.path.join(kb_dir(), "patterns", "index.json")
    if not os.path.exists(index):
        return []
    entries = json.load(open(index, encoding="utf-8"))
    mine = {d["name"] for d in s.get("doctypes") or []} | {g["doctype"] for g in s.get("custom_fields") or []} \
        | {w["doctype"] for w in s.get("workflows") or []} | set(s.get("standard_permissions") or {})
    words = set(scrub(s["title"]).split("_"))
    scored = []
    for e in entries:
        score = 2 * len(mine & set(e.get("doctypes") or [])) + len(words & set(scrub(e.get("title", "")).split("_")))
        if score:
            scored.append((score, e))
    return [e for _, e in sorted(scored, key=lambda x: -x[0])[:limit]]


def task_prompt(ctx, s, data, t):
    """The task's whole context: stable parts first (shared across tasks, so they hit the prompt
    cache), task-specific parts last."""
    reg = register_rows(ctx)
    brief_rel = f"docs/spec/slices/{s['id']}-{slug(s['title'])}.md"
    report_rel = f"docs/spec/slices/{s['id']}-tasks/{t['id']}.json"
    conventions = os.path.join(ctx.spec, "conventions.md")
    lessons = os.path.join(kb_dir(), "lessons.md")
    out = [f"# Slice {s['id']} {s['title']} — working rules", "",
           f"Bench `{ctx.bench}`, app `apps/{ctx.app}`. Paths below are relative to `apps/{ctx.app}`.", "",
           "- Work on **one task only** (at the end). A guard blocks writes outside the task's files, and blocks "
           "migrate / install / build / git commands: the orchestrator does those.",
           "- Look up standard ERPNext with the `frappe` MCP tools (`get_doctype_schema`, `search_methods`, "
           "`get_method_schema`, `list_documents` on Custom Field / Property Setter) instead of reading `apps/erpnext`.",
           "- Check site data with the MCP read tools (`get_document`, `list_documents`, `get_count`), not "
           "`bench console` / `bench execute`; use those only for what MCP cannot answer.",
           "- Read code with Read / Grep / Glob, change it with Edit / Write. Shell writes into apps/ (`cat >`, "
           "heredocs, `sed -i`, inline python) are blocked. Bash is for `run-tests`.",
           "- Read only what you need: Grep for the function, then Read with offset/limit around it. Whole-file "
           "dumps (`cat`, long `sed -n`) fill your context and every later step pays for them again.",
           "- Tag what you implement with `# REQ: <id>` / `// REQ: <id>`; never put IDs in labels or messages.",
           "- Run only this task's tests on the test site (`bench --site <test-site> run-tests --doctype \"<DocType>\"` "
           "or `--module`). Stop after 2 failed fix attempts on the same test and return `pause`.",
           "- Need a change in a file you do not own (hooks.py, DocType JSON, another task's file)? Do not work around "
           "it: list it under `requests`.", ""]
    if os.path.exists(conventions):
        out += ["## Project conventions (follow them)", "", open(conventions, encoding="utf-8").read().strip()[:6000], ""]
    if os.path.exists(lessons):
        out += ["## Lessons from earlier reviews (do not repeat these mistakes)", "",
                open(lessons, encoding="utf-8").read().strip()[:6000], ""]
    out += ["## Return", "", f"Write `{report_rel}`:", "", "```json",
            json.dumps({"task": t["id"], "status": "done | pause", "summary": "one line",
                        "files": ["paths you changed"], "requirements": ["FR-012"],
                        "tests": "6 passed, 0 failed",
                        "requests": [{"file": "hooks.py", "kind": "hooks | schema | other", "change": "what and why"}],
                        "review_items": ["what the developer should check by hand"],
                        "open_questions": [], "handoff": ["helpers, factories or patterns the next task should reuse"]},
                       indent=1), "```", "",
            f"Then reply with one line only: `{t['id']} done` or `{t['id']} pause: <reason>`.", "",
            f"Slice brief (data model, design notes): `{brief_rel}`. Read only the parts this task needs.", ""]

    done = [x for x in data["tasks"] if x["status"] == "done" and x.get("handoff")]
    if done:
        out += ["## Earlier tasks in this slice", ""]
        out += [f"- {x['id']} {x['title']}: " + "; ".join(x["handoff"]) for x in done] + [""]
    own = {f for f in t["files"]}
    sigs = []
    for f in slice_files_so_far(ctx, data.get("base_commit")):
        if f.endswith(".py") and not any(fnmatch.fnmatch(f, g) for g in own):
            lines = python_signatures(os.path.join(ctx.app_path, f))
            if lines:
                sigs += [f"- `{f}`: " + "; ".join(lines)]
    if sigs:
        out += ["## Code already written in this slice (reuse, do not duplicate)", ""] + sigs[:40] + [""]
    patterns = matching_patterns(s)
    if patterns:
        out += ["## Similar approved slices from earlier projects (read the patch only if useful)", ""]
        out += [f"- {p['app']} {p['slice']} {p['title']}: {p.get('summary', '')} → `{p['path']}/changes.patch`"
                for p in patterns] + [""]

    out += [f"## Your task: {t['id']} {t['title']} ({t['owner']})", ""]
    if t["requirements"]:
        out += ["| Req | Requirement | Acceptance criteria |", "|---|---|---|"]
        out += [f"| {r} | {reg.get(r, {}).get('requirement', '(see register)')} | {reg.get(r, {}).get('acceptance', '')} |"
                for r in t["requirements"]] + [""]
    if t["rules"]:
        out += ["Rules (stubs exist; replace each `# TODO(rule <id>): ...` line with `# REQ: <id>` and the code, and "
                "write the matching test in place of its `skipTest`):", ""]
        out += [f"- {r['id']} `{rule_method(r)}` on {rule_event(r)}: {r['rule']}" for r in t["rules"]] + [""]
    if t["owner"] == "ui-ux":
        out += [f"Build the {t['title']} as the brief describes (layout, form behaviour, list view, print). "
                "Requirement IDs, if any, are context for the server logic owned by the Developer.", ""]
    out += ["Files you may change:", ""] + [f"- `{f}`" for f in t["files"]] + [""]
    return "\n".join(out)


def load_active(ctx):
    path = active_path(ctx)
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"tasks": {}}


def save_active(ctx, active):
    path = active_path(ctx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(active, f, indent=1)
    os.replace(tmp, path)


def next_task(ctx, slice_id, owner, as_json):
    s = ctx.slice(slice_id)
    path = tasks_path(ctx, s)
    data = load_yaml(path, None)
    if not data:
        sys.exit(f"ERROR: no tasks yet - run: tasks --slice {slice_id}")
    status = {t["id"]: t["status"] for t in data["tasks"]}
    mine = [t for t in data["tasks"] if not owner or t["owner"] == owner]
    t = next((x for x in mine if x["status"] in ("running", "needs-fix")), None)
    if not t:
        t = next((x for x in mine if x["status"] == "pending"
                  and all(status.get(d) == "done" for d in x["depends_on"])), None)
    if not t:
        left = [x for x in mine if x["status"] != "done"]
        result = {"task": None, "done": not left, "waiting_on": [x["id"] for x in left]}
        print(json.dumps(result) if as_json else ("ALL DONE" if not left else
              "WAITING: " + ", ".join(f"{x['id']} ({x['status']})" for x in left)))
        return
    if t["status"] == "pending":
        t["status"] = "running"
        t["started"] = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        t["start_commit"] = snapshot(ctx, f"{s['id']}/{t['id']}-start")
        save_yaml(path, data)
    os.makedirs(task_dir(ctx, s), exist_ok=True)
    prompt_path = os.path.join(task_dir(ctx, s), f"{t['id']}-prompt.md")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(task_prompt(ctx, s, data, t))
    active = load_active(ctx)
    key = f"{s['id']}/{t['id']}"
    allowed = [os.path.join(ctx.app_path, f) for f in t["files"]] + [os.path.join(task_dir(ctx, s), "*")]
    entry = active["tasks"].get(key) or {"writes": {}, "test_runs": []}
    entry.update({"slice": s["id"], "task": t["id"], "owner": t["owner"], "app_path": ctx.app_path,
                  "apps_root": os.path.join(ctx.bench, "apps"), "allowed": allowed,
                  "max_writes": t.get("max_writes", 30), "blocked_bash": BLOCKED_BASH})
    active["tasks"][key] = entry
    save_active(ctx, active)
    continue_agent, why = reuse_agent(data, t)
    if t["status"] == "running" and "agent_tasks" not in t:
        prev = done_before(data, t)
        t["agent_tasks"] = (prev.get("agent_tasks", 1) + 1) if continue_agent and prev else 1
        save_yaml(path, data)
    result = {"task": t["id"], "title": t["title"], "owner": t["owner"], "group": t["group"],
              "complexity": t["complexity"], "continue_agent": continue_agent, "agent_reason": why,
              "model": "sonnet" if t["complexity"] == "low" else "inherit",
              "prompt": prompt_path, "status": t["status"]}
    if as_json:
        print(json.dumps(result))
    else:
        print(f"TASK {t['id']} [{t['owner']}, {t['complexity']}] {t['title']} - prompt: {ctx.rel(prompt_path)}"
              f" - {'continue the same agent' if continue_agent else 'new agent'} ({why})")


AGENT_IDLE_MINUTES = 5   # the prompt cache expires after ~5 idle minutes: a resumed agent re-writes its whole context
AGENT_MAX_TASKS = 2      # each continued task re-reads everything before it; handoff notes carry what matters


def done_before(data, t):
    previous = [x for x in data["tasks"] if x["status"] == "done" and x["owner"] == t["owner"] and x["id"] != t["id"]]
    return max(previous, key=lambda x: x.get("finished", "")) if previous else None


def reuse_agent(data, t):
    """Continue the previous agent of this line only while that is cheaper than a fresh one."""
    prev = done_before(data, t)
    if not prev or prev["group"] != t["group"]:
        return False, "new group"
    if prev.get("agent_tasks", 1) >= AGENT_MAX_TASKS:
        return False, f"previous agent already did {prev.get('agent_tasks', 1)} tasks"
    try:
        idle = (datetime.datetime.now() - datetime.datetime.strptime(prev["finished"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
    except (KeyError, ValueError):
        return False, "previous finish time unknown"
    if idle > AGENT_IDLE_MINUTES:
        return False, f"previous agent idle {idle:.0f} min, its cache has expired"
    return True, "same group, previous agent still warm"


def checkpoint(ctx, slice_id, task_id, accept, as_json):
    s = ctx.slice(slice_id)
    path = tasks_path(ctx, s)
    data = load_yaml(path, None) or sys.exit(f"ERROR: no tasks for {slice_id}")
    t = next((x for x in data["tasks"] if x["id"] == task_id), None) or sys.exit(f"ERROR: no task {task_id}")
    if t["status"] not in ("running", "needs-fix"):
        sys.exit(f"ERROR: {task_id} is {t['status']}, not running")
    key = f"{s['id']}/{t['id']}"
    active = load_active(ctx)
    entry = active["tasks"].get(key, {})
    end = snapshot(ctx, f"{s['id']}/{t['id']}")
    changed = (git(ctx, "diff", "--name-only", t.get("start_commit"), end) or "").split() if t.get("start_commit") and end else []
    hard, soft = [], []

    report_path = os.path.join(task_dir(ctx, s), f"{t['id']}.json")
    try:
        report = json.load(open(report_path, encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
        hard.append(f"The agent did not write its report ({ctx.rel(report_path)}), so what it finished is unknown. "
                    "→ Fix: ask it to write the report.")

    # the other line (developer / UI-UX) runs at the same time: its files land in this diff too
    parallel = [x for x in data["tasks"] if x["id"] != t["id"] and x["owner"] != t["owner"]
                and (x["status"] in ("running", "needs-fix") or x.get("finished", "") >= t.get("started", ""))]
    outside = [f for f in changed if not f.startswith("docs/spec/") and not any(fnmatch.fnmatch(f, g) for g in t["files"])
               and not any(fnmatch.fnmatch(f, g) for x in parallel for g in x["files"])]
    if outside:
        hard.append("It changed files that belong to no running task: " + ", ".join(outside)
                    + ". → Undo them, or Continue if the change is intended.")
    runs = entry.get("test_runs") or []
    if t["owner"] == "developer":
        if not runs:
            hard.append("No tests were run for this task, so its code is unproven. → Fix: ask the agent to run them.")
        elif not runs[-1].get("ok"):
            hard.append(f"The last test run failed ({runs[-1].get('summary', '')}): {runs[-1].get('cmd', '')[:120]}. "
                        "→ Fix, or Continue to leave it for the Tester.")
    if entry.get("stuck"):
        hard.append(f"The agent got stuck: {entry['stuck']}. → Give it a hint (Fix with my comment), or Stop.")

    texts = {}
    for f in set(changed) | {f for f in t["files"] if "*" not in f}:
        full = os.path.join(ctx.app_path, f)
        if os.path.isfile(full) and not f.startswith("docs/"):
            texts[f] = open(full, encoding="utf-8", errors="replace").read()
    if t["owner"] == "developer":
        tagged = {rid for text in texts.values() for m in re.finditer(r"REQ:\s*([A-Z][A-Z0-9 ,.\-]*)", text)
                  for rid in REQ_ID.findall(m.group(1))}
        missing = [r for r in t["requirements"] if r not in tagged]
        if missing:
            soft.append(f"No code is marked as implementing {', '.join(missing)} (`# REQ:` tag missing): either not "
                        "done or not tagged. → Fix, or Continue if another task covers it.")
    left = [f for f, text in texts.items() if RULE_MARK in text]
    code_left = [f for f in left if not os.path.basename(f).startswith("test_")]
    tests_left = [f for f in left if f not in code_left]
    if code_left:
        soft.append("Business rules still left as placeholders (`TODO(rule`) in " + ", ".join(code_left) + ". → Fix.")
    if tests_left:
        soft.append("Tests for some rules are still placeholders (skipped) in " + ", ".join(tests_left)
                    + ". → Fix, or Continue to leave them for the Tester.")
    if report.get("status") == "pause":
        soft.append("The agent stopped before finishing: " + str(report.get("summary", "")))
    if report.get("open_questions"):
        soft += ["The agent asks: " + q for q in report["open_questions"]]
    requests = report.get("requests") or []
    for r in requests:
        if isinstance(r, dict) and r.get("kind") not in ("hooks",):
            soft.append(f"The agent needs a change outside its files ({r.get('kind', 'other')}): {r.get('file', '')}"
                        f" - {r.get('change', '')}. → Approve it (Continue) or say no (Fix with my comment).")

    mode = data.get("mode", "on-issue")
    issues = hard + (soft if mode != "slice" else [])
    group_end = not any(x["group"] == t["group"] and x["owner"] == t["owner"] and x["status"] != "done"
                        and x["id"] != t["id"] for x in data["tasks"])
    t["end_commit"] = end
    t["files_changed"] = changed
    t["tests"] = report.get("tests") or (runs[-1].get("summary") if runs else "")
    t["handoff"] = report.get("handoff") or t.get("handoff") or []
    t["review_items"] = report.get("review_items") or []
    if issues and not accept:
        t["status"] = "needs-fix"
        t["issues"] = issues
        decision = "pause"
    else:
        t["status"] = "done"
        t["finished"] = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        t["issues"] = issues if accept else []
        if accept and issues:
            t["accepted_with_issues"] = True
        active["tasks"].pop(key, None)
        decision = "pause" if mode == "every-task" or mode == "per-group" and group_end else "continue"
    save_yaml(path, data)
    save_active(ctx, active)
    own = [f for f in changed if not f.startswith("docs/spec/") and (f in outside or any(fnmatch.fnmatch(f, g) for g in t["files"]))]
    restore = ""
    if t.get("start_commit") and end:
        status_lines = (git(ctx, "diff", "--name-status", t["start_commit"], end) or "").splitlines()
        code = [l.split("\t") for l in status_lines if l and l.split("\t")[-1] in own]  # not the other line's files
        back = [p[-1] for p in code if p[0] != "A"]
        added = [p[-1] for p in code if p[0] == "A"]
        restore = " && ".join(x for x in (
            f"git -C apps/{ctx.app} checkout {t['start_commit'][:12]} -- " + " ".join(back) if back else "",
            "rm " + " ".join(f"apps/{ctx.app}/{f}" for f in added) if added else "") if x)
    message = checkpoint_message(s, data, t, report, issues, requests, own, decision, mode)
    result = {"task": t["id"], "status": t["status"], "decision": decision, "issues": issues,
              "requests": requests, "summary": report.get("summary", ""), "tests": t["tests"],
              "files": changed, "checkpoint": end, "restore": restore, "message": message}
    if as_json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
    else:
        print(message)
        if restore:
            print("\nUndo command: " + restore)


OWNER_NAME = {"developer": "Developer", "ui-ux": "UI/UX"}


def checkpoint_message(s, data, t, report, issues, requests, own, decision, mode):
    """What the user reads at a checkpoint: what this task finished, what is carried forward, what runs next."""
    done = sum(x["status"] == "done" for x in data["tasks"])
    state = {"done": "Done ✓", "needs-fix": "Needs your decision"}.get(t["status"], t["status"])
    lines = [f"{s['id']} {t['id']} · {t['title']} ({OWNER_NAME.get(t['owner'], t['owner'])})",
             f"Status: {state} | Slice {s['id']}: {done} of {len(data['tasks'])} tasks done", ""]
    if report.get("summary"):
        lines.append(f"What was done: {report['summary']}")
    lines.append(f"Tests: {t.get('tests') or 'none run'}")
    reqs = report.get("requirements") or t["requirements"]
    if reqs:
        lines.append("Requirements covered: " + ", ".join(reqs))
    if own:
        lines.append(f"Files changed ({len(own)}): " + ", ".join(own[:8]) + (f" and {len(own) - 8} more" if len(own) > 8 else ""))
    if issues:
        lines += ["", "Needs your attention:"] + [f"  {i}. {x}" for i, x in enumerate(issues, 1)]
        if t["status"] == "done":
            lines.append("  (accepted as is: these stay listed on the slice review checklist)")
    carried = [f"  • For the next tasks: {h}" for h in report.get("handoff") or []]
    carried += [f"  • To apply once no task is running: {r.get('file', '')} - {r.get('change', '')}"
                for r in requests if isinstance(r, dict) and r.get("kind") == "hooks"]
    carried += [f"  • For your slice review checklist: {r}" for r in report.get("review_items") or []]
    if carried:
        lines += ["", "Carried forward:"] + carried
    pending = sorted((x for x in data["tasks"] if x["status"] != "done" and x["id"] != t["id"]),
                     key=lambda x: x["status"] == "pending")  # running ones first
    status = {x["id"]: x["status"] for x in data["tasks"]}
    nxt = []
    for x in pending[:4]:
        waits = [d for d in x["depends_on"] if status.get(d) != "done"]
        tag = "running now" if x["status"] in ("running", "needs-fix") else \
            ("waits for " + ", ".join(waits) if waits else "")
        nxt.append(f"{x['id']} {x['title']} ({OWNER_NAME.get(x['owner'], x['owner'])}{', ' + tag if tag else ''})")
    lines += ["", "Next: " + (" · ".join(nxt) + (f" · and {len(pending) - 4} more" if len(pending) > 4 else "")
                              if nxt else "all tasks done; the Tester and review come next")]
    if decision == "pause":
        lines.append("Paused: " + ("waiting for your decision on the points above." if t["status"] != "done"
                                   else f"pause mode is {mode}. Say continue to go on."))
    return "\n".join(lines)


# ---------------------------------------------------------------- company knowledge base

def review_rounds(ctx, s):
    current = review_path(ctx, s)
    folder = os.path.dirname(current)
    stem = os.path.basename(current)[:-3]
    files = [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.startswith(stem + "-r")] if os.path.isdir(folder) else []
    return files + ([current] if os.path.exists(current) else [])


def lessons(ctx, slice_id):
    """Append every rejected review item of a slice to the company review log, and print the new ones
    for the orchestrator to turn into short rules in lessons.md."""
    s = ctx.slice(slice_id)
    log = os.path.join(kb_dir(), "review-log.jsonl")
    seen = set()
    if os.path.exists(log):
        seen = {json.loads(l).get("key") for l in open(log, encoding="utf-8") if l.strip()}
    new = []
    for path in review_rounds(ctx, s):
        for item_id, item in parse_review(path).items():
            if item_state(item) != "rejected":
                continue
            key = sha(f"{ctx.app}|{s['id']}|{item_id}|{item['comment']}")[:16]
            if key in seen:
                continue
            seen.add(key)
            new.append({"key": key, "date": f"{datetime.date.today()}", "app": ctx.app, "slice": s["id"],
                        "title": s["title"], "item": item_id, "what": short(item_title(item), 300),
                        "comment": item["comment"]})
    os.makedirs(kb_dir(), exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        for e in new:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"{len(new)} new rejected item(s) logged to {log}")
    for e in new:
        print(f"- [{e['item']}] {e['what']}\n    developer said: {e['comment']}")
    if new:
        print(f"\nTurn the ones that generalise into short rules in {os.path.join(kb_dir(), 'lessons.md')} "
              "(one line each, imperative, with a correct example). Skip project-specific ones.")


def archive(ctx, slice_id, base_commit):
    """Store an approved slice in the company pattern library for later projects."""
    s = ctx.slice(slice_id)
    if s.get("status") != "approved":
        print(f"WARN   {slice_id} is not approved in design.yaml - archiving anyway")
    data = load_yaml(tasks_path(ctx, s), {})
    base = base_commit or data.get("base_commit")
    dest = os.path.join(kb_dir(), "patterns", f"{ctx.app}-{s['id']}-{slug(s['title'])}")
    os.makedirs(dest, exist_ok=True)
    brief_file = os.path.join(ctx.spec, "slices", f"{s['id']}-{slug(s['title'])}.md")
    if os.path.exists(brief_file):
        shutil.copy(brief_file, os.path.join(dest, "brief.md"))
    save_yaml(os.path.join(dest, "slice.yaml"), s)
    patch = git(ctx, "diff", base, "--", ".", ":(exclude)docs") if base else None
    if patch:
        with open(os.path.join(dest, "changes.patch"), "w", encoding="utf-8") as f:
            f.write(patch[:400_000])
    index_path = os.path.join(kb_dir(), "patterns", "index.json")
    index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else []
    doctypes = sorted({d["name"] for d in s.get("doctypes") or []} | {g["doctype"] for g in s.get("custom_fields") or []}
                      | {w["doctype"] for w in s.get("workflows") or []} | set(s.get("standard_permissions") or {}))
    entry = {"app": ctx.app, "slice": s["id"], "title": s["title"], "summary": s.get("summary", ""),
             "doctypes": doctypes, "path": dest, "date": f"{datetime.date.today()}"}
    index = [e for e in index if e.get("path") != dest] + [entry]
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1, ensure_ascii=False)
    print(f"Archived {s['id']} {s['title']} to {dest}" + ("" if patch else " (no patch: pass --base-commit)"))


def metrics(ctx, slice_id):
    path = os.path.join(ctx.bench, ".claude", "frappe-agent-metrics.jsonl")
    if not os.path.exists(path):
        sys.exit("No agent metrics yet (the SubagentStop hook writes .claude/frappe-agent-metrics.jsonl)")
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    if slice_id:
        rows = [r for r in rows if any(t.startswith(slice_id + "/") for t in r.get("tasks") or [])]
    print("| When | Agent | Task | Minutes | Tool calls | Writes | Test runs | Reads of apps/erpnext |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r.get('ended', '')[:16]} | {r.get('agent_type') or '-'} | {', '.join(r.get('tasks') or []) or '-'} | "
              f"{r.get('minutes', '')} | {r.get('tool_calls', '')} | {r.get('writes', '')} | {r.get('test_runs', '')} | "
              f"{r.get('erpnext_reads', '')} |")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Feature-slice tooling driven by docs/spec/design.yaml")
    ap.add_argument("command", choices=["validate", "generate", "brief", "routes", "review", "review-status",
                                        "flows", "plan", "progress", "tasks", "next", "checkpoint", "lessons",
                                        "archive", "metrics"])
    ap.add_argument("--app", required=True, help="app package name, e.g. geofresh")
    ap.add_argument("--bench", default=".", help="bench folder (default: current folder)")
    ap.add_argument("--design", help="design file (default: apps/<app>/docs/spec/design.yaml)")
    ap.add_argument("--slice", help="slice id, e.g. S02")
    ap.add_argument("--force", action="store_true", help="generate: overwrite DocType JSON edited by hand")
    ap.add_argument("--base-url", default="http://dev.localhost:8000", help="review: site URL for links")
    ap.add_argument("--tests", help="review: test summary line, e.g. '42 passed, 0 failed'")
    ap.add_argument("--browser", help="review: browser_check.mjs JSON result file")
    ap.add_argument("--fix-notes", help="review / flows / plan: JSON {item_id: what was changed} for the next round")
    ap.add_argument("--base-commit", help="review: commit the slice started from (for the code-changes item)")
    ap.add_argument("--gate", choices=sorted(GATES), help="review-status: read the flows or plan checklist instead of a slice's")
    ap.add_argument("--json", action="store_true", help="review-status / next / checkpoint: JSON output")
    ap.add_argument("--mode", choices=MODES, default="on-issue", help="tasks: when the build pauses for the user")
    ap.add_argument("--owner", choices=["developer", "ui-ux"], help="next: only this agent's tasks")
    ap.add_argument("--task", help="checkpoint: task id, e.g. T03")
    ap.add_argument("--accept", action="store_true", help="checkpoint: mark done despite issues (the user decided)")
    args = ap.parse_args()
    ctx = Ctx(args)

    if args.command in ("validate", "generate", "brief", "routes", "review") or not args.gate and args.command == "review-status":
        ctx.require_design()
    if args.command in ("tasks", "next", "checkpoint", "lessons", "archive"):
        ctx.require_design()
    needs_slice = args.command in ("generate", "brief", "routes", "review", "tasks", "next", "checkpoint", "lessons",
                                   "archive") or args.command == "review-status" and not args.gate
    if needs_slice and not args.slice:
        sys.exit("ERROR: --slice is required" + (" (or --gate flows|plan)" if args.command == "review-status" else ""))
    if args.command == "flows":
        flows_review(ctx, args.fix_notes)
    if args.command == "plan":
        plan_review(ctx, args.fix_notes)
    if args.command == "progress":
        progress(ctx)
    if args.command == "validate":
        reg = register_rows(ctx)
        errors, warnings = validate(ctx, set(reg) if reg else None)
        for w in warnings:
            print("WARN   " + w)
        for e in errors:
            print("ERROR  " + e)
        print("OK - design.yaml is valid" if not errors else f"{len(errors)} error(s)")
        sys.exit(1 if errors else 0)
    if args.command == "generate":
        errors, _ = validate(ctx)
        if errors:
            print("\n".join("ERROR  " + e for e in errors))
            sys.exit("Fix design.yaml first (run validate)")
        generate(ctx, args.slice, args.force)
        for w in ctx.written:
            print("WROTE  " + w)
        for w in ctx.skipped:
            print("SKIP   " + w)
        for w in ctx.warnings:
            print("TODO   " + w)
        print(f"{len(ctx.written)} file(s) written. Next: bench --site <site> migrate")
        sys.exit(1 if ctx.skipped else 0)
    if args.command == "brief":
        brief(ctx, args.slice)
    if args.command == "routes":
        print("\n".join(routes(ctx, args.slice)))
    if args.command == "review":
        review(ctx, args.slice, args.base_url, args.tests, args.browser, args.fix_notes, args.base_commit)
    if args.command == "review-status":
        review_status(ctx, args.slice, args.gate, args.json)
    if args.command == "tasks":
        make_tasks(ctx, args.slice, args.mode, args.force)
    if args.command == "next":
        next_task(ctx, args.slice, args.owner, args.json)
    if args.command == "checkpoint":
        if not args.task:
            sys.exit("ERROR: --task is required")
        checkpoint(ctx, args.slice, args.task, args.accept, args.json)
    if args.command == "lessons":
        lessons(ctx, args.slice)
    if args.command == "archive":
        archive(ctx, args.slice, args.base_commit)
    if args.command == "metrics":
        metrics(ctx, args.slice)


if __name__ == "__main__":
    main()
