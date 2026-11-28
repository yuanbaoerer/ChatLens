"""Small, dependency-free JSON Schema subset for generation outputs.

Supported keywords: type, enum, required, properties, additionalProperties,
items, minItems, maxItems, minLength, maxLength, pattern, minimum, maximum.
Unsupported JSON Schema features are rejected rather than silently ignored.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_JSON_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_SUPPORTED_KEYWORDS = frozenset({
    "type", "enum", "required", "properties", "additionalProperties", "items",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "minimum", "maximum",
    # Common annotations are accepted and intentionally ignored.
    "title", "description", "$schema", "$id",
})


class SchemaValidationError(ValueError):
    """Raised when a generation output does not match its declared schema."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise SchemaValidationError("$", f"unsupported schema type: {expected}")


def _validate_schema_keywords(schema: Mapping[str, Any]) -> None:
    unsupported = set(schema) - _SUPPORTED_KEYWORDS
    if unsupported:
        raise SchemaValidationError("$", f"unsupported schema keyword: {sorted(unsupported)[0]}")
    expected = schema.get("type")
    if isinstance(expected, str):
        if expected not in _JSON_TYPES:
            raise SchemaValidationError("$", f"unsupported schema type: {expected}")
    elif isinstance(expected, list):
        if not expected or any(not isinstance(item, str) or item not in _JSON_TYPES for item in expected):
            raise SchemaValidationError("$", "schema type list must contain supported types")
    elif expected is not None:
        raise SchemaValidationError("$", "schema type must be a string or list")
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
            raise SchemaValidationError("$", "required must be a list of non-empty strings")
    if "properties" in schema and not isinstance(schema["properties"], Mapping):
        raise SchemaValidationError("$", "properties must be an object")
    if "items" in schema and not isinstance(schema["items"], Mapping):
        raise SchemaValidationError("$", "items must be an object")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], (bool, Mapping)):
        raise SchemaValidationError("$", "additionalProperties must be a boolean or object")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise SchemaValidationError("$", "enum must be a list")
    if "pattern" in schema and not isinstance(schema["pattern"], str):
        raise SchemaValidationError("$", "pattern must be a string")


def validate_json_schema(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    """Validate ``value`` against the supported JSON Schema subset.

    Validation errors deliberately omit the offending value to avoid leaking
    model or chat content through diagnostics.
    """
    if not isinstance(schema, Mapping):
        raise SchemaValidationError(path, "schema must be an object")
    _validate_schema_keywords(schema)

    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else expected
        if not any(_type_matches(value, item) for item in expected_types):
            raise SchemaValidationError(path, "type mismatch")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(path, "value is not in enum")

    if isinstance(value, Mapping):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise SchemaValidationError(f"{path}.{key}", "required property is missing")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise SchemaValidationError(path, "properties must be an object")
        for key, child in value.items():
            if key in properties:
                validate_json_schema(child, properties[key], path=f"{path}.{key}")
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                raise SchemaValidationError(f"{path}.{key}", "additional property is not allowed")
            if isinstance(additional, Mapping):
                validate_json_schema(child, additional, path=f"{path}.{key}")
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaValidationError(path, "too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaValidationError(path, "too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_json_schema(item, schema["items"], path=f"{path}[{index}]")
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaValidationError(path, "string is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaValidationError(path, "string is too long")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise SchemaValidationError(path, "string does not match pattern")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(path, "number is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError(path, "number is above maximum")
