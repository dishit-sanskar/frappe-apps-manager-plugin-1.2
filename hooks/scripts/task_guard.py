"""
Hook: keep an agent inside its current task while /frappe-build-from-docs runs a slice task.

Active only while <bench>/.claude/frappe-active-task.json lists running tasks
(written by `slice_tool.py next`, cleared by `slice_tool.py checkpoint`). Otherwise silent.

PreToolUse  Write|Edit|MultiEdit|NotebookEdit
    - blocks writes inside apps/ that are not in the task's files (other apps always blocked)
    - blocks writes past the task's write budget
PreToolUse  Bash
    - blocks migrate / install / build / drop-site and git commands that move the tree
    - blocks shell file writes into apps/ (redirects, heredocs, tee, sed -i, cp/mv, inline python):
      they would skip the file check above and the lint hook
PreToolUse  Read|Grep|Glob|Bash
    - refuses the first read of ERPNext / Frappe DocType JSON per task, pointing at the MCP
      `get_doctype_schema`; a repeat is allowed (for sessions without the MCP server)
PostToolUse Bash
    - tells the agent when a shell read (cat, sed -n, head ...) printed more than 200 lines
PostToolUse Bash
    - records each `run-tests` result for the checkpoint; after the same tests fail 3 times
      in a row, tells the agent to stop and return `pause`

A block exits 2 with the reason on stderr, which the agent reads.
"""

import fnmatch
import json
import os
import re
import sys
import time

AGENT_OWNER = {"developer": "developer", "ui-ux": "ui-ux"}


def find_active(cwd):
    path = os.path.abspath(cwd or ".")
    for _ in range(6):
        candidate = os.path.join(path, ".claude", "frappe-active-task.json")
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def save(path, active):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(active, f, indent=1)
    os.replace(tmp, path)


def my_tasks(active, payload):
    """Tasks of the calling agent: by agent type when the hook input has it, else all running tasks."""
    agent = str(payload.get("agent_type") or "")
    owner = next((o for k, o in AGENT_OWNER.items() if k in agent), None)
    tasks = active.get("tasks") or {}
    if owner:
        return {k: v for k, v in tasks.items() if v.get("owner") == owner}
    return tasks


def block(reason):
    print(reason, file=sys.stderr)
    sys.exit(2)


def check_write(payload, active, path, tasks):
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return
    target = os.path.abspath(os.path.join(payload.get("cwd") or ".", target))
    any_task = next(iter(tasks.values()))
    if not target.startswith(any_task["apps_root"] + os.sep):
        return  # outside the bench apps: scratch files etc.
    owners = [t for t in tasks.values() if any(fnmatch.fnmatch(target, g) for g in t["allowed"])]
    if not owners:
        names = ", ".join(f"{t['slice']}/{t['task']}" for t in tasks.values())
        block(f"Blocked: {target} is not one of the files of task {names}. "
              "If this change is needed, add it to `requests` in your checkpoint file instead.")
    for t in owners:
        t["writes"][target] = t["writes"].get(target, 0) + 1
        total = sum(t["writes"].values())
        if total > t.get("max_writes", 30):
            save(path, active)
            block(f"Task {t['task']} write budget ({t['max_writes']}) reached. Stop here: write your checkpoint "
                  "file with status `pause` and what is left, then reply with one line.")
    save(path, active)


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.S | re.M)
REDIRECT = re.compile(r"(?<![0-9&<>])>>?\s*(['\"]?)([^\s'\";&|()<>]+)\1|\btee\s+(?:-a\s+)?(['\"]?)([^\s'\";&|()]+)\3")
SHELL_EDIT = re.compile(r"\bsed\s+(-\w*\s+)*-i|\bperl\s+-\w*i|\b(cp|mv|install|truncate|touch)\b\s")
PY_WRITE = re.compile(r"\.write(_text|_bytes)?\(|open\([^)]*['\"][wa]b?\+?['\"]|json\.dump\(|shutil\.(copy|move)")


def shell_writes_app(cmd, cwd, apps_root):
    """True when a shell command writes into apps/: redirects, tee, sed -i, cp/mv, inline python."""
    if re.search(r"\bpython3?\b", cmd) and PY_WRITE.search(cmd):
        return True  # inline python that writes files: the target can't be told, so always via Write/Edit
    plain = HEREDOC.sub("<<HEREDOC\n", cmd)  # heredoc bodies hold HTML/JS whose `>` is not a redirect
    base = cwd
    m = re.search(r"\bcd\s+(['\"]?)([^\s'\";&|]+)\1", plain)
    if m:
        base = os.path.join(cwd, os.path.expanduser(m.group(2)))
    for m in REDIRECT.finditer(plain):
        target = m.group(2) or m.group(4)
        if target.startswith(("&", "/dev/")):
            continue
        if os.path.abspath(os.path.join(base, target)).startswith(apps_root + os.sep):
            return True
    if SHELL_EDIT.search(plain):
        inside = os.path.abspath(base).startswith(apps_root + os.sep) or (apps_root + os.sep) in plain \
            or re.search(r"(^|\s)apps/", plain)
        return bool(inside)
    return False


