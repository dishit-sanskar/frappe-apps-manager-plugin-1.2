"""
PreToolUse hook (Bash), always on: block commands that destroy a site, its data or uncommitted work.

Applies everywhere, inside or outside /frappe-build-from-docs:
- a subagent is always refused (exit 2, reason on stderr)
- the main session gets a permission prompt naming the risk, so commands the user asked for
  (/frappe-db-reset, /frappe-new-site ...) still work but never run unnoticed

Blocked:
- bench: drop-site, reinstall, restore / partial-restore, trim-database / trim-tables,
  uninstall-app / remove-app, new-site --force (overwrites a site)
- SQL that drops or empties data (DROP DATABASE/TABLE, TRUNCATE, DELETE FROM) sent to
  mysql / mariadb, bench mariadb / console / execute, or frappe.db.sql / frappe.db.delete / frappe.db.truncate
- rm -r on sites/, apps/, env/, config/ or a whole bench
- git reset --hard, git clean -f, git push --force
"""

import json
import re
import sys

RULES = [
    (r"\bbench\b.*\b(drop-site|reinstall|restore|partial-restore|trim-database|trim-tables|uninstall-app|remove-app)\b",
     "removes or overwrites a site, an app or its data"),
    (r"\bbench\b.*\bnew-site\b.*--force\b", "overwrites an existing site"),
    (r"(?i)\b(mysql|mariadb|bench\b.*\b(mariadb|console|execute|db-console))\b.*"
     r"\b(drop\s+(database|schema|table)|truncate\b|delete\s+from)\b", "drops or deletes data with SQL"),
    (r"(?i)frappe\.db\.(delete|truncate)\b|frappe\.db\.sql\(.*\b(drop|truncate|delete\s+from)\b",
     "deletes data through frappe.db"),
    (r"\brm\s+(-\w*\s+)*-\w*[rR]\w*\b.*?(^|[\s/'\"])(sites|apps|env|config)(/|\s|$|['\"])",
     "recursively deletes bench folders"),
    (r"\brm\s+(-\w*\s+)*-\w*[rR]\w*\s+(\.|\./?|~|/|\*)(\s|$)", "recursively deletes a whole folder tree"),
    (r"\bgit\b.*\breset\b.*--hard\b", "discards uncommitted work"),
    (r"\bgit\b.*\bclean\b.*\s-\w*f", "deletes untracked files"),
    (r"\bgit\b.*\bpush\b.*(--force\b|--force-with-lease\b|\s-f\b)", "rewrites remote history"),
]


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    for pattern, why in RULES:
        if not re.search(pattern, cmd, re.S):
            continue
        if payload.get("agent_id") or payload.get("agent_type"):
            print(f"Blocked: this command {why}. Agents never run it; report it as needed and the user "
                  "decides.", file=sys.stderr)
            return 2
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": f"Destructive: this command {why}. Approve only if you asked for it."}}))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
