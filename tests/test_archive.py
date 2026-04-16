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

from meerkhive.archive import build_selection_block, parse_filters, parse_sort, unwrap_type


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
# unwrap_type
# ---------------------------------------------------------------------------


def test_unwrap_type_strips_nonnull_and_list():
    inner = GraphQLString
    wrapped = GraphQLNonNull(GraphQLList(GraphQLNonNull(inner)))
    assert unwrap_type(wrapped) is inner


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


def test_build_selection_block_explicit_field_subset(observation_type):
    block = build_selection_block(observation_type, fields={"CaptureBlockId"})
    assert "CaptureBlockId" in block
    assert "rdb" not in block
    assert "telescope" not in block


def test_build_selection_block_star_means_all(observation_type):
    block_star = build_selection_block(observation_type, fields={"*"})
    block_default = build_selection_block(observation_type, fields=None)
    assert block_star == block_default


def test_build_selection_block_field_overrides_can_be_replaced(observation_type):
    custom = {"CaptureBlockId": lambda _fmt: "CaptureBlockId @custom"}
    block = build_selection_block(observation_type, field_overrides=custom)
    assert "CaptureBlockId @custom" in block
    # Default rdb override is not in effect when overrides are replaced.
    assert "rdb(internal" not in block


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


# --- Depth-limit handling for object fields ---


def test_build_selection_block_skips_objects_at_depth_limit():
    """Object fields beyond max_depth are skipped, not emitted as bare names."""
    leaf = GraphQLObjectType(
        name="Leaf",
        fields={"value": GraphQLField(GraphQLString)},
    )
    mid = GraphQLObjectType(
        name="Mid",
        fields={
            "label": GraphQLField(GraphQLString),
            "leaf": GraphQLField(leaf),
        },
    )
    root = GraphQLObjectType(
        name="Root",
        fields={
            "name": GraphQLField(GraphQLString),
            "mid": GraphQLField(mid),
        },
    )
    # max_depth=1: root fields are processed at depth 0 (0 < 1 so mid
    # recurses), but inside mid the depth is 1 which equals max_depth —
    # so "leaf" (an object) must be skipped rather than emitted as a bare
    # field name (which would be invalid GraphQL).
    block = build_selection_block(root, max_depth=1)
    assert "name" in block
    assert "mid {" in block
    assert "label" in block
    assert "leaf" not in block


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
