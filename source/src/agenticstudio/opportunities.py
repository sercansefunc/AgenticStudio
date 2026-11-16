"""Owner tool over stored opportunities: python -m agenticstudio.opportunities list|mark."""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from uuid import UUID

from agenticstudio.domain import Opportunity, Status
from agenticstudio.lifecycle import InvalidTransitionError, transition
from agenticstudio.persistence import SQLiteOpportunityStore

ACTIVE = ("new", "shortlisted", "investigating", "pursuing", "proposal_sent")


def _line(item: Opportunity, now: datetime) -> str:
    commercial = item.commercial
    budget = (f"{commercial.budget_min}-{commercial.budget_max} {commercial.currency}"
              if commercial.budget_min is not None else "budget unknown")
    age = f"{(now - item.posted_at).days}d" if item.posted_at else "age unknown"
    title = " ".join(item.problem.summary.split("\n")[0].split())[:80]
    return (f"{item.id} {item.decision.status:<13} {item.source.name:<10} "
            f"{commercial.pricing_model:<6} {budget}; "
            f"competition {item.signals.competition_estimate}; {age}; {title}\n"
            f"    {item.source.url}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="read-only; newest first")
    listing.add_argument("--source")
    listing.add_argument("--status", action="append", choices=get_args(Status),
                         help="default: all non-terminal statuses")
    listing.add_argument("--max-age-days", type=int)
    listing.add_argument("--limit", type=int, default=30)
    mark = commands.add_parser("mark", help="owner lifecycle decision, e.g. rejected to pass")
    mark.add_argument("id", type=UUID)
    mark.add_argument("status", choices=get_args(Status))
    args = parser.parse_args(argv)
    if args.command == "list" and args.limit < 1:
        parser.error("--limit must be at least 1")
    if not args.database.is_file():
        parser.error("--database does not exist")
    store = SQLiteOpportunityStore(args.database)
    try:
        with store.transaction() as repository:
            if args.command == "mark":
                item = repository.get(args.id)
                if item is None:
                    parser.error("unknown opportunity ID")
                try:
                    repository.save(transition(item, args.status))
                except InvalidTransitionError as error:
                    print(error, file=sys.stderr)
                    return 1
                print(f"{args.id}: {item.decision.status} -> {args.status}")
                return 0
            now = datetime.now(UTC)
            statuses = set(args.status or ACTIVE)
            rows = [
                item for item in repository.list_all()
                if item.decision.status in statuses
                and (not args.source or item.source.name == args.source)
                and (args.max_age_days is None or (
                    item.posted_at is not None
                    and (now - item.posted_at).days <= args.max_age_days))
            ]
            rows.sort(key=lambda item: (
                -(item.posted_at.timestamp() if item.posted_at else 0.0), str(item.id)))
            for item in rows[:args.limit]:
                print(_line(item, now))
            print(f"{min(len(rows), args.limit)} of {len(rows)} matching shown.")
            return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
