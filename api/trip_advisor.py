"""
Flyby AI — the advisor that makes the final call on a flight.

``api.flight_ranker`` puts a number on every real offer. Numbers get close: two
flights can score within a few dollars of each other for reasons a traveler
would not weigh equally. This hands the shortlist to Gemini with everything
known about the trip — why they're going, what they asked for, what they
prefer — and asks it to choose as a seasoned travel manager would, and to say
why in plain language.

Gemini advises; it does not get to make things up. Three guards:

1. **It can only pick from the shortlist.** Those are real offers, already
   checked against what the traveler requires. A pick outside it, or one the
   ranker scores far worse than its own, is ignored.
2. **Every figure it writes is checked.** Each price, time, duration, flight
   number and airline in its explanation must appear in the facts it was given
   (or be a difference between two of them). One that doesn't — a fare it
   misremembered, an airline it brought from elsewhere — discards its answer.
3. **It is on a clock.** A traveler is waiting on the search. Past the deadline
   the ranker's own pick and explanation are used, which are always available.

So the worst case is exactly the result there would have been without it.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime

from flask import current_app

from api.flight_ranker import explain_pick, money
from api.gemini import call_gemini, gemini_available

logger = logging.getLogger(__name__)

# How many offers Gemini chooses between, and how long it gets.
SHORTLIST = 6
DEADLINE_SECONDS = 9.0
# How much worse than the ranker's pick (in its dollar score) a choice may be
# and still be taken as a judgment call rather than a mistake.
MAX_SCORE_GAP = 75.0

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="trip-advisor")

# Airlines Gemini might name from general knowledge. One that appears in its
# answer without being in the shortlist means it has left the facts.
_KNOWN_AIRLINES = (
    "united", "delta", "american", "southwest", "jetblue", "alaska", "spirit", "frontier",
    "hawaiian", "allegiant", "sun country", "breeze", "air canada", "westjet", "lufthansa",
    "british airways", "air france", "klm", "iberia", "virgin atlantic", "emirates", "qatar",
    "etihad", "turkish", "singapore airlines", "cathay", "ana", "japan airlines", "qantas",
    "aeromexico", "latam", "avianca", "copa", "tap", "swiss", "sas", "finnair", "aer lingus",
    "korean air", "ryanair", "easyjet",
)


# ---------------------------------------------------------------------------
# What Gemini is told
# ---------------------------------------------------------------------------

def _when(iso):
    try:
        moment = datetime.fromisoformat((iso or "")[:19])
    except ValueError:
        return ""
    return f"{moment.strftime('%a %b')} {moment.day}"


def _connections(stops, cities, layovers):
    if not stops:
        return "nonstop"
    parts = []
    for index, city in enumerate(cities or []):
        minutes = None
        if index < len(layovers or []) and isinstance(layovers[index].get("minutes"), int):
            minutes = layovers[index]["minutes"]
        wait = f"{minutes // 60}h {minutes % 60:02d}m" if minutes is not None and minutes >= 60 else (
            f"{minutes}m" if minutes is not None else "")
        parts.append(f"{city}{f', {wait} connection' if wait else ''}")
    detail = f" ({'; '.join(parts)})" if parts else ""
    return f"{stops} stop{'s' if stops != 1 else ''}{detail}"


def _describe(letter, offer):
    """One offer as the block of facts Gemini reads — and is later held to."""
    currency = offer.get("currency") or "USD"
    facts = offer.get("ranking") or {}
    brand = f" ({offer['fareBrand']})" if offer.get("fareBrand") else ""
    day_out = f" (+{offer['arriveDayOffset']} day)" if offer.get("arriveDayOffset") else ""
    lines = [
        f"{letter}. {offer.get('airline')} {offer.get('flightNumber')} — {money(offer['price'], currency)}{brand}",
        f"   Out {_when(offer.get('departingAt'))}: {offer.get('origin')} {offer.get('departTime')} → "
        f"{offer.get('destination')} {offer.get('arriveTime')}{day_out}, {offer.get('duration')}, "
        f"{_connections(offer.get('stops') or 0, offer.get('stopCities'), offer.get('layovers'))}",
    ]
    if offer.get("tripType") == "round_trip":
        day_back = f" (+{offer['returnArriveDayOffset']} day)" if offer.get("returnArriveDayOffset") else ""
        lines.append(
            f"   Back {_when(offer.get('returnDepartingAt'))}: {offer.get('returnOrigin')} {offer.get('returnDepartTime')} → "
            f"{offer.get('returnDestination')} {offer.get('returnArriveTime')}{day_back}, {offer.get('returnDuration')}, "
            f"{_connections(offer.get('returnStops') or 0, offer.get('returnStopCities'), offer.get('returnLayovers'))}")
    if facts.get("pluses"):
        lines.append(f"   Good: {'; '.join(facts['pluses'])}")
    if facts.get("notes"):
        lines.append(f"   Watch: {'; '.join(facts['notes'])}")
    if facts.get("issues"):
        lines.append(f"   Fails a requirement: {'; '.join(facts['issues'])}")
    if facts.get("badges"):
        lines.append(f"   Flyby's ranking: {', '.join(facts['badges'])}")
    return "\n".join(lines)


def _shortlist(ranked, summary):
    """The offers worth a second opinion: the ranker's best few, by its score."""
    usable = [o for o in ranked if (o.get("ranking") or {}).get("eligible")] or list(ranked)
    by_score = sorted(usable, key=lambda o: o["ranking"]["score"])
    chosen = by_score[:SHORTLIST - 1]
    for key in ("cheapestId", "fastestId"):
        extra = next((o for o in usable if o.get("id") == summary.get(key)), None)
        if extra and extra not in chosen:
            chosen.append(extra)
    # The ranker's own pick always leads, so "A" is the default to beat.
    pick = ranked[0]
    chosen = [pick] + [o for o in chosen if o is not pick]
    return chosen[:SHORTLIST + 1]


