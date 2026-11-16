"""Shared Gemini call and schema helpers for triage."""

import os
from typing import Any

DEFAULT_MODEL = "gemini-3.5-flash-lite"

_SUPPORTED_SCHEMA_KEYS = frozenset({
    "type", "title", "description", "properties", "required", "additionalProperties",
    "enum", "format", "minimum", "maximum", "items", "prefixItems", "minItems",
    "maxItems",
})


def _supported_schema(node: dict[str, Any]) -> dict[str, Any]:
    """Keep only Gemini's documented JSON Schema subset.

    Pydantic emits validation-only keywords such as defaults, regexes and string
    lengths. Application validation still enforces those after generation. Simple
    nullable fields are normalized from anyOf into Gemini's documented type array.
    """
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2:
        non_null = [part for part in any_of if part != {"type": "null"}]
        if len(non_null) == 1 and isinstance(non_null[0], dict):
            normalized = _supported_schema(non_null[0])
            base_type = normalized.get("type")
            if isinstance(base_type, str):
                normalized["type"] = [base_type, "null"]
                for key in ("title", "description"):
                    if key in node:
                        normalized[key] = node[key]
                return normalized
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _SUPPORTED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: _supported_schema(schema)
                           for name, schema in value.items() if isinstance(schema, dict)}
        elif key == "items" and isinstance(value, dict):
            result[key] = _supported_schema(value)
        elif key == "prefixItems" and isinstance(value, list):
            result[key] = [_supported_schema(item) for item in value if isinstance(item, dict)]
        else:
            result[key] = value
    return result


def _generate(model: str, evidence: str, schema: dict[str, Any], system: str,
              max_output_tokens: int = 2048) -> str:
    # Lazy import keeps disabled/missing-key runtime independent of SDK availability.
    from google import genai
    from google.genai import types

    with genai.Client(api_key=os.environ["GEMINI_API_KEY"], vertexai=False,
                      http_options=types.HttpOptions(
                          timeout=20_000, retry_options=types.HttpRetryOptions(attempts=1),
                      )) as client:
        response = client.models.generate_content(
            model=model, contents=evidence,
            config=types.GenerateContentConfig(
                system_instruction=system, response_mime_type="application/json",
                response_json_schema=schema, max_output_tokens=max_output_tokens, temperature=0,
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
            ),
        )
        if not response.candidates or len(response.candidates) != 1:
            raise ValueError("Unavailable semantic output")
        if str(response.candidates[0].finish_reason).split(".")[-1] != "STOP":
            raise ValueError("Incomplete semantic output")
        return response.text or ""
