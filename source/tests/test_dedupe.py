from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from agenticstudio.dedupe import build_dedupe_key, normalize_url
from agenticstudio.domain import Estimate, Opportunity, Problem, Risk, Signals, Source


def source(url: str = "https://example.com/item", external_id: str | None = None,
           name: str = "example") -> Source:
    return Source(name=name, url=url, external_id=external_id, risk_class="core")


def test_external_id_precedence_and_repeatability() -> None:
    key = build_dedupe_key(source(external_id="42"))
    assert key == build_dedupe_key(source("invalid URL", "42"))
    assert key.kind == "external_id"
    assert key.version == 1
    assert key.storage_key == f"dedupe:v1:external_id:sha256:{key.fingerprint}"
    assert len(key.fingerprint) == 64
    assert key.fingerprint == "db6b935c1b335a155111b5a656ae04266f4de438a570fa9a24dffb4d788e83ea"
    assert key != build_dedupe_key(source(external_id="43"))


@pytest.mark.parametrize("external_id", [None, "", " \t"])
def test_url_fallback(external_id: str | None) -> None:
    key = build_dedupe_key(source("HTTPS://EXAMPLE.COM:443/item#details", external_id))
    assert key == build_dedupe_key(source())
    assert key.kind == "url"


@pytest.mark.parametrize("url,expected", [
    ("HTTP://Example.COM:80", "http://example.com/"),
    ("https://Example.COM:443/a#part", "https://example.com/a"),
    ("https://example.com:8443/a/", "https://example.com:8443/a/"),
    ("https://[2001:DB8::1]:443", "https://[2001:db8::1]/"),
    ("https://example.com/%2f/%7E", "https://example.com/%2f/%7E"),
    ("https://example.com/?b=2&a=1&a=3&utm_source=x",
     "https://example.com/?b=2&a=1&a=3&utm_source=x"),
    ("https://example.com?", "https://example.com/?"),
    ("https://example.com#?fragment", "https://example.com/"),
])
def test_url_normalization_and_idempotence(url: str, expected: str) -> None:
    assert normalize_url(url) == expected
    assert normalize_url(expected) == expected


@pytest.mark.parametrize("url", [
    "", "/relative", "ftp://example.com/a", "https:///a", "https://user:pw@example.com",
    " https://example.com", "https://example.com/\na", "https://example.com/a b",
    "https://example.com:bad/a", "https://example.com:99999/a", "https://example.com:/",
    "https://[bad]/", "https://example.com/%GG", "https://example.com/%",
    "https://example.com\\other/a", "https://exa%6dple.com/",
])
def test_invalid_fallback_urls(url: str) -> None:
    with pytest.raises(ValueError):
        build_dedupe_key(source(url))


@pytest.mark.parametrize("other_url", [
    "http://example.com/item", "https://example.com/Item", "https://example.com/item/",
    "https://example.com/item?a=1", "https://example.com/item?",
])
def test_meaningful_url_distinctions(other_url: str) -> None:
    assert build_dedupe_key(source()) != build_dedupe_key(source(other_url))


def test_identity_namespaces_and_opaque_strings() -> None:
    assert build_dedupe_key(source()) != build_dedupe_key(source(name="other"))
    assert build_dedupe_key(source(external_id="42")) != build_dedupe_key(
        source(external_id="42", name="other")
    )
    assert build_dedupe_key(source(external_id="A")) != build_dedupe_key(source(external_id="a"))
    assert build_dedupe_key(source(external_id=" A ")) != build_dedupe_key(source(external_id="A"))
    assert build_dedupe_key(source()) != build_dedupe_key(source(external_id=source().url))
    assert build_dedupe_key(source(external_id="c", name="ab")) != build_dedupe_key(
        source(external_id="bc", name="a")
    )


def test_query_order_and_reserved_escape_distinctions() -> None:
    assert build_dedupe_key(source("https://example.com/?a=1&b=2")) != build_dedupe_key(
        source("https://example.com/?b=2&a=1")
    )
    assert build_dedupe_key(source("https://example.com/a%2Fb")) != build_dedupe_key(
        source("https://example.com/a/b")
    )


def test_key_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(build_dedupe_key(source()), "version", 2)


@pytest.mark.parametrize("version", [0, 2, True])
def test_unknown_versions_rejected(version: int) -> None:
    with pytest.raises(ValueError, match="unsupported dedupe version"):
        build_dedupe_key(source(), version=version)
    with pytest.raises(ValueError, match="unsupported dedupe version"):
        normalize_url(source().url, version=version)


@pytest.mark.parametrize("name", ["", " \t"])
def test_blank_source_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="source name"):
        build_dedupe_key(source(name=name))


def test_opportunity_id_is_not_identity() -> None:
    first = Opportunity(
        source=source(external_id="42"),
        problem=Problem(raw_text="Build a report", summary="Report"),
        signals=Signals(acceptance_clarity=100, strategic_reuse_value=0),
        risk=Risk(
            technical_uncertainty=0, scope_uncertainty=0, maintenance_burden=0,
            legal_or_platform_risk=0, abuse_risk=0, payment_or_dispute_risk=0,
        ),
        estimate=Estimate(delivery_probability=1.0, win_probability=0.0, confidence=100),
    )
    second = first.model_copy(update={"id": uuid4()})
    assert first.id != second.id
    assert build_dedupe_key(first.source) == build_dedupe_key(second.source)
