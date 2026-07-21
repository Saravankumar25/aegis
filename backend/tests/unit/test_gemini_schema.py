"""Unit tests: Pydantic JSON Schema -> Gemini `responseSchema` translation (ESD §20).

This module is tested directly rather than only through the provider because its
failure mode is silent. A schema Gemini quietly accepts in degraded form produces
output that fails Pydantic validation downstream, and the repair loop then reads that
as the *model's* mistake — burning the whole repair budget re-asking a question that
was malformed before it was ever sent. Every assertion here corresponds to a
construct Pydantic actually emits for the models Aegis already uses.
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, Field

from providers.gemini_schema import SchemaTranslationError, to_gemini_schema


def _translate(model: type[BaseModel]) -> dict:
    return to_gemini_schema(model.model_json_schema())


# --- the basics Gemini rejects if we pass JSON Schema through unchanged -------------------


def test_types_are_uppercased_and_noise_keys_dropped():
    class M(BaseModel):
        name: str = Field(default="x", description="The name.")
        count: int

    out = _translate(M)
    assert out["type"] == "OBJECT"
    assert out["properties"]["name"]["type"] == "STRING"
    assert out["properties"]["count"]["type"] == "INTEGER"
    # `title`, `default` and `additionalProperties` are all rejected by Gemini.
    for forbidden in ("title", "default", "additionalProperties", "$schema"):
        assert forbidden not in out
        assert forbidden not in out["properties"]["name"]
    # Descriptions are the one hint the model gets; they must survive.
    assert out["properties"]["name"]["description"] == "The name."


def test_required_is_preserved_and_optional_is_not_required():
    class M(BaseModel):
        needed: str
        optional: str | None = None

    out = _translate(M)
    assert out["required"] == ["needed"]


def test_property_ordering_is_emitted_and_matches_declaration_order():
    """Gemini's decoder is order-sensitive; unpinned order drifts between calls."""

    class M(BaseModel):
        first: str
        second: str
        third: str

    out = _translate(M)
    assert out["propertyOrdering"] == ["first", "second", "third"]


# --- the constructs that actually break ---------------------------------------------------


def test_nested_model_ref_is_inlined():
    """Pydantic emits `$ref` + `$defs` for any nested model; Gemini rejects both."""

    class Inner(BaseModel):
        value: str

    class Outer(BaseModel):
        inner: Inner

    out = _translate(Outer)
    inner = out["properties"]["inner"]
    assert "$ref" not in str(out)
    assert "$defs" not in out
    assert inner["type"] == "OBJECT"
    assert inner["properties"]["value"]["type"] == "STRING"


def test_optional_becomes_nullable_rather_than_any_of_null():
    class M(BaseModel):
        maybe: str | None = None

    out = _translate(M)
    field = out["properties"]["maybe"]
    assert field["type"] == "STRING"
    assert field["nullable"] is True
    assert "anyOf" not in field


def test_optional_field_keeps_its_description():
    """Pydantic hangs the description on the anyOf wrapper, which is easy to drop."""

    class M(BaseModel):
        prior: str | None = Field(default=None, description="Related incident id.")

    out = _translate(M)
    assert out["properties"]["prior"]["description"] == "Related incident id."


def test_literal_becomes_a_string_enum():
    class M(BaseModel):
        severity: Literal["P1", "P2", "P3", "P4"]

    out = _translate(M)
    field = out["properties"]["severity"]
    assert field["type"] == "STRING"
    assert field["enum"] == ["P1", "P2", "P3", "P4"]


def test_list_of_nested_models_is_inlined():
    class Citation(BaseModel):
        source: str

    class M(BaseModel):
        citations: list[Citation]

    out = _translate(M)
    items = out["properties"]["citations"]["items"]
    assert out["properties"]["citations"]["type"] == "ARRAY"
    assert items["type"] == "OBJECT"
    assert items["properties"]["source"]["type"] == "STRING"


def test_optional_list_of_models_is_nullable_array():
    class Item(BaseModel):
        k: str

    class M(BaseModel):
        items: list[Item] | None = None

    out = _translate(M)
    field = out["properties"]["items"]
    assert field["type"] == "ARRAY"
    assert field["nullable"] is True
    assert field["items"]["properties"]["k"]["type"] == "STRING"


# --- constructs that must fail loudly at build time, not as an opaque 400 -----------------


def test_recursive_model_is_rejected_with_a_named_cause():
    class Node(BaseModel):
        child: Node | None = None

    Node.model_rebuild()
    with pytest.raises(SchemaTranslationError, match="recursi"):
        _translate(Node)


def test_open_ended_dict_is_rejected_with_actionable_message():
    class M(BaseModel):
        payload: dict

    with pytest.raises(SchemaTranslationError, match="no properties"):
        _translate(M)


def test_dangling_ref_is_rejected():
    with pytest.raises(SchemaTranslationError, match="dangling"):
        to_gemini_schema(
            {"type": "object", "properties": {"a": {"$ref": "#/$defs/Missing"}}, "$defs": {}}
        )


def test_external_ref_is_rejected():
    with pytest.raises(SchemaTranslationError, match="local"):
        to_gemini_schema({"type": "object", "properties": {"a": {"$ref": "https://example.com/S"}}})


# --- the real schemas the system actually sends -------------------------------------------


def test_every_registered_agent_schema_translates():
    """A schema that cannot be translated is a runtime failure on a real incident.

    Import-and-translate rather than hand-rolled fixtures: this fails the build when
    someone adds an untranslatable field to a live agent contract.
    """
    from agents.observer.critic import CritiqueResult

    out = to_gemini_schema(CritiqueResult.model_json_schema())
    assert out["type"] == "OBJECT"
    assert out["properties"]