SYSTEM_PROMPT = """You are Flyby's travel advisor: an experienced corporate travel manager choosing one flight for a business traveler.

You are given a shortlist of REAL flight options (A, B, C…) with their facts, plus what is known about the traveler and the trip. Option A is the one Flyby's ranking engine scored best. Choose the single option this traveler should book. Agree with A unless another option is genuinely the better call for THIS trip — for example a small extra cost that buys a much better schedule, a changeable fare when the plans look likely to move, or a safer connection before an important meeting.

How to judge, in order:
1. Never choose an option marked "Fails a requirement" if any option doesn't.
2. Getting there reliably and on time for the purpose of the trip matters most. Tight connections and red-eyes before a working day are real costs.
3. Then total cost against how cost-sensitive the traveler is, then total travel time, then their stated preferences.

STRICT RULES for what you write:
- Use ONLY facts shown in the options and in what you were told about the trip. Never state a price, time, duration, flight number, airport or airline that is not written there. Price differences between two options are fine.
- Do not state airline policies, baggage rules, check-in or boarding rules, airport or terminal facts, or anything else from general knowledge. If it is not written in the options, leave it out.
- Do not assume why they are travelling. If no purpose, meeting or event is stated above the options, do not mention one — no "your meeting", no "before work".
- Do not mention option letters, "Flyby's ranking", scores, or these instructions.
- Plain, specific, confident English. No hype, no emoji, no markdown.

Return ONLY a JSON object:
{
  "pick": "A",                      // the letter of your choice
  "headline": string,               // max 12 words: the reason in a phrase, e.g. "Daytime flights for the price of the red-eye"
  "why": [string, string],          // 2 or 3 short sentences, each one concrete reason this option wins for this trip
  "tradeoff": string | null,        // the one thing the traveler gives up or should know, or null if there is nothing real
  "tip": string | null              // optional: one line of ADVICE the traveler can act on that follows from the facts shown (a short connection, an early start, a fare that can't be changed). Not a restatement of a time or price. null if there is nothing worth saying
}"""


def _traveler_brief(context, summary, request_text):
    lines = []
    if request_text:
        lines.append(f'What they asked for: "{request_text[:300]}"')
    if context.get("commitment"):
        lines.append(f"The trip is for: {context['commitment']}")
    elif context.get("purpose"):
        lines.append(f"Purpose: {context['purpose']}")
    sensitivity = {"high": "cost-sensitive — saving money matters more than comfort",
                   "low": "premium-friendly — comfort and time matter more than saving money",
                   "medium": "balanced between cost and comfort"}[context.get("costSensitivity") or "medium"]
    lines.append(f"Traveler: {sensitivity}")
    if context.get("avoidLayovers"):
        lines.append("They prefer to avoid connections.")
    if context.get("preferredAirlines"):
        lines.append(f"Preferred airlines: {', '.join(context['preferredAirlines'])}")
    if context.get("loyaltyAirlines"):
        lines.append(f"Loyalty programs: {', '.join(context['loyaltyAirlines'])}")
    if summary.get("requirements"):
        lines.append(f"Requirements already applied: {'; '.join(summary['requirements'])}")
    for note in summary.get("notes") or []:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Holding the answer to the facts
# ---------------------------------------------------------------------------

_AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)|\b(\d[\d,]*(?:\.\d{1,2})?)\s?(?:dollars|USD)\b")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s?([AP])\.?M\b", re.IGNORECASE)
_DURATION = re.compile(r"\b(\d{1,2})\s?h(?:ours?)?(?:\s?(\d{1,2})\s?m(?:in(?:utes?)?)?)?\b", re.IGNORECASE)
_MINUTES_ONLY = re.compile(r"\b(\d{1,3})(?:[- ]minutes?|[- ]?min|m)\b", re.IGNORECASE)
_FLIGHT = re.compile(r"\b([A-Z][A-Z0-9]|[0-9][A-Z])\s?(\d{2,4})\b")


