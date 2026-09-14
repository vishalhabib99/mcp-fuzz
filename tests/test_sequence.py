"""Unit tests for the pure heuristics in mcp_fuzz.sequence, plus an
end-to-end test proving the real engine catches a genuine stale-after-delete
bug in the fixture server's deliberately-buggy create_ticket/get_ticket/
delete_ticket trio, and does NOT flag the correctly-behaved
create_item/get_item/delete_item trio alongside it."""

import sys
from pathlib import Path

import pytest

from mcp_fuzz.engine import run_fuzz
from mcp_fuzz.report import build_report
from mcp_fuzz.sequence import (
    ResourceGroup,
    extract_id,
    find_id_property,
    find_parent_child_pairs,
    find_parent_link_property,
    group_resource_tools,
)

FIXTURE_SERVER = str(Path(__file__).parent / "fixtures" / "fixture_server.py")


# --- group_resource_tools --------------------------------------------------

def test_groups_a_clean_create_get_delete_trio():
    groups = group_resource_tools(["create_item", "get_item", "delete_item", "well_behaved"])
    assert len(groups) == 1
    g = groups[0]
    assert g.resource == "item"
    assert g.create_tool == "create_item"
    assert g.read_tool == "get_item"
    assert g.delete_tool == "delete_item"


def test_recognizes_alternate_verbs():
    groups = group_resource_tools(["add_widget", "fetch_widget", "remove_widget"])
    assert len(groups) == 1
    g = groups[0]
    assert (g.resource, g.create_tool, g.read_tool, g.delete_tool) == ("widget", "add_widget", "fetch_widget", "remove_widget")


def test_create_only_with_no_read_or_delete_is_not_a_group():
    groups = group_resource_tools(["create_orphan", "well_behaved"])
    assert groups == []


def test_ambiguous_double_create_for_same_resource_is_dropped():
    groups = group_resource_tools(["create_item", "add_item", "get_item"])
    assert groups == []  # two create-shaped tools for "item" — don't guess which one


def test_does_not_false_match_a_tool_whose_name_merely_starts_with_a_verb_word():
    # "creative_writing_helper" must never be treated as create_<resource>.
    groups = group_resource_tools(["creative_writing_helper", "get_item"])
    assert groups == []


def test_partial_group_with_only_delete_is_still_detected():
    groups = group_resource_tools(["create_session", "delete_session"])
    assert len(groups) == 1
    assert groups[0].read_tool is None
    assert groups[0].delete_tool == "delete_session"


# --- extract_id --------------------------------------------------------------

def test_extracts_id_from_json_object_response():
    assert extract_id('{"id": "abc123", "name": "x"}', "item") == "abc123"


def test_prefers_resource_prefixed_id_key():
    assert extract_id('{"item_id": "abc123", "id": "wrong"}', "item") == "abc123"


def test_extracts_id_from_a_single_item_json_list():
    assert extract_id('[{"id": "abc123"}]', "item") == "abc123"


def test_falls_back_to_a_bare_uuid_in_plain_text():
    text = "Created item 3f9a1c1e-7b2a-4a3e-9c1a-1e2b3c4d5e6f successfully"
    assert extract_id(text, "item") == "3f9a1c1e-7b2a-4a3e-9c1a-1e2b3c4d5e6f"


def test_returns_none_when_nothing_id_shaped_is_present():
    assert extract_id("created successfully", "item") is None


# --- find_id_property ---------------------------------------------------------

