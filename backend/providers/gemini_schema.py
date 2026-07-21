"""Translate Pydantic JSON Schema into Gemini's `responseSchema` dialect (ESD §20).

Gemini does not accept JSON Schema. It accepts a subset of the OpenAPI 3 Schema
object, which differs in ways that fail *silently* rather than loudly:

- **No ``$ref`` / ``$defs``.** Pydantic emits a ref for every nested model. Gemini
  rejects the payload, so refs must be inlined.
- **Type names are uppercase** (``STRING``, not ``string``).
- **Unknown keywords are rejected** — ``title``, ``default``, ``additionalProperties``
  and ``$schema`` all appear in Pydantic output and none are permitted.
- **Optionals arrive as ``anyOf: [T, null]``**, which is expressed as ``nullable`` on
  the non-null branch instead.

Getting this wrong does not raise a clear error: the API either 400s with a terse
message, or accepts a degraded schema and returns output that then fails Pydantic
validation, which the repair loop misreads as the *model's* fault and burns the
entire repair budget re-asking a question that was malformed to begin with. That is
why this is a separate, directly tested module rather than an inline dict-comprehension.

``propertyOrdering`` is emitted because Gemini's decoder is order-sensitive: without
it, field order can drift between calls, which measurably hurts quality on schemas
where one field's value depends on reasoning stated in an earlier one.
"""

from __future__ import annotations

from typing import Any

# JSON Schema type -> Gemini/OpenAPI type. Gemini has no "null" type; nullability is a
# separate boolean flag, handled where anyOf is unwrapped.
_TYPES = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}

# Formats Gemini recognises. Anything else (``uuid``, ``email``, ``date-time`` on a
# non-string, ...) is dropped rather than passed through, because an unrecognised
# format is a hard 400 and the constraint is not worth losing the call over.
_FORMATS = {
    "STRING": {"enum", "date-time"},
    "INTEGER": {"int32", "int64"},
    "NUMBER": {"float", "double"},
}

_MAX_DEPTH = 12


class SchemaTranslationError(ValueError):
    """The schema cannot be expressed in Gemini's dialect.

    Raised eagerly at call-construction time rather than letting the API reject it,
    so the failure names the offending construct instead of surfacing as a 400.
    """