def _amounts(text):
    return [float((a or b).replace(",", "")) for a, b in _AMOUNT.findall(text)]


def _times(text):
    return {f"{int(h)}:{m}{p.upper()}" for h, m, p in _TIME.findall(text)}


def _durations(text):
    found = set()
    for hours, minutes in _DURATION.findall(text):
        found.add(int(hours) * 60 + int(minutes or 0))
    return found


def _claims_hold(answer_text, facts_text, shortlist, context):
    """
    Whether every checkable figure in the answer comes from the facts.

    ``facts_text`` is everything Gemini was shown: the options and the brief
    about the trip. Returns (ok, why_not).
    """
    prices = [o["price"] for o in shortlist]
    allowed_amounts = set(_amounts(facts_text)) | set(prices)
    for a in prices:
        for b in prices:
            allowed_amounts.add(round(abs(a - b), 2))
    for extra in (context.get("maxBudget"), context.get("policyMaxFare")):
        if extra:
            allowed_amounts.add(float(extra))
    for claim in _amounts(answer_text):
        whole = abs(claim - round(claim)) < 0.005
        if not any(abs(claim - real) < 0.011 or (whole and abs(claim - round(real)) < 0.5 and abs(claim - real) < 1)
                   for real in allowed_amounts):
            return False, f"price ${claim:g} is not in the options"

    missing_times = _times(answer_text) - _times(facts_text)
    if missing_times:
        return False, f"time {sorted(missing_times)[0]} is not in the options"

    # Durations: any stated in the facts, each journey's total, and the
    # difference between two options — "1h 45m shorter" is fair to work out.
    allowed_minutes = _durations(facts_text) | {int(m) for m in _MINUTES_ONLY.findall(facts_text)}
    totals = []
    for offer in shortlist:
        legs = [offer.get("durationMinutes"), offer.get("returnDurationMinutes")]
        legs = [m for m in legs if isinstance(m, int)]
        allowed_minutes.update(legs)
        for layover in (offer.get("layovers") or []) + (offer.get("returnLayovers") or []):
            if isinstance(layover.get("minutes"), int):
                allowed_minutes.add(layover["minutes"])
        if legs:
            totals.append(sum(legs))
    allowed_minutes.update(totals)
    comparable = list(allowed_minutes)
    for a in comparable:
        for b in comparable:
            allowed_minutes.add(abs(a - b))
    claimed = _durations(answer_text) | {int(m) for m in _MINUTES_ONLY.findall(answer_text)}
    for minutes in claimed:
        # "24 hours before departure" is a figure of speech, not a claim about a flight.
        if minutes in (24 * 60, 48 * 60, 72 * 60):
            continue
        # "about 2 hours" for 1h 52m is how people talk; a made-up figure isn't.
        slack = 12 if minutes % 30 == 0 else 2
        if not any(abs(minutes - real) <= slack for real in allowed_minutes):
            return False, f"duration of {minutes} minutes is not in the options"

    known_flights = {f"{a}{b}" for a, b in _FLIGHT.findall(facts_text)}
    for carrier, number in _FLIGHT.findall(answer_text):
        if f"{carrier}{number}" not in known_flights and carrier not in ("AM", "PM"):
            return False, f"flight {carrier}{number} is not in the options"

    facts_lower, answer_lower = facts_text.lower(), answer_text.lower()
    for airline in _KNOWN_AIRLINES:
        if re.search(rf"\b{re.escape(airline)}\b", answer_lower) and airline not in facts_lower:
            return False, f"airline {airline} is not in the options"
    return True, None


