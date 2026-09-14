"""A tiny real MCP server used to exercise mcp-fuzz's engine end-to-end.
Deliberately includes one tool of each kind mcp-fuzz should distinguish:
well-behaved, crashes on bad input, hangs, and a non-read-only tool that
should be skipped by default.

Written against the official SDK's current `MCPServer` API (`mcp>=2.0`,
where `FastMCP` was renamed from `mcp.server.fastmcp.FastMCP`) — verified
directly against the installed package, not assumed from older examples.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("mcp-fuzz-fixture")

READ_ONLY = ToolAnnotations(read_only_hint=True)
NOT_READ_ONLY = ToolAnnotations(read_only_hint=False)


@server.tool(annotations=READ_ONLY)
def well_behaved(name: str, count: int = 1) -> str:
    """Echoes name count times. Validates its own inputs properly."""
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    if not isinstance(count, int):
        raise ValueError("count must be an integer")
    return (name + " ") * count


@server.tool(annotations=READ_ONLY)
def crashes_on_bad_input(value: int) -> str:
    """Divides 100 by value. Crashes (unhandled exception) if value is missing or the wrong type."""
    # Deliberately no validation — a wrong-typed or missing `value` raises
    # an uncaught TypeError, simulating a real server that doesn't guard
    # its handler against a malformed call.
    return str(100 / value)


@server.tool(annotations=READ_ONLY)
def hangs_forever(value: str) -> str:
    """Never returns — simulates a server tool that hangs on certain input."""
    time.sleep(3600)
    return value


@server.tool(annotations=NOT_READ_ONLY)
def delete_everything(target: str) -> str:
    """A destructive tool that should be skipped by default."""
    return f"deleted {target}"


@server.tool(annotations=READ_ONLY)
def always_crashes(x: str) -> str:
    """Raises unconditionally, even on schema-valid input — the SDK's own
    exception handling turns this into a structured error, not a process
    crash (see kills_process below for that)."""
    raise RuntimeError("this tool always crashes")


@server.tool(annotations=READ_ONLY)
def kills_process(x: str) -> str:
    """os._exit terminates the process immediately, bypassing all Python
    exception handling — a real process crash, not an SDK-caught error,
    to verify mcp-fuzz's connection-death detection and reconnect."""
    import os

    os._exit(1)


@server.tool(annotations=READ_ONLY)
def slow_but_fine(value: str) -> str:
    """Sleeps briefly then returns normally — well under the timeout, so it
    should never be flagged as a crash or hang, only (relative to this
    fixture server's other near-instant tools) as unusually slow by the
    latency check."""
    time.sleep(1.2)
    return value


@server.tool(annotations=READ_ONLY)
def bloated_but_fine(value: str) -> str:
    """Returns a huge response instantly — not slow, not a crash, only
    (relative to this fixture server's other tiny-response tools) unusually
    large by the response-size check."""
    return value * 10000


_LOCK_PATH = os.path.join(tempfile.gettempdir(), "mcp_fuzz_fixture_concurrency_lock")


@server.tool(annotations=READ_ONLY)
def not_concurrency_safe(value: str) -> str:
    """Uses a naive create-exclusive lock file with no retry/queue handling
    to serialize access to a shared resource — a real, common (buggy)
    pattern. Fine when called once at a time; a second call landing while
    the first is still "holding" the file raises FileExistsError, exactly
    the kind of shared-state race the concurrency check exists to catch."""
    fh = open(_LOCK_PATH, "x")
    try:
        time.sleep(0.3)
        return value
    finally:
        fh.close()
        os.remove(_LOCK_PATH)


_ITEMS: dict[str, dict] = {}


@server.tool(annotations=NOT_READ_ONLY)
def create_item(name: str) -> str:
    """Creates an item and returns its id as JSON — part of a well-behaved
    create/get/delete trio for the --sequential check to exercise."""
    item_id = str(uuid.uuid4())
    _ITEMS[item_id] = {"id": item_id, "name": name}
    return json.dumps(_ITEMS[item_id])


@server.tool(annotations=READ_ONLY)
def get_item(item_id: str) -> str:
    """Reads an item by id — errors if it doesn't exist (including after a
    real delete_item call), the correctly-behaved case."""
    if item_id not in _ITEMS:
        raise ValueError(f"no item with id {item_id}")
    return json.dumps(_ITEMS[item_id])


@server.tool(annotations=NOT_READ_ONLY)
def delete_item(item_id: str) -> str:
    """Deletes an item by id for real."""
    _ITEMS.pop(item_id, None)
    return f"deleted {item_id}"


_TICKETS: dict[str, dict] = {}


