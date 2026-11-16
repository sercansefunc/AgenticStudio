"""Exact source identity, independent of internal Opportunity IDs or similarity.

Version 1 hashes a UTF-8 JSON array of [source name, identity kind, identity].
Source names and nonblank external IDs are opaque, case-sensitive strings, with
no trimming. Collectors must use a stable source namespace. Missing or blank
external IDs fall back to the normalized URL. An external ID appearing later
produces a different key; reconciliation is a caller concern.

Persist the complete storage_key (including version), not just the digest.
Future algorithms must introduce a new version and retain the old algorithm
for callers reading/recomputing old keys. Keys do not express cross-source
similarity, even when two sources have the same URL.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

__all__ = ["DEDUPE_VERSION", "DedupeKey", "SourceIdentity", "build_dedupe_key", "normalize_url"]

DEDUPE_VERSION = 1


class SourceIdentity(Protocol):
    """Structural input contract satisfied by the canonical domain Source."""

    @property
    def name(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def external_id(self) -> str | None: ...


@dataclass(frozen=True)
class DedupeKey:
    version: int
    kind: Literal["external_id", "url"]
    fingerprint: str

    @property
    def storage_key(self) -> str:
        """Namespaced, versioned SHA-256 key suitable for persistence."""
        return f"dedupe:v{self.version}:{self.kind}:sha256:{self.fingerprint}"


def _require_v1(version: int) -> None:
    if type(version) is not int or version != 1:
        raise ValueError("unsupported dedupe version")


def normalize_url(url: str, *, version: int = DEDUPE_VERSION) -> str:
    """Conservative HTTP(S) normalization, frozen as part of version 1.

    Lowercase scheme/host, remove default ports and fragments, and use '/' for
    an empty path. Preserve scheme distinctions, path case, trailing slashes,
    percent escapes, query ordering/duplicates/tracking parameters and a bare
    query delimiter. Do not guess which source-specific parameters identify
    a resource. Reject credentials, whitespace, malformed escapes and ports.
    """
    _require_v1(version)
    if not url or any(character.isspace() or ord(character) < 32
                      or ord(character) == 127 for character in url):
        raise ValueError("URL must be nonempty and contain no whitespace or controls")
    if "\\" in url or re.search(r"%(?![0-9a-fA-F]{2})", url):
        raise ValueError("URL contains a backslash or malformed percent escape")
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must be absolute HTTP(S) with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are not supported")
    host = parts.hostname.lower()
    if "%" in host or parts.netloc.endswith(":"):
        raise ValueError("URL contains an invalid host or empty port")
    if ":" in host:
        host = f"[{host}]"
    port = parts.port  # urllib validates the port and its range.
    if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    result = urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))
    if "?" in url.split("#", 1)[0] and not parts.query:
        result += "?"
    return result


def build_dedupe_key(source: SourceIdentity, *, version: int = DEDUPE_VERSION) -> DedupeKey:
    """Build a deterministic exact-source key without accepting OpportunityId.

    A strong external ID takes precedence even if the URL is invalid/changes.
    SHA-256 fingerprints use JSON framing to prevent ambiguous concatenation.
    """
    _require_v1(version)
    if not source.name or not source.name.strip():
        raise ValueError("source name must be nonblank")
    kind: Literal["external_id", "url"]
    if source.external_id is not None and source.external_id.strip():
        kind, identity = "external_id", source.external_id
    else:
        kind, identity = "url", normalize_url(source.url, version=version)
    payload = json.dumps([source.name, kind, identity], ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return DedupeKey(version=version, kind=kind, fingerprint=fingerprint)
