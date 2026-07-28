"""
Flyby AI — conversation intelligence.

Reads a chat thread and decides whether the people in it are planning a business
trip, extracting the destination, dates and purpose. This is what lets the chat
assistant say "looks like you're planning a trip to Denver — want me to set it
up?" from a real conversation instead of a scripted example.

Gemini does the understanding; the frontend fills in flight/hotel specifics from
its inventory (still simulated until Duffel). Returns None when there's no key
or no trip in the conversation, so the assistant simply stays quiet.
"""

from datetime import date

from api.gemini import call_gemini


def _system_prompt(today):
    return f"""You read a group chat or DM between coworkers and decide whether they are planning a business trip together, then extract the details.

Today's date is {today} (ISO). Resolve relative dates ("next Thursday", "the week of the 12th") against it; always return dates today or later.

Return ONLY a JSON object with EXACTLY these fields:

{{
  "detected": boolean,              // true only if a specific trip is actually being planned (a real destination is mentioned or clearly implied)
  "destination": string | null,     // city or place, plain name e.g. "Denver", "London". null if none.
  "startDate": "YYYY-MM-DD" | null,
  "endDate": "YYYY-MM-DD" | null,
  "travelers": number,              // how many people are going, from the conversation; default 1
  "purpose": string | null,         // short reason e.g. "Client summit", "Team offsite", "Conference"
  "confidence": number,             // 0-100, how sure you are this is a real trip being planned
  "reasoning": string               // one sentence: what in the conversation makes you think so, quoting a detail
}}

RULES:
- Set detected=false (and destination=null) for small talk, vague "we should travel sometime" musings, or chat with no place named. Be conservative — a false positive is worse than a miss.
- Only detected=true when there is a concrete destination.
- travelers reflects who's actually going ("me and Sarah" -> 2).
- Keep reasoning specific to the actual messages.
- Return ONLY valid JSON. No markdown, no commentary."""


def _transcript(messages):
    """Render the message list as a readable transcript for the model."""
    lines = []
    for message in messages[-25:]:  # recent context is enough; keeps the call cheap
        if not isinstance(message, dict):
            continue
        sender = str(message.get("sender") or message.get("senderName") or "Someone").strip()
        text = str(message.get("text") or message.get("content") or "").strip()
        if text:
            lines.append(f"{sender}: {text}")
    return "\n".join(lines)


def detect_trip(messages):
    """
    Detect a trip in a conversation, or return None.

    None means "nothing to suggest" — no key, no messages, or no real trip — so
    the assistant shows nothing rather than a guess.
    """
    if not isinstance(messages, list) or not messages:
        return None

    transcript = _transcript(messages)
    if not transcript.strip():
        return None

    parsed = call_gemini(
        _system_prompt(date.today().isoformat()),
        f"Conversation:\n{transcript}",
        max_output_tokens=1024,
        temperature=0.3,
    )
    if not isinstance(parsed, dict):
        return None

    if not parsed.get("detected") or not parsed.get("destination"):
        return None

    def clean_date(value):
        text = str(value).strip() if value else ""
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None

    def clamp_int(value, default, lo, hi):
        try:
            return max(lo, min(hi, int(round(float(value)))))
        except (TypeError, ValueError):
            return default

    return {
        "detected": True,
        "destination": str(parsed["destination"]).strip(),
        "startDate": clean_date(parsed.get("startDate")),
        "endDate": clean_date(parsed.get("endDate")),
        "travelers": clamp_int(parsed.get("travelers"), 1, 1, 20),
        "purpose": (str(parsed.get("purpose")).strip() if parsed.get("purpose") else None),
        "confidence": clamp_int(parsed.get("confidence"), 70, 0, 100),
        "reasoning": str(parsed.get("reasoning") or "").strip(),
    }
