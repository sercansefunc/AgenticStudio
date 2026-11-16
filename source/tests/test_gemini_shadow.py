"""Shared Gemini helpers: schema subset, SDK call configuration, spend guard."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from agenticstudio.domain import DomainModel
from agenticstudio.evaluation.gemini import _supported_schema
from agenticstudio.evaluation.shadow import reserved_batch

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}
AT = datetime(2026, 10, 7, tzinfo=UTC)


class Record(DomainModel):
    label: str = Field(default="unknown", pattern=r"\S", max_length=20)
    minutes: float | None = None


def test_schema_uses_only_documented_gemini_subset() -> None:
    schema = _supported_schema(Record.model_json_schema())
    forbidden = {"default", "pattern", "minLength", "maxLength", "anyOf"}
    for node in (schema, *schema["properties"].values()):
        assert not (forbidden & set(node))
    assert schema["properties"]["minutes"]["type"] == ["number", "null"]


def test_failure_reserved_and_corrupt_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret")
    calls = 0

    def fail(_: Any) -> list[Record]:
        nonlocal calls
        calls += 1
        raise RuntimeError("secret")

    def run() -> Record:
        return reserved_batch(directory=tmp_path, identities=["a"], at=AT, daily_cap=60,
                              fallbacks=[Record()], parse=Record.model_validate_json,
                              compute=fail, matches=lambda *_: True)[0]

    for _ in range(2):
        assert run() == Record()
    assert calls == 1
    for item in tmp_path.glob("item-*.json"):
        item.unlink()
    (tmp_path / "calls-2026-10-07.json").write_text("corrupt")
    assert run() == Record()
    assert calls == 1


@pytest.mark.parametrize("finish,text", [("STOP", "{}"), ("SAFETY", ""), ("MAX_TOKENS", "{}")])
def test_official_sdk_request_configuration(
    monkeypatch: pytest.MonkeyPatch, finish: str, text: str,
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    from agenticstudio.evaluation.gemini import _generate

    monkeypatch.setenv("GEMINI_API_KEY", "test-secret")
    captured: dict[str, Any] = {}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.models = self

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: Any) -> None:
            captured["closed"] = True

        def generate_content(self, **kwargs: Any) -> Any:
            captured["request"] = kwargs
            return SimpleNamespace(candidates=[SimpleNamespace(finish_reason=finish)], text=text)

    google = ModuleType("google")
    genai = ModuleType("google.genai")
    types = SimpleNamespace(HttpOptions=lambda **kw: kw, HttpRetryOptions=lambda **kw: kw,
                            GenerateContentConfig=lambda **kw: kw, ThinkingConfig=lambda **kw: kw)
    monkeypatch.setattr(google, "genai", genai, raising=False)
    monkeypatch.setattr(genai, "Client", Client, raising=False)
    monkeypatch.setattr(genai, "types", types, raising=False)
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    if finish == "STOP":
        assert _generate("gemini-3.5-flash-lite", "canonical", SCHEMA, "system") == text
    else:
        with pytest.raises(ValueError):
            _generate("gemini-3.5-flash-lite", "canonical", SCHEMA, "system")
    assert captured["http_options"] == {"timeout": 20_000, "retry_options": {"attempts": 1}}
    assert captured["vertexai"] is False and captured["closed"] is True
    config = captured["request"]["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_json_schema"] == SCHEMA
    assert config["max_output_tokens"] == 2048
    assert config["thinking_config"] == {"thinking_level": "minimal"}
    assert config["system_instruction"] == "system"