def check_bash(payload, tasks):
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    for t in tasks.values():
        if re.search(t.get("blocked_bash") or "$^", cmd):
            block(f"Blocked during task {t['task']}: `{cmd[:100]}`. The orchestrator runs migrate, install, build "
                  "and git; ask for it under `requests` in your checkpoint file.")
    any_task = next(iter(tasks.values()))
    if shell_writes_app(cmd, payload.get("cwd") or ".", any_task["apps_root"]):
        block("Blocked: change files with the Write / Edit tools, not shell redirects, heredocs, sed -i, cp/mv or "
              "inline python. Only those tools are checked against your task's files and linted. Use Bash for "
              "run-tests and for what the frappe MCP tools cannot answer.")


BIG_READ = re.compile(r"(^|[;&|]\s*|\bthen\s+)(cat|sed\s+-n|head|tail|less|awk|nl)\b")
BIG_READ_LINES = 200


def framework_json(payload):
    """The standard-DocType JSON this call reads from apps/erpnext or apps/frappe, if any."""
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or "."
    if tool == "Bash":
        cmd = ti.get("command") or ""
        m = re.search(r"\bcd\s+(['\"]?)([^\s'\";&|]+)\1", cmd)
        where = os.path.join(cwd, m.group(2)) if m else cwd
        inside = re.search(r"/apps/(erpnext|frappe)(/|$)", os.path.abspath(where) + "/")
        if (inside and re.search(r"\.json\b", cmd)) or re.search(r"(apps/|\b)(erpnext|frappe)/(erpnext|frappe)/\S*\.json", cmd):
            return cmd[:120]
        return None
    target = " ".join(str(ti.get(k) or "") for k in ("file_path", "path", "glob", "pattern"))
    if re.search(r"/apps/(erpnext|frappe)/", os.path.join(cwd, ti.get("file_path") or ti.get("path") or "")) \
            and (".json" in target or ti.get("type") == "json"):
        return target[:120]
    return None


def nudge_mcp(payload, active, path, tasks):
    """Standard DocType fields come from the MCP `get_doctype_schema`: refuse the first JSON read per task.
    A repeat is allowed, so an agent without the MCP server is never stuck."""
    what = framework_json(payload)
    if not what:
        return
    t = next(iter(tasks.values()))
    if t.get("mcp_nudged"):
        return
    t["mcp_nudged"] = True
    save(path, active)
    block(f"Read standard DocType fields with the frappe MCP tool `get_doctype_schema` (or `search_doctypes`), "
          f"not the ERPNext/Frappe JSON files: it is much shorter. ({what}) If the frappe MCP tools are not "
          "available in this session, run the same call again and it will be allowed.")


def warn_big_read(payload):
    """After a shell read printed a whole file: tell the agent to read in ranges next time."""
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if "run-tests" in cmd or not BIG_READ.search(cmd):
        return
    lines = response_text(payload.get("tool_response")).count("\n")
    if lines > BIG_READ_LINES:
        block(f"That command printed {lines} lines into your context. Read files with Read (offset/limit) or "
              "search with Grep, and print only the lines you need. For ERPNext fields use the MCP "
              "`get_doctype_schema`.")


def response_text(resp):
    if isinstance(resp, dict):
        return "\n".join(str(resp.get(k) or "") for k in ("stdout", "stderr", "output"))
    return str(resp or "")


def record_tests(payload, active, path, tasks):
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if "run-tests" not in cmd:
        return
    text = response_text(payload.get("tool_response"))
    ran = re.findall(r"Ran (\d+) tests?", text)
    ok = bool(ran) and bool(re.search(r"^OK\b", text, re.M)) and not re.search(r"^FAILED\b", text, re.M)
    summary = (re.findall(r"^(OK.*|FAILED.*)$", text, re.M) or ["no result line"])[-1]
    run = {"cmd": cmd[:300], "ok": ok, "ran": int(ran[-1]) if ran else 0, "summary": summary, "at": time.time()}
    stuck = None
    for t in tasks.values():
        t["test_runs"] = (t.get("test_runs") or []) + [run]
        if ok:
            t.pop("stuck", None)
        last = t["test_runs"][-3:]
        if len(last) == 3 and all(not r["ok"] and r["cmd"] == cmd[:300] for r in last):
            t["stuck"] = f"same tests failed 3 times in a row: {cmd[:120]}"
            stuck = t
    save(path, active)
    if stuck:
        block(f"Task {stuck['task']}: these tests have failed 3 times in a row. Stop fixing: write your checkpoint "
              "file with status `pause`, the failing test and your best guess at the cause, then reply with one line.")


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = find_active(payload.get("cwd"))
    if not path:
        return 0
    try:
        active = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    tasks = my_tasks(active, payload)
    if not tasks:
        return 0
    tool = payload.get("tool_name") or ""
    event = payload.get("hook_event_name") or ""
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit") and event == "PreToolUse":
        check_write(payload, active, path, tasks)
    elif tool in ("Read", "Grep", "Glob") and event == "PreToolUse":
        nudge_mcp(payload, active, path, tasks)
    elif tool == "Bash" and event == "PreToolUse":
        check_bash(payload, tasks)
        nudge_mcp(payload, active, path, tasks)
    elif tool == "Bash" and event == "PostToolUse":
        record_tests(payload, active, path, tasks)
        warn_big_read(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
