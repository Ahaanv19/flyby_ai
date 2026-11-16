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

from datetime import date, datetime, timedelta

from api.gemini import call_gemini

CABIN_CLASSES = {"economy", "premium", "business", "first"}
TRIP_TYPES = {"roundtrip", "oneway", "multicity", "flexible"}
PURPOSES = {"business", "leisure", "flexible"}
TIME_WINDOWS = {"early_morning", "morning", "afternoon", "evening", "night"}


def _system_prompt(today):
    return f"""You are Flyby AI's travel search parser. You read a person's free-text travel request and extract structured booking parameters.

Today's date is {today} (ISO). Resolve all relative dates ("next Monday", "this weekend", "in three weeks", "mid-August") against it, and always return dates that are today or later.

Return ONLY a JSON object with EXACTLY these fields (use null when the request doesn't specify one):

{{
  "destinationCity": string | null,   // the city or place they want to GO to, plain name e.g. "Austin", "Tokyo", "New York". Never an airport code.
  "originCity": string | null,        // where they depart FROM, plain city name, or null if not stated
  "departureDate": "YYYY-MM-DD" | null,
  "returnDate": "YYYY-MM-DD" | null,
  "departureDateStated": boolean,     // true ONLY if they said which day to fly out ("leaving Monday", "Dec 8 to Dec 10"); false if you worked it out from a meeting or event
  "returnDateStated": boolean,        // true ONLY if they said which day to come back ("back Wednesday"); false if you worked it out
  "durationDays": number | null,      // nights/days if a length is given but no explicit return date
  "flexibleDates": boolean,           // true if they said flexible/around/sometime/anytime
  "passengers": number,               // default 1; infer from "me and 2 colleagues" etc.
  "cabinClass": "economy" | "premium" | "business" | "first" | null,
  "tripType": "roundtrip" | "oneway" | "multicity" | "flexible",
  "purpose": "business" | "leisure" | "flexible",
  "maxBudget": number | null,         // total budget in USD if a cap is given ("under $2k" -> 2000)
  "preferNonstop": boolean,           // true if they asked for nonstop/direct
  "maxStops": number | null,          // 0 for "nonstop only", 1 for "one stop max"; null if not stated
  "departWindow": "early_morning" | "morning" | "afternoon" | "evening" | "night" | null,   // when they want to FLY OUT ("morning flight", "after work" -> evening)
  "returnWindow": "early_morning" | "morning" | "afternoon" | "evening" | "night" | null,   // when they want to fly BACK
  "arriveBy": "YYYY-MM-DDTHH:MM" | null,     // local time at the DESTINATION they must BE there by: a meeting, event or dinner start ("3pm board meeting Tuesday")
  "leaveAfter": "YYYY-MM-DDTHH:MM" | null,   // local time at the destination their commitment ENDS, before which they can't leave ("done at 4pm Thursday")
  "avoidRedEye": boolean | null,      // true if they said no red-eyes/overnight flights; false if they said a red-eye is fine or asked for one; null if not mentioned
  "airline": string | null,           // an airline they asked to fly for this trip ("on Delta"), plain name; null if none
  "commitment": string | null,        // what they are going for, in a few words, if stated ("3 PM board meeting", "SXSW", "client dinner")
  "interpretation": string            // one concise human sentence summarizing what you understood
}}

RULES:
- destinationCity is the single most important field. Extract it even from terse input ("SXSW Austin", "get me to london next week").
- For "X to Y", X is origin and Y is destination.
- If they give a duration but no return date, set durationDays and leave returnDate null.
- Default tripType to "roundtrip", purpose to "business" for work/meeting/conference/client language, else "flexible".
- Never invent a destination that wasn't implied. If there is genuinely no destination, set destinationCity to null.
- arriveBy and leaveAfter are only for a stated commitment with a clock time. Never guess a time that wasn't given.
- When they give a commitment but not travel dates, still fill departureDate and returnDate with your best estimate and set departureDateStated / returnDateStated to false.
- "nonstop" / "direct" sets preferNonstop true AND maxStops 0.
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

    def clean_moment(value):
        """A local date-time, to the minute — or None if it isn't one."""
        text = clean_str(value)
        if not text or "T" not in text:
            return None
        try:
            return datetime.fromisoformat(text[:16]).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            return None

    def clean_flag(value):
        return value if isinstance(value, bool) else None

    max_stops = clean_int(parsed.get("maxStops"), default=None, lo=0, hi=3)
    if max_stops is None and bool(parsed.get("preferNonstop")):
        max_stops = 0

    return {
        "departureDateStated": bool(parsed.get("departureDateStated")),
        "returnDateStated": bool(parsed.get("returnDateStated")),
        "maxStops": max_stops,
        "departWindow": clean_enum(parsed.get("departWindow"), TIME_WINDOWS),
        "returnWindow": clean_enum(parsed.get("returnWindow"), TIME_WINDOWS),
        "arriveBy": clean_moment(parsed.get("arriveBy")),
        "leaveAfter": clean_moment(parsed.get("leaveAfter")),
        "avoidRedEye": clean_flag(parsed.get("avoidRedEye")),
        "airline": (clean_str(parsed.get("airline")) or "")[:60] or None,
        "commitment": (clean_str(parsed.get("commitment")) or "")[:120] or None,
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


def _travel_days_around_commitment(result):
    """
    Pick the travel days for a commitment when the traveler didn't name them.

    "I have a 9am pitch in Austin on the 3rd" says when to be there, not when to
    fly. The rule is the one a travel manager uses, and it is applied here
    rather than left to the model so it is the same every time: a commitment
    before noon means flying in the day before; one that ends by 4 PM means
    home the same evening, otherwise the next day. Days the traveler stated are
    never touched.
    """
    def moment(key):
        try:
            return datetime.fromisoformat(result[key]) if result.get(key) else None
        except ValueError:
            return None

    starts, ends = moment("arriveBy"), moment("leaveAfter")
    if starts and not result.get("departureDateStated"):
        fly_out = starts.date() - timedelta(days=1) if starts.hour < 12 else starts.date()
        result["departureDate"] = max(fly_out, date.today()).isoformat()
    if result.get("tripType") == "oneway" or result.get("returnDateStated"):
        return
    if ends:
        home = ends.date() if ends.hour <= 16 else ends.date() + timedelta(days=1)
        result["returnDate"] = home.isoformat()
    elif starts and not result.get("returnDate"):
        # A start time and nothing else: assume it runs into the evening.
        result["returnDate"] = (starts.date() + timedelta(days=1)).isoformat()


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
        max_output_tokens=2048,
        temperature=0.2,
    )
    if parsed is None:
        return None

    result = _coerce(parsed)
    # A parse with no destination is no better than the heuristic; let the
    # frontend fall back rather than returning an empty shell.
    if not result or not result.get("destinationCity"):
        return None

    _travel_days_around_commitment(result)

    # A commitment in the past, or one that contradicts the travel dates, would
    # mark every flight as arriving too late. Drop it rather than apply it.
    today = date.today().isoformat()
    departure, back = result.get("departureDate"), result.get("returnDate")
    arrive_by, leave_after = result.get("arriveBy"), result.get("leaveAfter")
    if arrive_by and (arrive_by[:10] < today or (departure and arrive_by[:10] < departure)
                      or (back and arrive_by[:10] > back)):
        result["arriveBy"] = None
    if leave_after and (leave_after[:10] < today or (departure and leave_after[:10] < departure)
                        or (back and leave_after[:10] > back)):
        result["leaveAfter"] = None
    return result