def to_gemini_schema(json_schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic ``model_json_schema()`` document into Gemini's dialect."""
    defs = json_schema.get("$defs", {})
    return _convert(json_schema, defs, depth=0, seen=())


def _convert(
    node: dict[str, Any],
    defs: dict[str, Any],
    *,
    depth: int,
    seen: tuple[str, ...],
) -> dict[str, Any]:
    if depth > _MAX_DEPTH:
        raise SchemaTranslationError(
            f"schema nesting exceeded {_MAX_DEPTH} levels; Gemini rejects deeply nested "
            "schemas and a model cannot reliably fill one either — flatten the model"
        )

    node, seen = _resolve_ref(node, defs, seen=seen)

    if "anyOf" in node:
        return _convert_any_of(node, defs, depth=depth, seen=seen)

    out: dict[str, Any] = {}
    json_type = node.get("type")

    # An enum with no declared type is a string enum in practice (Pydantic emits this
    # for `Literal["a", "b"]` unions of strings).
    if json_type is None and "enum" in node:
        json_type = "string"
    if json_type is None and "properties" in node:
        json_type = "object"

    if json_type is None:
        # An unconstrained field. Gemini requires a type, and STRING is the only choice
        # that cannot corrupt the value — a model asked for STRING will serialise
        # whatever it had, whereas guessing OBJECT would force invented structure.
        json_type = "string"

    if json_type not in _TYPES:
        raise SchemaTranslationError(f"unsupported JSON Schema type: {json_type!r}")

    gemini_type = _TYPES[json_type]
    out["type"] = gemini_type

    if description := (node.get("description") or "").strip():
        out["description"] = description

    if enum := node.get("enum"):
        # Gemini requires enum values as strings even when the underlying type is not.
        out["enum"] = [str(v) for v in enum]

    fmt = node.get("format")
    if fmt and fmt in _FORMATS.get(gemini_type, set()):
        out["format"] = fmt

    if gemini_type == "ARRAY":
        items = node.get("items")
        if not isinstance(items, dict):
            raise SchemaTranslationError("array schema is missing a typed `items`")
        out["items"] = _convert(items, defs, depth=depth + 1, seen=seen)
        for bound_src, bound_dst in (("minItems", "minItems"), ("maxItems", "maxItems")):
            if bound_src in node:
                out[bound_dst] = node[bound_src]

    if gemini_type == "OBJECT":
        properties = node.get("properties") or {}
        converted = {
            name: _convert(sub, defs, depth=depth + 1, seen=seen)
            for name, sub in properties.items()
        }
        if not converted:
            # Gemini rejects an OBJECT with no properties. This is almost always a
            # `dict[str, Any]` field, which cannot be expressed — say so precisely.
            raise SchemaTranslationError(
                "object schema has no properties (an open-ended dict cannot be "
                "expressed in Gemini's dialect); give the field an explicit model"
            )
        out["properties"] = converted
        # Order-sensitive decoding: pin it so repeated calls generate fields in the
        # same order the schema declares them.
        out["propertyOrdering"] = list(converted)
        if required := [r for r in node.get("required", []) if r in converted]:
            out["required"] = required

    return out


def _convert_any_of(
    node: dict[str, Any],
    defs: dict[str, Any],
    *,
    depth: int,
    seen: tuple[str, ...],
) -> dict[str, Any]:
    """Unwrap ``anyOf``, folding an explicit null branch into ``nullable``."""
    branches = [b for b in node.get("anyOf", []) if isinstance(b, dict)]
    non_null = [b for b in branches if b.get("type") != "null"]
    nullable = len(non_null) != len(branches)

    if not non_null:
        raise SchemaTranslationError("anyOf contained only a null branch")

    if len(non_null) == 1:
        converted = _convert(non_null[0], defs, depth=depth, seen=seen)
        # Carry the outer description down: Pydantic puts `Field(description=...)` on
        # the wrapper for `Optional[T]`, and losing it strips the model's only hint
        # about what the field means.
        if (outer := (node.get("description") or "").strip()) and "description" not in converted:
            converted["description"] = outer
        if nullable:
            converted["nullable"] = True
        return converted

    # A genuine union. Gemini supports anyOf, but each branch must itself be valid.
    converted_branches = [_convert(b, defs, depth=depth + 1, seen=seen) for b in non_null]
    out: dict[str, Any] = {"anyOf": converted_branches}
    if nullable:
        out["nullable"] = True
    if description := (node.get("description") or "").strip():
        out["description"] = description
    return out


def _resolve_ref(
    node: dict[str, Any],
    defs: dict[str, Any],
    *,
    seen: tuple[str, ...],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Inline a local ``$ref``, refusing to follow a cycle.

    Returns the resolved node together with the updated set of definition names on the
    current path. The caller must thread that set into child conversions — otherwise a
    cycle (A contains B contains A) resets the path at every level and is only caught
    later by the depth guard, which then reports "too deeply nested" for what is really
    an unrepresentable recursive model.
    """
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node, seen
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        raise SchemaTranslationError(f"only local #/$defs refs are supported, got {ref!r}")
    name = ref[len(prefix) :]
    if name in seen:
        raise SchemaTranslationError(
            f"recursive model detected via {name!r}; Gemini's schema dialect cannot "
            "express recursion — break the cycle or serialise that field as a string"
        )
    target = defs.get(name)
    if target is None:
        raise SchemaTranslationError(f"dangling $ref: {ref!r}")

    # Keys alongside the $ref (a description on the field, say) win over the target's.
    merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return _resolve_ref(merged, defs, seen=(*seen, name))
