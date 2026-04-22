"""Unit tests for the schema-driven helpers in :mod:`meerkhive.archive`.

The HTTP transport itself is intentionally not unit-tested — it's a thin
shim over :class:`gql.transport.aiohttp.AIOHTTPTransport`, and exercising it
meaningfully requires a live GraphQL server. The optional
:mod:`tests.test_archive_live` integration test covers that path against
the real archive when credentials are available.
"""

import pytest
from graphql import (
    GraphQLArgument,
    GraphQLEnumType,
    GraphQLEnumValue,
    GraphQLField,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLString,
    GraphQLUnionType,
)
from graphql.pyutils import Undefined

from meerkhive.archive import build_selection_block, parse_filters, parse_sort


@pytest.fixture
def observation_type() -> GraphQLObjectType:
    """A miniature ``Observation`` type that mirrors the real archive shape.

    Includes one scalar, one nested object, one list-wrapped scalar, and
    the special ``rdb`` field that needs an ``internal`` argument.
    """
    nested = GraphQLObjectType(
        name="Telescope",
        fields={
            "name": GraphQLField(GraphQLString),
            "band": GraphQLField(GraphQLString),
        },
    )
    return GraphQLObjectType(
        name="Observation",
        fields={
            "CaptureBlockId": GraphQLField(GraphQLNonNull(GraphQLString)),
            "rdb": GraphQLField(GraphQLString),
            "products": GraphQLField(GraphQLList(GraphQLString)),
            "telescope": GraphQLField(nested),
        },
    )


# ---------------------------------------------------------------------------
# build_selection_block
# ---------------------------------------------------------------------------


def test_build_selection_block_includes_all_fields_by_default(observation_type):
    block = build_selection_block(observation_type)
    assert "CaptureBlockId" in block
    assert "products" in block
    assert "telescope {" in block
    assert "name" in block and "band" in block


def test_build_selection_block_external_renders_rdb_internal_false(observation_type):
    block = build_selection_block(observation_type, url_format="external")
    assert "rdb(internal: false)" in block


def test_build_selection_block_internal_renders_rdb_internal_true(observation_type):
    block = build_selection_block(observation_type, url_format="internal")
    assert "rdb(internal: true)" in block


def test_build_selection_block_skip_fields_omits_them(observation_type):
    block = build_selection_block(observation_type, skip_fields={"rdb", "products"})
    assert "rdb" not in block
    assert "products" not in block
    assert "CaptureBlockId" in block


def test_build_selection_block_skip_fields_does_not_propagate_to_nested_types(observation_type):
    """skip_fields is top-level only; nested types with same-named fields are unaffected."""
    # The fixture's Telescope nested type has "name" and "band".
    # Skipping "name" at the top level must not drop Telescope.name.
    block = build_selection_block(observation_type, skip_fields={"name"})
    assert "telescope {" in block
    assert "name" in block  # Telescope.name must still appear


def test_build_selection_block_unknown_fields_raises(observation_type):
    """Requesting a field not in the schema raises ValueError immediately."""
    with pytest.raises(ValueError, match="Unknown field"):
        build_selection_block(observation_type, fields={"CaptureBlockId", "nonExistent"})


def test_build_selection_block_explicit_field_subset(observation_type):
    block = build_selection_block(observation_type, fields={"CaptureBlockId"})
    assert "CaptureBlockId" in block
    assert "rdb" not in block
    assert "telescope" not in block


def test_build_selection_block_none_means_all(observation_type):
    """Passing fields=None is identical to omitting the argument."""
    block_explicit_none = build_selection_block(observation_type, fields=None)
    block_default = build_selection_block(observation_type)
    assert block_explicit_none == block_default


def test_build_selection_block_field_overrides_can_be_replaced(observation_type):
    custom = {"CaptureBlockId": lambda _fmt: "CaptureBlockId @custom"}
    block = build_selection_block(observation_type, field_overrides=custom)
    assert "CaptureBlockId @custom" in block
    # Default rdb override is not in effect when overrides are replaced.
    assert "rdb(internal" not in block