def test_finds_exact_resource_prefixed_id_property():
    schema = {"properties": {"item_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["item_id"]}
    assert find_id_property(schema, "item") == "item_id"


def test_finds_bare_id_property():
    schema = {"properties": {"id": {"type": "string"}}, "required": ["id"]}
    assert find_id_property(schema, "item") == "id"


def test_falls_back_to_sole_required_string_property():
    schema = {"properties": {"ticket_ref": {"type": "string"}}, "required": ["ticket_ref"]}
    assert find_id_property(schema, "item") == "ticket_ref"


def test_gives_up_when_multiple_required_string_properties_and_no_exact_match():
    schema = {
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }
    assert find_id_property(schema, "item") is None


# --- find_parent_link_property ------------------------------------------------

def test_finds_exact_parent_id_property():
    schema = {"properties": {"project_id": {"type": "string"}, "title": {"type": "string"}}, "required": ["project_id", "title"]}
    assert find_parent_link_property(schema, "project") == "project_id"


def test_no_fallback_to_sole_required_string_property_for_parent_link():
    # Unlike find_id_property, a create call commonly has several required
    # fields — a bare match on "the one required string" would be a guess
    # about which one is the foreign key, not a real signal.
    schema = {"properties": {"ref": {"type": "string"}}, "required": ["ref"]}
    assert find_parent_link_property(schema, "project") is None


def test_returns_none_when_no_matching_property():
    schema = {"properties": {"title": {"type": "string"}}, "required": ["title"]}
    assert find_parent_link_property(schema, "project") is None


# --- find_parent_child_pairs ---------------------------------------------------

_PROJECT = ResourceGroup(resource="project", create_tool="create_project", read_tool=None, delete_tool="delete_project")
_TASK = ResourceGroup(resource="task", create_tool="create_task", read_tool="get_task", delete_tool=None)
_TICKET = ResourceGroup(resource="ticket", create_tool="create_ticket", read_tool="get_ticket", delete_tool="delete_ticket")


def test_detects_a_real_parent_child_pair():
    schemas = {"create_task": {"properties": {"project_id": {"type": "string"}, "title": {"type": "string"}}}}
    pairs = find_parent_child_pairs([_PROJECT, _TASK], schemas)
    assert len(pairs) == 1
    assert (pairs[0].parent.resource, pairs[0].child.resource, pairs[0].parent_link_property) == ("project", "task", "project_id")


def test_parent_without_a_delete_tool_is_not_considered():
    parent_no_delete = ResourceGroup(resource="project", create_tool="create_project", read_tool="get_project", delete_tool=None)
    schemas = {"create_task": {"properties": {"project_id": {"type": "string"}}}}
    assert find_parent_child_pairs([parent_no_delete, _TASK], schemas) == []


def test_child_without_a_read_tool_is_not_considered():
    child_no_read = ResourceGroup(resource="task", create_tool="create_task", read_tool=None, delete_tool="delete_task")
    schemas = {"create_task": {"properties": {"project_id": {"type": "string"}}}}
    assert find_parent_child_pairs([_PROJECT, child_no_read], schemas) == []


def test_unrelated_groups_produce_no_pairs():
    schemas = {"create_ticket": {"properties": {"title": {"type": "string"}}}}
    assert find_parent_child_pairs([_PROJECT, _TICKET], schemas) == []


# --- end-to-end: real engine against the real fixture server -----------------

@pytest.fixture(scope="module")
def sequential_report():
    import asyncio

    return asyncio.run(run_fuzz(
        sys.executable, [FIXTURE_SERVER], timeout=5.0, include_destructive=True, sequential=True,
    ))


def _group(report, resource):
    return next(g for g in report.sequence_results if g.resource == resource)


def test_healthy_trio_is_not_flagged_stale(sequential_report):
    g = _group(sequential_report, "item")
    assert g.extracted_id is not None
    assert g.stale_after_delete is False
    roles = [s.role for s in g.steps]
    assert roles == ["create", "read", "delete", "read_after_delete"]
    assert g.steps[-1].outcome.outcome != "ok"  # get_item correctly errors on the deleted id


def test_buggy_trio_is_caught_as_stale_after_delete(sequential_report):
    # This is the actual bug the whole check exists to catch: delete_ticket
    # reports success but never removes the ticket, so get_ticket still
    # succeeds afterward — a real, reproducible stale-read bug, not a
    # simulated outcome.
    g = _group(sequential_report, "ticket")
    assert g.extracted_id is not None
    assert g.stale_after_delete is True
    assert g.steps[-1].role == "read_after_delete"
    assert g.steps[-1].outcome.outcome == "ok"
    assert "just deleted" in g.note


def test_report_summarizes_the_sequence_findings(sequential_report):
    report = build_report(sequential_report)
    # item, ticket (full trios) plus project, task, team, member (each forms
    # its own single-resource group too, since every one of them has either
    # a delete or a read tool of its own) — six total, only ticket is stale.
    assert report.sequence.groups_detected == 6
    assert report.sequence.stale_after_delete_count == 1


# --- end-to-end cross-resource: real engine against the real fixture server --

def _pair(report, child_resource):
    return next(r for r in report.cross_resource_results if r.child_resource == child_resource)


def test_detects_both_real_parent_child_pairs(sequential_report):
    assert len(sequential_report.cross_resource_results) == 2
    resources = {(r.parent_resource, r.child_resource) for r in sequential_report.cross_resource_results}
    assert resources == {("project", "task"), ("team", "member")}


def test_buggy_pair_flags_child_still_readable_after_parent_delete(sequential_report):
    # The actual bug this check exists to catch: delete_project has no idea
    # tasks exist, so get_task still succeeds on a task whose project is gone.
    r = _pair(sequential_report, "task")
    assert r.parent_id is not None
    assert r.child_id is not None
    assert r.child_readable_after_parent_delete is True
    assert r.steps[-1].role == "read_child_after_parent_delete"
    assert r.steps[-1].outcome.outcome == "ok"
    assert "worth confirming" in r.note


def test_cascading_pair_is_not_flagged(sequential_report):
    r = _pair(sequential_report, "member")
    assert r.parent_id is not None
    assert r.child_id is not None
    assert r.child_readable_after_parent_delete is False
    assert r.steps[-1].outcome.outcome != "ok"
    assert r.note == ""


def test_report_summarizes_the_cross_resource_findings(sequential_report):
    report = build_report(sequential_report)
    assert report.cross_resource.pairs_detected == 2
    assert report.cross_resource.orphaned_child_count == 1
