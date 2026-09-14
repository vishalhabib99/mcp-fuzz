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

A second, related gap: even a full create/read/delete chain only ever
exercises one resource type in isolation. A common real bug lives between
two resources instead — `create_task(project_id=...)` referencing a
`project`, then `delete_project` running with no idea `task` ever existed.
Whether the task should still be readable afterward is a real design
choice (cascade vs. orphan-allowed), not something this module judges —
`find_parent_child_pairs` below only detects the relationship so the engine
can chain a real workflow across it and report what actually happens as a
neutral observation, not a pass/fail verdict.

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


@dataclass
class ParentChildPair:
    parent: ResourceGroup
    child: ResourceGroup
    parent_link_property: str


def find_parent_link_property(child_create_schema: dict[str, Any] | None, parent_resource: str) -> str | None:
    """Whether a child resource's create call takes the parent's id as one
    of its own arguments — exact-name match only (`{parent_resource}_id`),
    deliberately with **no** fallback to a sole-required-string property the
    way `find_id_property` has for a get/delete call. A create call commonly
    has several required fields at once (a title, a name, *and* the foreign
    key) — guessing which one is the parent link among those would be
    exactly the kind of wrong guess this module exists to avoid, unlike a
    get/delete call, which usually takes just the id and little else."""
    if not isinstance(child_create_schema, dict):
        return None
    properties = child_create_schema.get("properties", {})
    if not isinstance(properties, dict):
        return None
    candidate = f"{parent_resource}_id"
    return candidate if candidate in properties else None


def find_parent_child_pairs(
    groups: list[ResourceGroup], create_schemas: dict[str, dict[str, Any] | None],
) -> list[ParentChildPair]:
    """Detects which already-identified resource groups (see
    `group_resource_tools`) reference another as a parent, restricted to
    pairs where the cross-resource workflow this exists to drive is actually
    runnable: the parent must have a delete tool (the workflow deletes it)
    and the child must have a read tool (the workflow checks whether it's
    still readable afterward). Within that, a pair is only reported when
    exactly one property on the child's create schema names the parent
    (`{parent_resource}_id`) — everything else (zero matches, or the same
    child linking to more than one plausible parent by that name) isn't
    guessed at, it's just not a pair."""
    pairs: list[ParentChildPair] = []
    for parent in groups:
        if parent.delete_tool is None:
            continue
        for child in groups:
            if child is parent or child.read_tool is None:
                continue
            link = find_parent_link_property(create_schemas.get(child.create_tool), parent.resource)
            if link is not None:
                pairs.append(ParentChildPair(parent=parent, child=child, parent_link_property=link))
    return pairs