def test_build_selection_block_omits_nested_field_when_sub_selection_is_empty():
    """An object field whose sub-selection would be empty is dropped entirely.

    If every field inside a nested type is skipped (e.g. all have required
    arguments), emitting ``field { }`` would produce invalid GraphQL. The
    walker must omit the outer field instead.
    """
    # A nested type whose only field requires an argument — it will always be skipped.
    empty_nested = GraphQLObjectType(
        name="Inner",
        fields={
            "guarded": GraphQLField(
                GraphQLString,
                args={
                    "mode": GraphQLArgument(
                        GraphQLNonNull(GraphQLString),
                        default_value=Undefined,
                    ),
                },
            ),
        },
    )
    root = GraphQLObjectType(
        name="Root",
        fields={
            "id": GraphQLField(GraphQLString),
            "inner": GraphQLField(empty_nested),
        },
    )
    block = build_selection_block(root)
    assert "id" in block
    assert "inner" not in block


# --- Required-argument skip logic ---


@pytest.fixture
def type_with_required_arg() -> GraphQLObjectType:
    """Type containing a field with a required (non-null, no default) argument."""
    return GraphQLObjectType(
        name="Query",
        fields={
            "simple": GraphQLField(GraphQLString),
            "guarded": GraphQLField(
                GraphQLString,
                args={
                    "mode": GraphQLArgument(
                        GraphQLNonNull(GraphQLString),
                        default_value=Undefined,
                    ),
                },
            ),
        },
    )


def test_build_selection_block_skips_field_with_required_arg(type_with_required_arg):
    """Fields with required arguments are omitted unless overridden."""
    block = build_selection_block(type_with_required_arg)
    assert "simple" in block
    assert "guarded" not in block


def test_build_selection_block_includes_required_arg_field_with_override(
    type_with_required_arg,
):
    """An explicit field_overrides entry allows a required-arg field through."""
    overrides = {"guarded": lambda _fmt: 'guarded(mode: "fast")'}
    block = build_selection_block(type_with_required_arg, field_overrides=overrides)
    assert "simple" in block
    assert 'guarded(mode: "fast")' in block


# --- Enum and abstract type handling ---


def test_build_selection_block_includes_enum_fields():
    """Enum-typed fields are leaf types and must appear in the selection block."""
    state_enum = GraphQLEnumType(
        "ObservationState",
        {"SCHEDULED": GraphQLEnumValue("SCHEDULED"), "COMPLETE": GraphQLEnumValue("COMPLETE")},
    )
    t = GraphQLObjectType(
        name="Observation",
        fields={
            "id": GraphQLField(GraphQLString),
            "state": GraphQLField(state_enum),
        },
    )
    block = build_selection_block(t)
    assert "id" in block
    assert "state" in block


def test_build_selection_block_skips_interface_fields():
    """Interface-typed fields are skipped — they need inline fragments."""
    iface = GraphQLInterfaceType("Node", fields={"id": GraphQLField(GraphQLString)})
    t = GraphQLObjectType(
        name="Observation",
        fields={
            "name": GraphQLField(GraphQLString),
            "node": GraphQLField(iface),
        },
        interfaces=[],
    )
    block = build_selection_block(t)
    assert "name" in block
    assert "node" not in block


def test_build_selection_block_breaks_self_referential_cycle():
    """A type that references itself (directly or transitively) must not recurse forever.

    The live archive schema exposes ``Observation -> Proposal -> KeycloakGroup``,
    where ``KeycloakGroup`` has a ``subGroups: [KeycloakGroup]`` field. Without
    cycle detection the walker would hit Python's recursion limit. The walker
    must break the cycle by skipping the back-reference while still emitting
    the leaf fields of the recursing type.
    """
    # Build a type that references itself via a list field. We construct the
    # field lazily because GraphQLObjectType's fields thunk is the only way
    # to express self-reference at construction time.
    group: GraphQLObjectType
    group = GraphQLObjectType(
        name="KeycloakGroup",
        fields=lambda: {
            "id": GraphQLField(GraphQLString),
            "name": GraphQLField(GraphQLString),
            "subGroups": GraphQLField(GraphQLList(group)),
        },
    )
    root = GraphQLObjectType(
        name="Observation",
        fields={
            "CaptureBlockId": GraphQLField(GraphQLString),
            "group": GraphQLField(group),
        },
    )

    block = build_selection_block(root)

    # The outer group field is emitted with its leaf fields, but the
    # self-referential subGroups field is skipped to break the cycle.
    assert "CaptureBlockId" in block
    assert "group {" in block
    assert "id" in block and "name" in block
    assert "subGroups" not in block


