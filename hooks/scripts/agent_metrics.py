"""
SubagentStop hook: log how long each agent ran and what it spent its time on.

Appends one line per agent run to <bench>/.claude/frappe-agent-metrics.jsonl (only inside a
Frappe bench): minutes, tool calls by tool, file writes, `run-tests` runs, and reads of
apps/erpnext or apps/frappe (a sign the agent is searching framework source instead of using
the frappe MCP tools). `slice_tool.py metrics` prints them as a table.
"""

import datetime
import json
import os
import sys


def find_bench(cwd):
    path = os.path.abspath(cwd or ".")
    for _ in range(6):
        if os.path.exists(os.path.join(path, "sites", "common_site_config.json")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return None


def parse_time(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    bench = find_bench(payload.get("cwd"))
    transcript = payload.get("agent_transcript_path") or payload.get("transcript_path")
    if not bench or not transcript or not os.path.exists(transcript):
        return 0
    times, tools, writes, tests, framework_reads = [], {}, 0, 0, 0
    for line in open(transcript, encoding="utf-8", errors="replace"):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        t = parse_time(entry.get("timestamp"))
        if t:
            times.append(t)
        content = (entry.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "?")
            tools[name] = tools.get(name, 0) + 1
            args = json.dumps(block.get("input") or {})
            if name in ("Write", "Edit", "MultiEdit"):
                writes += 1
            if "run-tests" in args:
                tests += 1
            if name in ("Read", "Grep", "Glob", "Bash") and ("apps/erpnext" in args or "apps/frappe/" in args):
                framework_reads += 1
    active_file = os.path.join(bench, ".claude", "frappe-active-task.json")
    try:
        active = list(json.load(open(active_file, encoding="utf-8")).get("tasks") or {})
    except (OSError, ValueError):
        active = []
    row = {
        "ended": datetime.datetime.now().isoformat(timespec="seconds"),
        "agent_type": payload.get("agent_type"), "agent_id": payload.get("agent_id"), "tasks": active,
        "minutes": round((max(times) - min(times)).total_seconds() / 60, 1) if len(times) > 1 else None,
        "tool_calls": sum(tools.values()), "by_tool": tools, "writes": writes, "test_runs": tests,
        "erpnext_reads": framework_reads,
        "whole_session": not payload.get("agent_transcript_path"),
    }
    os.makedirs(os.path.join(bench, ".claude"), exist_ok=True)
    with open(os.path.join(bench, ".claude", "frappe-agent-metrics.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