@server.tool(annotations=NOT_READ_ONLY)
def create_ticket(title: str) -> str:
    """Creates a ticket and returns its id as JSON — part of a deliberately
    buggy create/get/delete trio (delete_ticket reports success but never
    actually removes it), to verify the --sequential check's stale-after-
    delete detection catches a real instance of the bug it exists for."""
    ticket_id = str(uuid.uuid4())
    _TICKETS[ticket_id] = {"id": ticket_id, "title": title}
    return json.dumps(_TICKETS[ticket_id])


@server.tool(annotations=READ_ONLY)
def get_ticket(ticket_id: str) -> str:
    """Reads a ticket by id."""
    if ticket_id not in _TICKETS:
        raise ValueError(f"no ticket with id {ticket_id}")
    return json.dumps(_TICKETS[ticket_id])


@server.tool(annotations=NOT_READ_ONLY)
def delete_ticket(ticket_id: str) -> str:
    """Deliberately buggy: validates the ticket exists and reports success,
    but never actually removes it from the store — get_ticket will still
    succeed afterward. This is the exact stale-read bug shape --sequential
    exists to catch, reproduced here on purpose rather than left to chance."""
    if ticket_id not in _TICKETS:
        raise ValueError(f"no ticket with id {ticket_id}")
    return f"deleted {ticket_id}"


_PROJECTS: dict[str, dict] = {}
_TASKS: dict[str, dict] = {}


@server.tool(annotations=NOT_READ_ONLY)
def create_project(name: str) -> str:
    """Creates a project and returns its id as JSON — the parent half of a
    deliberately buggy parent/child pair for the cross-resource lifecycle
    check to exercise: delete_project below does not clean up the project's
    own tasks."""
    project_id = str(uuid.uuid4())
    _PROJECTS[project_id] = {"id": project_id, "name": name}
    return json.dumps(_PROJECTS[project_id])


@server.tool(annotations=NOT_READ_ONLY)
def delete_project(project_id: str) -> str:
    """Deletes a project for real, but — deliberately, the bug this pair
    exists to demonstrate — never touches any task that referenced it, so a
    task created against this project stays fully readable afterward."""
    _PROJECTS.pop(project_id, None)
    return f"deleted {project_id}"


@server.tool(annotations=NOT_READ_ONLY)
def create_task(project_id: str, title: str) -> str:
    """Creates a task referencing a real project id — the child half of the
    project/task pair. Does not validate that project_id actually exists,
    same as most real APIs' create endpoints for a dependent resource."""
    task_id = str(uuid.uuid4())
    _TASKS[task_id] = {"id": task_id, "project_id": project_id, "title": title}
    return json.dumps(_TASKS[task_id])


@server.tool(annotations=READ_ONLY)
def get_task(task_id: str) -> str:
    """Reads a task by id — stays readable even once its project is
    deleted, since delete_project above never cascades. The cross-resource
    check exists to surface exactly this as an observation, not silently
    miss it the way testing project and task in isolation would."""
    if task_id not in _TASKS:
        raise ValueError(f"no task with id {task_id}")
    return json.dumps(_TASKS[task_id])


_TEAMS: dict[str, dict] = {}
_MEMBERS: dict[str, dict] = {}


@server.tool(annotations=NOT_READ_ONLY)
def create_team(name: str) -> str:
    """Creates a team and returns its id as JSON — the parent half of a
    correctly-behaved parent/child pair, included as a clean contrast to
    the buggy project/task pair above: delete_team below does cascade."""
    team_id = str(uuid.uuid4())
    _TEAMS[team_id] = {"id": team_id, "name": name}
    return json.dumps(_TEAMS[team_id])


@server.tool(annotations=NOT_READ_ONLY)
def delete_team(team_id: str) -> str:
    """Deletes a team for real, and — the correctly-behaved case — also
    removes every member that referenced it, so a member created against
    this team correctly stops being readable afterward."""
    _TEAMS.pop(team_id, None)
    for member_id in [m_id for m_id, m in _MEMBERS.items() if m["team_id"] == team_id]:
        _MEMBERS.pop(member_id, None)
    return f"deleted {team_id}"


@server.tool(annotations=NOT_READ_ONLY)
def create_member(team_id: str, name: str) -> str:
    """Creates a member referencing a real team id — the child half of the
    team/member pair."""
    member_id = str(uuid.uuid4())
    _MEMBERS[member_id] = {"id": member_id, "team_id": team_id, "name": name}
    return json.dumps(_MEMBERS[member_id])


@server.tool(annotations=READ_ONLY)
def get_member(member_id: str) -> str:
    """Reads a member by id — correctly errors once its team (and thus this
    member, via delete_team's cascade) has been deleted."""
    if member_id not in _MEMBERS:
        raise ValueError(f"no member with id {member_id}")
    return json.dumps(_MEMBERS[member_id])


if __name__ == "__main__":
    server.run(transport="stdio")