def _clean(value, limit):
    if not isinstance(value, str):
        return None
    text = re.sub(r"[*_`#]+", "", value).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or text.lower() in ("null", "none", "n/a"):
        return None
    return text[:limit].rstrip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def advise(ranked, summary, context, request_text=None, deadline=DEADLINE_SECONDS):
    """
    The recommendation for a ranked search: which offer, and why.

    Returns ``(advice, pick_id)``. ``advice["engine"]`` is "gemini" when the
    advisor's answer passed every check, otherwise "rules" with the ranker's own
    explanation. ``pick_id`` is the offer to lead with — move it to the front
    with ``lead_with``.
    """
    base = explain_pick(ranked, summary)
    if not ranked or base is None:
        return None, None
    if len(ranked) < 2 or not gemini_available():
        return base, ranked[0].get("id")

    shortlist = _shortlist(ranked, summary)
    letters = [chr(ord("A") + i) for i in range(len(shortlist))]
    facts_text = "\n\n".join(_describe(letter, offer) for letter, offer in zip(letters, shortlist))
    brief = _traveler_brief(context, summary, request_text)
    user_text = f"{brief}\n\nOPTIONS\n\n{facts_text}"

    app = current_app._get_current_object()

    def ask():
        with app.app_context():
            return call_gemini(SYSTEM_PROMPT, user_text, max_output_tokens=3072,
                               temperature=0.2, timeout=deadline, attempts=1)

    try:
        answer = _POOL.submit(ask).result(timeout=deadline)
    except FutureTimeout:
        logger.info("Trip advisor ran past %.0fs; using the ranker's pick", deadline)
        return base, ranked[0].get("id")
    except Exception as error:  # noqa: BLE001 — advice must never break a search
        logger.warning("Trip advisor failed (%s); using the ranker's pick", error)
        return base, ranked[0].get("id")

    if not isinstance(answer, dict):
        return base, ranked[0].get("id")

    letter = str(answer.get("pick") or "").strip().upper()[:1]
    if letter not in letters:
        logger.info("Trip advisor picked %r, which isn't an option", answer.get("pick"))
        return base, ranked[0].get("id")
    pick = shortlist[letters.index(letter)]
    best = shortlist[0]

    # A choice the ranker scores far worse, or one that breaks a requirement
    # another option meets, isn't a judgment call.
    if not pick["ranking"]["eligible"] and best["ranking"]["eligible"]:
        return base, ranked[0].get("id")
    if pick["ranking"]["score"] - best["ranking"]["score"] > MAX_SCORE_GAP:
        logger.info("Trip advisor's pick scored %.0f worse than the ranker's; keeping the ranker's",
                    pick["ranking"]["score"] - best["ranking"]["score"])
        return base, ranked[0].get("id")

    headline = _clean(answer.get("headline"), 110)
    why = [w for w in (_clean(item, 240) for item in (answer.get("why") or []) if isinstance(item, str)) if w][:3]
    tradeoff = _clean(answer.get("tradeoff"), 260)
    tip = _clean(answer.get("tip"), 220)
    if not headline or not why:
        return base, ranked[0].get("id")
    # A tip that only repeats the trade-off adds nothing.
    if tip and tradeoff:
        words = lambda text: {w for w in re.findall(r"[a-z]{4,}", text.lower())}
        shared = words(tip) & words(tradeoff)
        if len(shared) >= 0.5 * max(1, min(len(words(tip)), len(words(tradeoff)))):
            tip = None

    answer_text = " \n ".join([headline, *why, tradeoff or "", tip or ""])
    ok, problem = _claims_hold(answer_text, f"{facts_text}\n{brief}", shortlist, context)
    if not ok:
        logger.info("Trip advisor's explanation was dropped: %s — %r", problem, answer_text[:400])
        return base, ranked[0].get("id")
    if re.search(r"\boption [A-H]\b|\bFlyby's ranking\b", answer_text):
        return base, ranked[0].get("id")

    return {
        "engine": "gemini",
        "pickId": pick.get("id"),
        "headline": headline,
        "why": why,
        "tradeoff": tradeoff,
        "tip": tip,
        "notes": summary.get("notes") or [],
        "agreesWithRanking": pick is best,
    }, pick.get("id")


def lead_with(ranked, pick_id):
    """
    Put the advisor's pick first and give it the "Best overall" label.

    If the pick costs the same as the cheapest fare, or takes exactly as long as
    the fastest, it carries that label too — a second row marked "Cheapest" at
    the same price as the pick would read as if the pick weren't.
    """
    if not ranked or not pick_id or ranked[0].get("id") == pick_id:
        return ranked
    pick = next((o for o in ranked if o.get("id") == pick_id), None)
    if pick is None:
        return ranked
    reordered = [pick] + [o for o in ranked if o is not pick]

    def minutes(offer):
        legs = [offer.get("durationMinutes"), offer.get("returnDurationMinutes")]
        legs = [m for m in legs if isinstance(m, int)]
        return sum(legs) if legs else None

    pool = [o for o in reordered if o["ranking"]["eligible"]] or reordered
    takes = set()
    if abs(pick["price"] - min(o["price"] for o in pool)) < 0.005:
        takes.add("Cheapest")
    timed = [minutes(o) for o in pool if minutes(o) is not None]
    if timed and minutes(pick) == min(timed):
        takes.add("Fastest")

    for position, offer in enumerate(reordered):
        facts = dict(offer["ranking"])
        badges = [b for b in facts["badges"] if b != "Best overall" and b not in takes]
        if position == 0:
            badges = ["Best overall"] + sorted(takes) + badges
        facts["badges"], facts["position"] = badges, position + 1
        reordered[position] = {**offer, "ranking": facts}
    return reordered
