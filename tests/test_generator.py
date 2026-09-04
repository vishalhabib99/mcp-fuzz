from mcp_fuzz.generator import (
    generate_valid_arguments,
    generate_valid_value,
    missing_required_variants,
    wrong_type_variants,
)


def test_string_respects_enum():
    assert generate_valid_value({"type": "string", "enum": ["b", "a"]}) == "b"


def test_string_respects_format():
    assert generate_valid_value({"type": "string", "format": "email"}) == "test@example.com"


def test_string_respects_min_length():
    value = generate_valid_value({"type": "string", "minLength": 8})
    assert len(value) >= 8


def test_integer_respects_minimum():
    assert generate_valid_value({"type": "integer", "minimum": 5}) == 5


def test_number_respects_exclusive_minimum():
    assert generate_valid_value({"type": "number", "exclusiveMinimum": 0}) == 1.0


def test_boolean():
    assert generate_valid_value({"type": "boolean"}) is True


def test_array_uses_items_schema():
    value = generate_valid_value({"type": "array", "items": {"type": "integer", "minimum": 3}})
    assert value == [3]


def test_nested_object_fills_required_fields():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "required": ["name"],
    }
    value = generate_valid_value(schema)
    assert value == {"name": "test"}


def test_anyof_uses_first_branch():
    schema = {"anyOf": [{"type": "integer", "minimum": 9}, {"type": "string"}]}
    assert generate_valid_value(schema) == 9


def test_generate_valid_arguments_top_level_schema():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    }
    args = generate_valid_arguments(schema)
    assert args == {"query": "test", "limit": 10}


def test_missing_required_variants_one_per_required_field():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }
    variants = missing_required_variants(schema)
    names = {name for name, _ in variants}
    assert names == {"a", "b"}
    for name, args in variants:
        assert name not in args


def test_missing_required_variants_empty_when_nothing_required():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert missing_required_variants(schema) == []


def test_wrong_type_variants_swaps_each_typed_property():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "flag": {"type": "boolean"},
        },
        "required": ["count", "flag"],
    }
    variants = wrong_type_variants(schema)
    by_name = dict(variants)
    assert isinstance(by_name["count"]["count"], str)
    assert isinstance(by_name["flag"]["flag"], str)


def test_wrong_type_variants_skips_untyped_property():
    schema = {
        "type": "object",
        "properties": {"anything": {"description": "no type declared"}},
        "required": ["anything"],
    }
    assert wrong_type_variants(schema) == []


def test_empty_schema_produces_no_variants():
    assert generate_valid_arguments(None) == {}
    assert missing_required_variants(None) == []
    assert wrong_type_variants(None) == []
