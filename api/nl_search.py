"""
Flyby AI — natural-language travel search parsing.

Turns a free-text travel request ("3 days in Austin for SXSW under $2k, nonstop
from SF") into clean structured primitives the frontend can act on. Gemini does
the language understanding; the frontend's airport database resolves the city
names into airport codes, so each side does what it's best at.

The model only ever returns primitives — city *names*, ISO dates, counts and
enums — never airport codes, because the authoritative airport data lives in the
frontend. Today's date is passed in so relative dates ("next Monday", "this
weekend") resolve correctly.

Returns None when Gemini is unavailable, so the frontend falls back to its
built-in heuristic parser and search still works.
"""

from datetime import date

from api.gemini import call_gemini

CABIN_CLASSES = {"economy", "premium", "business", "first"}
TRIP_TYPES = {"roundtrip", "oneway", "multicity", "flexible"}
PURPOSES = {"business", "leisure", "flexible"}


def _system_prompt(today):
    return f"""You are Flyby AI's travel search parser. You read a person's free-text travel request and extract structured booking parameters.

Today's date is {today} (ISO). Resolve all relative dates ("next Monday", "this weekend", "in three weeks", "mid-August") against it, and always return dates that are today or later.

Return ONLY a JSON object with EXACTLY these fields (use null when the request doesn't specify one):

{{
  "destinationCity": string | null,   // the city or place they want to GO to, plain name e.g. "Austin", "Tokyo", "New York". Never an airport code.
  "originCity": string | null,        // where they depart FROM, plain city name, or null if not stated
  "departureDate": "YYYY-MM-DD" | null,
  "returnDate": "YYYY-MM-DD" | null,
  "durationDays": number | null,      // nights/days if a length is given but no explicit return date
  "flexibleDates": boolean,           // true if they said flexible/around/sometime/anytime
  "passengers": number,               // default 1; infer from "me and 2 colleagues" etc.
  "cabinClass": "economy" | "premium" | "business" | "first" | null,
  "tripType": "roundtrip" | "oneway" | "multicity" | "flexible",
  "purpose": "business" | "leisure" | "flexible",
  "maxBudget": number | null,         // total budget in USD if a cap is given ("under $2k" -> 2000)
  "preferNonstop": boolean,           // true if they asked for nonstop/direct
  "interpretation": string            // one concise human sentence summarizing what you understood
}}

RULES:
- destinationCity is the single most important field. Extract it even from terse input ("SXSW Austin", "get me to london next week").
- For "X to Y", X is origin and Y is destination.
- If they give a duration but no return date, set durationDays and leave returnDate null.
- Default tripType to "roundtrip", purpose to "business" for work/meeting/conference/client language, else "flexible".
- Never invent a destination that wasn't implied. If there is genuinely no destination, set destinationCity to null.
- Return ONLY valid JSON. No markdown, no commentary."""


def _coerce(parsed):
    """Validate and normalize the model's output into a safe primitives dict."""
    if not isinstance(parsed, dict):
        return None

    def clean_str(value):
        text = str(value).strip() if value is not None else ""
        return text or None

    def clean_enum(value, allowed, default=None):
        text = (str(value).strip().lower() if value is not None else "")
        return text if text in allowed else default

    def clean_int(value, default=None, lo=1, hi=9):
        try:
            return max(lo, min(hi, int(round(float(value)))))
        except (TypeError, ValueError):
            return default

    def clean_num(value):
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    def clean_date(value):
        text = clean_str(value)
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None

    return {
        "destinationCity": clean_str(parsed.get("destinationCity")),
        "originCity": clean_str(parsed.get("originCity")),
        "departureDate": clean_date(parsed.get("departureDate")),
        "returnDate": clean_date(parsed.get("returnDate")),
        "durationDays": clean_int(parsed.get("durationDays"), default=None, lo=1, hi=90),
        "flexibleDates": bool(parsed.get("flexibleDates")),
        "passengers": clean_int(parsed.get("passengers"), default=1),
        "cabinClass": clean_enum(parsed.get("cabinClass"), CABIN_CLASSES),
        "tripType": clean_enum(parsed.get("tripType"), TRIP_TYPES, default="roundtrip"),
        "purpose": clean_enum(parsed.get("purpose"), PURPOSES, default="flexible"),
        "maxBudget": clean_num(parsed.get("maxBudget")),
        "preferNonstop": bool(parsed.get("preferNonstop")),
        "interpretation": clean_str(parsed.get("interpretation")) or "",
    }


def parse_search(text):
    """
    Parse a free-text travel request into primitives, or None if unavailable.

    Returns None when Gemini isn't configured/reachable or produced nothing
    usable — the caller (and the frontend) then falls back to the heuristic.
    """
    text = (text or "").strip()
    if not text:
        return None

    parsed = call_gemini(
        _system_prompt(date.today().isoformat()),
        f'Travel request: "{text}"',
        max_output_tokens=1024,
        temperature=0.3,
    )
    if parsed is None:
        return None

    result = _coerce(parsed)
    # A parse with no destination is no better than the heuristic; let the
    # frontend fall back rather than returning an empty shell.
    if not result or not result.get("destinationCity"):
        return None
    return result
