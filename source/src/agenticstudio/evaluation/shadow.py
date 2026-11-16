"""Local shadow sidecars and crash-safe spend reservations (Linux operator runtime)."""

import fcntl
import hashlib
import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from agenticstudio.domain import DomainModel

# Runaway guard only; flash-lite cost per call is negligible at this scale.
MAX_DAILY_CAP = 500


def _durable(path: Path, value: str) -> None:
    # Caller holds the shared lock. Atomic replace plus directory fsync survives crashes.
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def reserved_batch[T: DomainModel](
    *, directory: Path, identities: Sequence[str], at: datetime, daily_cap: int,
    fallbacks: Sequence[T], parse: Callable[[str], T],
    compute: Callable[[Sequence[int]], Sequence[T]], matches: Callable[[int, T], bool],
    batch_size: int = 1,
) -> list[T]:
    """Cache each item by evidence identity; uncached items go in batches, and each batch
    reserves one slot of the shared UTC-day cap before calling."""
    results = list(fallbacks)
    if not os.environ.get("GEMINI_API_KEY"):
        return results
    if type(daily_cap) is not int or not 0 <= daily_cap <= MAX_DAILY_CAP:
        return results
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / "shadow.lock").open("a") as lock:
            # Busy concurrent invocation fails closed instead of blocking daily reporting.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            items = [directory / f"item-{hashlib.sha256(i.encode()).hexdigest()}.json"
                     for i in identities]
            pending = []
            for index, item in enumerate(items):
                cached = parse(item.read_text()) if item.exists() else None
                if cached is not None and matches(index, cached):
                    results[index] = cached
                else:
                    pending.append(index)
            day = directory / f"calls-{at.astimezone(UTC).date()}.json"
            for start in range(0, len(pending), batch_size):
                chunk = pending[start:start + batch_size]
                calls = json.loads(day.read_text()) if day.exists() else 0
                if type(calls) is not int or not 0 <= calls < daily_cap:
                    return results
                # Reserve BEFORE calling. A failure consumes a slot; retry only on a later run.
                _durable(day, json.dumps(calls + 1))
                for index in chunk:
                    _durable(items[index], fallbacks[index].model_dump_json())
                computed = compute(chunk)
                if len(computed) != len(chunk):
                    continue
                for index, record in zip(chunk, computed, strict=True):
                    _durable(items[index], record.model_dump_json())
                    results[index] = record
            return results
    except Exception:
        # Storage/locking/corrupt state cannot bypass the guard or fail the daily run.
        return results
