"""Regression tests for cross-`mcp`-SDK-version field access.

mcp<2.0 exposed several `types` fields under their raw camelCase wire name
directly (`isError`, `inputSchema`, `readOnlyHint`); mcp>=2.0 renamed them to
snake_case (`is_error`, `input_schema`, `read_only_hint`) with the camelCase
kept only as a validation alias, not a readable attribute. Found live:
installing a real target server (`arxiv-mcp-server`, pinned `mcp<2.0`) into
the same environment as mcp-fuzz downgraded the shared `mcp` package and
broke every hardcoded snake_case attribute access with an AttributeError.

These use plain `SimpleNamespace` stand-ins rather than a real old `mcp`
install (not pulled in as a test dependency) — `_field`/`_is_read_only` only
ever use `hasattr`/`getattr`, so a namespace missing the snake_case
attribute reproduces the real pydantic behavior exactly.
"""

from types import SimpleNamespace

from mcp_fuzz.engine import _field, _is_read_only


def test_field_prefers_snake_case_when_present():
    obj = SimpleNamespace(is_error=True, isError=False)
    assert _field(obj, "is_error", "isError") is True


def test_field_falls_back_to_camel_case_when_snake_missing():
    # mcp<2.0's shape — only the camelCase wire-format attribute exists.
    obj = SimpleNamespace(isError=True)
    assert _field(obj, "is_error", "isError") is True


def test_is_read_only_true_with_snake_case_annotations():
    tool = SimpleNamespace(annotations=SimpleNamespace(read_only_hint=True))
    assert _is_read_only(tool) is True


def test_is_read_only_true_with_camel_case_annotations():
    # The exact real-world shape (mcp<2.0) that broke against
    # arxiv-mcp-server with an AttributeError before this fix.
    tool = SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=True))
    assert _is_read_only(tool) is True


def test_is_read_only_false_when_no_annotations():
    assert _is_read_only(SimpleNamespace(annotations=None)) is False


def test_is_read_only_false_when_hint_is_false_or_none():
    assert _is_read_only(SimpleNamespace(annotations=SimpleNamespace(read_only_hint=False))) is False
    assert _is_read_only(SimpleNamespace(annotations=SimpleNamespace(read_only_hint=None))) is False