def test_build_selection_block_skips_union_fields():
    """Union-typed fields are skipped — they need inline fragments."""
    member = GraphQLObjectType("Member", fields={"value": GraphQLField(GraphQLString)})
    union = GraphQLUnionType("AnyResult", [member])
    t = GraphQLObjectType(
        name="Observation",
        fields={
            "name": GraphQLField(GraphQLString),
            "result": GraphQLField(union),
        },
    )
    block = build_selection_block(t)
    assert "name" in block
    assert "result" not in block


# ---------------------------------------------------------------------------
# parse_filters
# ---------------------------------------------------------------------------


def test_parse_filters_simple_key_value():
    result = parse_filters(["Band=L"])
    assert result == [{"field": "Band", "value": ["L"]}]


def test_parse_filters_colon_separator_rejected():
    with pytest.raises(ValueError, match="Invalid filter format"):
        parse_filters(["search:NGC1234"])


def test_parse_filters_from_to_pass_through():
    result = parse_filters(["from=2024-01-01", "to=2024-03-31"])
    assert result == [
        {"field": "from", "value": "2024-01-01"},
        {"field": "to", "value": "2024-03-31"},
    ]


def test_parse_filters_radec_parsed_as_json():
    result = parse_filters(['radec={"ra": 1.23, "dec": -4.56}'])
    assert result == [{"field": "radec", "value": {"ra": 1.23, "dec": -4.56}}]


def test_parse_filters_multi_value_band():
    result = parse_filters(["Band=L,UHF"])
    assert result == [{"field": "Band", "value": ["L", "UHF"]}]


def test_parse_filters_invalid_raises():
    with pytest.raises(ValueError, match="Invalid filter format"):
        parse_filters(["noequalssign"])


# ---------------------------------------------------------------------------
# parse_sort
# ---------------------------------------------------------------------------


def test_parse_sort_colon_separator():
    result = parse_sort(["StartTime:desc"])
    assert result == [{"columnKey": "StartTime", "direction": "DESC"}]


def test_parse_sort_equals_separator_rejected():
    with pytest.raises(ValueError, match="Invalid sort format"):
        parse_sort(["StartTime=asc"])


def test_parse_sort_multiple():
    result = parse_sort(["StartTime:desc", "CaptureBlockId:asc"])
    assert result == [
        {"columnKey": "StartTime", "direction": "DESC"},
        {"columnKey": "CaptureBlockId", "direction": "ASC"},
    ]


def test_parse_sort_case_insensitive_direction():
    result = parse_sort(["field:Desc"])
    assert result[0]["direction"] == "DESC"


def test_parse_sort_invalid_no_separator():
    with pytest.raises(ValueError, match="Invalid sort format"):
        parse_sort(["StartTime"])


def test_parse_sort_invalid_direction():
    with pytest.raises(ValueError, match="Invalid sort direction"):
        parse_sort(["StartTime:sideways"])


# ---------------------------------------------------------------------------
# fields / skip_fields interaction
# ---------------------------------------------------------------------------


def test_build_selection_block_skip_fields_wins_over_fields(observation_type):
    """When a field appears in both fields and skip_fields, skip_fields takes precedence."""
    block = build_selection_block(
        observation_type,
        fields={"CaptureBlockId", "rdb"},
        skip_fields={"rdb"},
    )
    assert "CaptureBlockId" in block
    assert "rdb" not in block
