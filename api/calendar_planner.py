"""
Flyby AI — reading a calendar for trips.

A connected calendar is a list of everything: stand-ups, video calls, a
dentist, a flight someone already booked, and — somewhere in there — the client
meeting in Chicago that needs a plane ticket. This picks those out.

The rule it replaces looked only at an event's Location box and asked whether
it resolved to an airport. That misses the meeting whose place is in its title
("Acme QBR — Chicago"), books a trip to a conference room in the traveler's own
city, and reads "Flight to Denver" — a trip that is already booked — as a
meeting to book travel for. Telling these apart is a reading task, so Gemini
does the reading.

What Gemini decides, per event: whether the traveler has to *physically go*
somewhere outside their home area, which city, and what the event is. What it
does **not** decide: dates and times (those are the calendar's, used as they
are), which airport (the app's airport database), or anything about flights.
Travel days, grouping meetings into one trip and the search itself are done in
ordinary code from the calendar's own data.

Its answers are checked: an event number it didn't get is ignored, a city is
only marked as *stated* if the event's own text contains it (otherwise it is
shown to the traveler as Flyby's inference), and anything malformed is dropped.

Only what is needed leaves the server: title, location, a short excerpt of the
description, and start and end times. Attendee addresses are never sent.

When Gemini is unavailable this returns None and the app uses its built-in
location rule, exactly as before.
"""

import re
from datetime import date

from api.gemini import call_gemini

MAX_EVENTS = 40
KINDS = {"meeting", "conference", "offsite", "personal", "travel_booking", "virtual", "local", "other"}


def _system_prompt(today, home):
    based = f"The traveler is based in {home}." if home else "The traveler's home city is not known."
    return f"""You read a business traveler's calendar and decide which events need a trip.

Today is {today}. {based}

You are given numbered events. For EACH one, decide whether the traveler must physically travel to another city to attend — far enough that they would book a flight.

Return ONLY a JSON object: {{"events": [ ... ]}} with one entry per event, in the same order:
{{
  "n": number,                 // the event's number
  "travel": boolean,           // true only if attending means being in person in a city outside the traveler's home area
  "city": string | null,       // the city to travel to, plain name ("Chicago", "London"). null when travel is false and no place is involved
  "cityStated": boolean,       // true if that city's name is written in the event; false if you inferred it from a venue or company name
  "kind": "meeting" | "conference" | "offsite" | "personal" | "travel_booking" | "virtual" | "local" | "other",
  "purpose": string,           // what the event is, in a few words, cleaned up ("Acme quarterly review", "Dreamforce")
  "confidence": number,        // 0 to 1: how sure you are about "travel"
  "why": string                // one short plain sentence explaining the decision
}}

RULES
- Video and phone meetings are never travel, even if a city is mentioned: Zoom, Teams, Google Meet, Webex, a dial-in number, "virtual", "remote", "online", "webinar". kind "virtual".
- An event that is itself a travel booking is NOT a meeting to book travel for: "Flight to Denver", "AA 1234 SFO-ORD", "Hotel: Marriott Chicago", "Rental car pickup", a train or airline confirmation. Set travel false, kind "travel_booking", and put the destination city in "city" when it is clear.
- An event in or near the traveler's home city is not travel. kind "local".
- Holidays, birthdays, reminders, out-of-office blocks, focus time, deadlines and all-day notes are not travel unless they clearly name going somewhere.
- A personal event in another city (a wedding, a family visit) IS travel; use kind "personal".
- Only use a city you can actually justify. If the event names a city, use it and set cityStated true. If it names only a venue or company, use its city ONLY when that is unambiguous and widely known (for example "Moscone Center" is in San Francisco) and set cityStated false. If you cannot tell where it is, set travel false and say so in "why" — never guess.
- Judge every event on its own. If several events are in the same city on nearby days, mark EACH of them as travel — combining them into one trip is done afterwards, not by you. The same goes when a flight or hotel for that city is already on the calendar: the meeting itself is still travel.
- When unsure whether an event is in person, set travel false with a low confidence rather than true.
- Return an entry for every event number you were given. No markdown, no commentary."""


def _clip(value, limit):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _sanitize(events):
    """The fields worth sending, bounded. Anything else in the request is ignored."""
    clean = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        identifier = _clip(event.get("id"), 300)
        title = _clip(event.get("title"), 160)
        if not identifier or not title:
            continue
        clean.append({
            "id": identifier,
            "title": title,
            "location": _clip(event.get("location"), 200),
            "description": _clip(event.get("description"), 300),
            "start": _clip(event.get("start"), 40),
            "end": _clip(event.get("end"), 40),
            "allDay": bool(event.get("allDay")),
            "attendeeCount": event.get("attendeeCount") if isinstance(event.get("attendeeCount"), int) else None,
        })
        if len(clean) >= MAX_EVENTS:
            break
    return clean


def _render(events):
    lines = []
    for number, event in enumerate(events, start=1):
        when = event["start"] + (f" to {event['end']}" if event["end"] else "")
        if event["allDay"]:
            when += " (all day)"
        parts = [f"{number}. {event['title']}", f"   When: {when or 'unknown'}"]
        parts.append(f"   Location: {event['location'] or '(none given)'}")
        if event["description"]:
            parts.append(f"   Notes: {event['description']}")
        if event["attendeeCount"]:
            parts.append(f"   Attendees: {event['attendeeCount']}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def _checked(answer, events):
    """Gemini's answer, with only the entries that hold up."""
    rows = answer.get("events") if isinstance(answer, dict) else None
    if not isinstance(rows, list):
        return None
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            number = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= len(events) or number in out:
            continue
        event = events[number - 1]

        city = _clip(row.get("city"), 60) or None
        if city and not re.fullmatch(r"[A-Za-zÀ-ÿ .'\-]{2,60}", city):
            city = None
        travel = row.get("travel") is True and bool(city)
        kind = str(row.get("kind") or "").strip().lower()
        if kind not in KINDS:
            kind = "meeting" if travel else "other"
        # Travel bookings and video calls are never something to book a trip for,
        # whatever else the answer says.
        if kind in ("travel_booking", "virtual", "local"):
            travel = False
        try:
            confidence = max(0.0, min(1.0, float(row.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.5

        # "Stated" has to be true of the event itself, not of the answer.
        text = f"{event['title']} {event['location']} {event['description']}".lower()
        stated = bool(city) and city.lower() in text

        out[event["id"]] = {
            "id": event["id"],
            "travel": travel,
            "city": city,
            "cityStated": stated,
            "kind": kind,
            "purpose": _clip(row.get("purpose"), 80) or event["title"][:80],
            "confidence": round(confidence, 2),
            "why": _clip(row.get("why"), 180),
        }
    return list(out.values()) or None


def assess_events(events, home=None):
    """
    Decide which calendar events need a trip.

    Returns ``(assessments, engine)``. ``assessments`` is a list of
    ``{id, travel, city, cityStated, kind, purpose, confidence, why}`` for the
    events Gemini answered about, or None when it couldn't be asked — the app
    then falls back to its own rule. ``engine`` is "gemini" or "fallback".
    """
    clean = _sanitize(events)
    if not clean:
        return [], "gemini"

    answer = call_gemini(
        _system_prompt(date.today().isoformat(), _clip(home, 80)),
        _render(clean),
        max_output_tokens=8192,
        temperature=0.1,
        timeout=25,
    )
    checked = _checked(answer, clean) if answer is not None else None
    if checked is None:
        return None, "fallback"
    return checked, "gemini"
