"""
PreToolUse hook for the Frappe site MCP server (`frappe`, /api/v2/mcp).

- discovery and read tools: allowed without a prompt
- delete_document: denied
- any method with "delete" in its name (call_method / call_document_method): denied
- writes to schema documents (DocType, Custom Field, Property Setter, Workflow, Server Script, ...)
  through create/update_document, frappe.client.* via call_method, or call_document_method:
  denied - schema changes go through design.yaml and `slice_tool.py generate`
- other writes (create_document, update_document, call_method, call_document_method): ask
"""

import json
import re
import sys

READ = {"search_doctypes", "get_doctype_schema", "search_methods", "get_method_schema", "list_document_methods",
        "list_documents", "get_document", "count_documents"}
SCHEMA = {"DocType", "DocField", "Custom Field", "Property Setter", "Workflow", "Workflow State",
          "Workflow Action Master", "Client Script", "Server Script", "Role", "Custom DocPerm", "Module Def",
          "Report", "Print Format"}
WRITE = {"create_document", "update_document", "call_method", "call_document_method"}


def decide(decision, reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))
    return 0


CLIENT_WRITES = re.compile(r"^frappe\.client\.(insert|insert_many|save|set_value|submit|cancel|rename_doc|bulk_update)$")


def touches_schema(args):
    """True if the call names a schema DocType anywhere in its arguments."""
    text = json.dumps(args)
    return any(re.search(rf'"(doctype|doc_type|dt|document_type)"\s*:\s*"{re.escape(d)}"', text) for d in SCHEMA)


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = (payload.get("tool_name") or "").rsplit("__", 1)[-1]
    args = payload.get("tool_input") or {}
    doctype = args.get("doctype") or args.get("doc_type") or ""
    if tool in READ:
        return decide("allow", "read-only Frappe MCP tool")
    if tool == "delete_document":
        return decide("deny", "Deleting documents through MCP is disabled in this plugin.")
    method = str(args.get("method") or args.get("method_name") or args.get("name") or "")
    if tool in ("call_method", "call_document_method") and "delete" in method.lower():
        return decide("deny", f"Deleting through MCP ({method}) is disabled in this plugin.")
    schema_write = (tool in ("create_document", "update_document") and (doctype in SCHEMA or touches_schema(args))) \
        or (tool == "call_method" and CLIENT_WRITES.match(method) and touches_schema(args)) \
        or (tool == "call_document_method" and (doctype in SCHEMA or touches_schema(args)))
    if schema_write:
        return decide("deny", f"{doctype or 'This call'} changes schema: change docs/spec/design.yaml and run "
                              "slice_tool.py generate, so the change is versioned and reviewed.")
    if tool in WRITE:
        return decide("ask", f"Frappe MCP {tool} changes data on the site")
    return 0


if __name__ == "__main__":
    sys.exit(main())
