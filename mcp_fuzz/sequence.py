"""Groups a server's own tools into create/read/delete resource-lifecycle
sets by name, and extracts a real resource id from a real response — the
pure, no-I/O heuristics `engine.py`'s sequential check drives.

Every other check in this package tests one tool call in isolation, always
with synthetic, schema-derived arguments. That misses a real and common bug
class: does calling `get_item(id=<the id create_item actually just
returned>)` behave correctly, and — the more interesting case — does
`get_item` still report success on an id that `delete_item` just removed
("stale read")? Testing that needs a *real* id chained from one call's
response into the next, which nothing else in this package attempts.

Deliberately conservative: every heuristic below either finds an
unambiguous match or gives up and reports why, rather than guessing. A wrong
guess here would call a real tool with a fabricated argument shape, not just
mis-score a report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_CREATE_VERBS = ("create", "add", "insert", "new")
_READ_VERBS = ("get", "read", "fetch", "retrieve", "describe", "show")
_DELETE_VERBS = ("delete", "remove", "destroy")

_ID_KEY_CANDIDATES_SUFFIX = ("_id", "id", "uuid", "key")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass
class ResourceGroup:
    resource: str
    create_tool: str
    read_tool: str | None
    delete_tool: str | None


def _split_verb(tool_name: str, verbs: tuple[str, ...]) -> str | None:
    """Returns the resource part of `verb_resource` / `verb-resource` if
    `tool_name` starts with one of `verbs` followed by a separator, else
    None. Case-sensitive and separator-strict on purpose — a tool named
    e.g. `creative_writing_helper` must never match `create`."""
    lowered = tool_name.lower()
    for verb in verbs:
        for sep in ("_", "-"):
            prefix = verb + sep
            if lowered.startswith(prefix) and len(tool_name) > len(prefix):
                return tool_name[len(prefix):]
    return None


def group_resource_tools(tool_names: list[str]) -> list[ResourceGroup]:
    """Groups tool names into create/read/delete sets sharing the same
    resource suffix (`create_item` / `get_item` / `delete_item` -> resource
    `item`). Only returns a group when exactly one create tool and at least
    one of (exactly one read tool, exactly one delete tool) share that
    resource name — an ambiguous match (two create tools for the same
    resource, e.g.) is dropped entirely rather than guessed at, same
    discipline as this project's other name-based heuristics."""
    creates: dict[str, list[str]] = {}
    reads: dict[str, list[str]] = {}
    deletes: dict[str, list[str]] = {}
    for name in tool_names:
        resource = _split_verb(name, _CREATE_VERBS)
        if resource:
            creates.setdefault(resource, []).append(name)
            continue
        resource = _split_verb(name, _READ_VERBS)
        if resource:
            reads.setdefault(resource, []).append(name)
            continue
        resource = _split_verb(name, _DELETE_VERBS)
        if resource:
            deletes.setdefault(resource, []).append(name)

    groups = []
    for resource, create_names in creates.items():
        if len(create_names) != 1:
            continue  # ambiguous — more than one create_ tool for this resource
        read_names = reads.get(resource, [])
        delete_names = deletes.get(resource, [])
        read_tool = read_names[0] if len(read_names) == 1 else None
        delete_tool = delete_names[0] if len(delete_names) == 1 else None
        if read_tool is None and delete_tool is None:
            continue  # nothing to chain the created resource into
        groups.append(ResourceGroup(
            resource=resource, create_tool=create_names[0], read_tool=read_tool, delete_tool=delete_tool,
        ))
    return groups


def extract_id(response_text: str, resource: str) -> str | None:
    """Pulls a plausible resource id out of a real tool response, trying
    the structured case first and falling back to a bare UUID scan — never
    a bare numeric/short-string guess, which would be far more likely to
    grab an unrelated field (a count, a name) than a real identifier."""
    try:
        parsed = json.loads(response_text)
    except (json.JSONDecodeError, ValueError):
        parsed = None

    candidates: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        candidates.append(parsed)
    elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        candidates.append(parsed[0])

    for obj in candidates:
        for suffix in (f"{resource}_id", "id", "uuid", "key"):
            if suffix in obj and isinstance(obj[suffix], (str, int)):
                return str(obj[suffix])

    match = _UUID_RE.search(response_text)
    if match:
        return match.group(0)
    return None


def find_id_property(schema: dict[str, Any] | None, resource: str) -> str | None:
    """Which of a dependent tool's schema properties should receive the
    real extracted id — an exact-name match only (`id`, `{resource}_id`),
    or, failing that, the tool's one and only required string property.
    Anything less specific is left unresolved rather than guessed."""
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return None

    for candidate in (f"{resource}_id", "id"):
        if candidate in properties:
            return candidate

    required = schema.get("required", [])
    if not isinstance(required, list):
        required = []
    required_string_props = [
        name for name in required
        if isinstance(properties.get(name), dict) and properties[name].get("type") == "string"
    ]
    if len(required_string_props) == 1:
        return required_string_props[0]
    return None
