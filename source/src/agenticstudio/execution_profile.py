"""Coarse competition bands; profile fit is judged by Gemini triage (profile_fit)."""

LOW_COMPETITION = 30
HIGH_COMPETITION = 70


def competition_evidence(
    value: int | None, *, low: int = LOW_COMPETITION, high: int = HIGH_COMPETITION,
) -> str:
    if value is None:
        return "Observed competition band: unknown; no exact proposal count or win probability."
    band = ("LOW_COMPETITION" if value <= low else
            "HIGH_COMPETITION" if value >= high else "intermediate")
    return (f"Observed competition band: {band} ({value}/100 coarse canonical evidence); "
            "no exact proposal count or win probability.")
