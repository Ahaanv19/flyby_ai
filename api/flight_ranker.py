"""
Flyby AI — choosing between real flights.

The airlines return dozens of fares for a route. This decides which one a
business traveler should actually take, and says why.

Every offer is priced in one unit — dollars — so unlike things can be weighed
against each other: the fare itself, plus what the journey costs the traveler in
time, connections, a night lost to a red-eye, a 5 AM alarm. The lowest total
wins. That is how a careful person compares flights anyway ("it's $40 more but
it's nonstop and I get a night's sleep"); this just does it for every offer,
the same way, every time.

Two kinds of input shape the result:

* **What must be true** — things the traveler asked for or the trip requires:
  land before the meeting, leave after it ends, stay under a budget, nonstop
  only, within the company's fare limit. An offer that breaks one is kept but
  marked, and ranked below every offer that doesn't. If *no* offer can satisfy
  a requirement (there is no nonstop on the route), that requirement is dropped
  and said so, rather than marking everything as failing.

* **What is preferred** — cost sensitivity, saved airlines, loyalty programs,
  a morning departure. These move the price of comfort up or down.

Nothing here invents or alters a flight: every number in the output comes from
the offer it describes. ``rank_offers`` only orders and annotates.
"""

from datetime import datetime, timedelta

# How much an hour of the traveler's time is worth against the fare, in USD.
VALUE_OF_HOUR = 30.0
# What a connection costs beyond the time it takes: the risk and the hassle.
STOP_COST = 60.0
# Asking to avoid connections makes each one close to disqualifying.
STOP_COST_AVOIDING = 260.0
# A night on a plane before a working day.
OVERNIGHT_COST = 90.0
OVERNIGHT_COST_AVOIDING = 320.0
# Leaving before 6 AM (a 3–4 AM alarm) or landing after 11:30 PM.
EARLY_DEPARTURE_COST = 35.0
LATE_ARRIVAL_COST = 30.0
# A connection short enough that a small delay misses it.
TIGHT_CONNECTION_MINUTES = 45
TIGHT_CONNECTION_COST = 70.0
LONG_LAYOVER_MINUTES = 240
# Coming home to a different airport than the one the car is parked at.
DIFFERENT_RETURN_AIRPORT_COST = 150.0
# Outside the time of day that was asked for.
WRONG_WINDOW_COST = 120.0
# How much a preference is worth.
# A ticket that can be changed when the meeting moves, or refunded if it's
# called off; and a fare that doesn't include the bag a work trip needs.
CHANGEABLE_BONUS = 25.0
FREE_CHANGE_BONUS = 40.0
REFUNDABLE_BONUS = 20.0
NO_CARRY_ON_COST = 45.0
PREFERRED_AIRLINE_BONUS = 60.0
LOYALTY_BONUS = 35.0
REQUESTED_AIRLINE_BONUS = 400.0

# Cost-sensitive travelers put up with more to save money; premium travelers
# pay more to be comfortable. Scales every comfort cost above, never the fare.
COMFORT_WEIGHT = {"high": 0.55, "medium": 1.0, "low": 1.7}

# From landing to being in the room, and from leaving the room to the gate.
ARRIVAL_BUFFER_MINUTES = 75
DEPARTURE_BUFFER_MINUTES = 120

WINDOWS = {
    "early_morning": (4, 8),
    "morning": (6, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "night": (20, 28),  # 8 PM to 4 AM
}
WINDOW_LABEL = {
    "early_morning": "an early-morning", "morning": "a morning", "afternoon": "an afternoon",
    "evening": "an evening", "night": "a late-night",
}

CABIN_ORDER = ["economy", "premium_economy", "business", "first"]


# ---------------------------------------------------------------------------
# Reading the request
# ---------------------------------------------------------------------------

def _parse_dt(value):
    """An ISO date-time as a naive local datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip()[:19])
    except ValueError:
        return None


def _number(value, low=0.0, high=1e6):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if low < number < high else None


def _names(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()][:12]


def build_context(raw=None, *, preferences=None, loyalty=None, policy=None):
    """
    One clean set of ranking inputs from the request, the traveler's saved
    preferences and their company's policy.

    ``raw`` comes from the client and is treated as untrusted: every field is
    type-checked and bounded. The policy is never read from it — a client can't
    grant itself a higher fare limit.
    """
    raw = raw if isinstance(raw, dict) else {}
    preferences = preferences or {}
    policy = policy or {}

    sensitivity = str(preferences.get("cost_sensitivity") or raw.get("costSensitivity") or "").lower()
    sensitivity = {"balanced": "medium", "moderate": "medium"}.get(sensitivity, sensitivity)
    if sensitivity not in COMFORT_WEIGHT:
        sensitivity = "medium"

    def window(key):
        value = str(raw.get(key) or "").strip().lower()
        return value if value in WINDOWS else None

    max_stops = raw.get("maxStops")
    try:
        max_stops = int(max_stops) if max_stops is not None else None
    except (TypeError, ValueError):
        max_stops = None
    if max_stops is not None and not 0 <= max_stops <= 3:
        max_stops = None
    if max_stops is None and raw.get("preferNonstop") is True:
        max_stops = 0

    avoid_red_eye = raw.get("avoidRedEye")
    preferred = _names(preferences.get("preferred_airlines")) or _names(raw.get("preferredAirlines"))

    return {
        "costSensitivity": sensitivity,
        "avoidLayovers": bool(preferences.get("avoid_layovers") or raw.get("avoidLayovers")),
        "preferredAirlines": preferred,
        "loyaltyAirlines": _names(loyalty),
        "requestedAirline": str(raw.get("airline") or "").strip()[:60] or None,
        "maxStops": max_stops,
        "maxBudget": _number(raw.get("maxBudget")),
        "departWindow": window("departWindow"),
        "returnWindow": window("returnWindow"),
        "arriveBy": _parse_dt(raw.get("arriveBy")),
        "leaveAfter": _parse_dt(raw.get("leaveAfter")),
        "avoidRedEye": avoid_red_eye if isinstance(avoid_red_eye, bool) else None,
        "policyMaxFare": _number(policy.get("max_flight_price")),
        "policyMaxClass": str(policy.get("max_flight_class") or "").strip().lower() or None,
        "purpose": str(raw.get("purpose") or "").strip()[:120] or None,
        "commitment": str(raw.get("commitment") or "").strip()[:120] or None,
    }


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def money(amount, currency="USD"):
    """$412 / $412.38 — never rounded to something the airline didn't quote."""
    symbol = "$" if (currency or "USD").upper() == "USD" else f"{currency} "
    return f"{symbol}{amount:,.0f}" if abs(amount - round(amount)) < 0.005 else f"{symbol}{amount:,.2f}"


def clock(moment):
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def span(minutes):
    """135 -> '2h 15m', 40 -> '40 minutes', 120 -> '2 hours'."""
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {rest:02d}m"


def _sentence(text):
    """Capital first letter, the rest untouched — airport codes stay upper-case."""
    text = text.strip()
    return text[:1].upper() + text[1:]


def _day(moment):
    return f"{moment.strftime('%a %b')} {moment.day}"


def _matches(airline_name, airline_code, wanted):
    """"Delta" matches "Delta Air Lines"; "UA" matches the code."""
    name = (airline_name or "").lower()
    code = (airline_code or "").lower()
    for item in wanted:
        needle = item.lower()
        if not needle:
            continue
        if needle == code or needle in name or name in needle:
            return item
        first = needle.split()[0]
        if len(first) >= 4 and first in name:
            return item
    return None


# ---------------------------------------------------------------------------
# One direction of travel
# ---------------------------------------------------------------------------

def _leg(offer, prefix=""):
    """The facts about one direction, read once."""
    def field(name):
        key = f"{prefix}{name[0].upper()}{name[1:]}" if prefix else name
        return offer.get(key)

    departs = _parse_dt(field("departingAt"))
    arrives = _parse_dt(field("arrivingAt"))
    minutes = field("durationMinutes")
    if not isinstance(minutes, int) or minutes <= 0:
        minutes = None
    return {
        "departs": departs,
        "arrives": arrives,
        "minutes": minutes,
        "stops": int(field("stops") or 0),
        "layovers": [l for l in (field("layovers") or []) if isinstance(l, dict)],
        "origin": field("origin") or "",
        "destination": field("destination") or "",
    }


def _overnight(leg):
    """Whether the traveler spends the night on this flight."""
    departs, arrives = leg["departs"], leg["arrives"]
    if not departs or not arrives:
        return False
    if departs.hour < 5:
        return True
    # Landing a little after midnight is a late arrival, not a night in the air.
    return arrives.date() > departs.date() and arrives.hour >= 4


def _in_window(moment, name):
    low, high = WINDOWS[name]
    hour = moment.hour + moment.minute / 60
    return low <= hour < high or low <= hour + 24 < high


def _comfort_costs(leg, direction, context, weight, notes):
    """Dollar cost of this direction's discomforts; appends what they are."""
    cost = 0.0
    departs, arrives = leg["departs"], leg["arrives"]
    label = "" if direction == "out" else "Return "

    per_stop = STOP_COST_AVOIDING if (context["avoidLayovers"] or context["maxStops"] == 0) else STOP_COST
    cost += leg["stops"] * per_stop * weight

    for layover in leg["layovers"]:
        minutes, airport = layover.get("minutes"), layover.get("airport") or "the connection"
        if not isinstance(minutes, int):
            continue
        if minutes < TIGHT_CONNECTION_MINUTES:
            cost += TIGHT_CONNECTION_COST * weight
            notes.append(_sentence(f"{label}tight {minutes}-minute connection in {airport}"))
        elif minutes >= LONG_LAYOVER_MINUTES:
            notes.append(f"{span(minutes)} layover in {airport}" + (" on the way back" if direction == "back" else ""))

    if not departs or not arrives:
        return cost

    if _overnight(leg):
        if context["avoidRedEye"] is True:
            cost += OVERNIGHT_COST_AVOIDING * weight
        elif context["avoidRedEye"] is None:
            cost += OVERNIGHT_COST * weight
        kind = "Red-eye" if (departs.hour >= 20 or departs.hour < 5) else "Overnight journey"
        next_day = " next day" if arrives.date() > departs.date() else ""
        notes.append(f"{kind}{' home' if direction == 'back' else ''} — departs {clock(departs)}, "
                     f"lands {clock(arrives)}{next_day}")
    else:
        if 5 <= departs.hour < 6:
            cost += EARLY_DEPARTURE_COST * weight
            notes.append(_sentence(f"{label}departs at {clock(departs)}"))
        if (arrives.hour == 23 and arrives.minute >= 30) or arrives.hour < 4:
            cost += LATE_ARRIVAL_COST * weight
            next_day = " the next day" if arrives.date() > departs.date() else ""
            notes.append(_sentence(f"{label}lands at {clock(arrives)}{next_day}"))

    wanted = context["departWindow"] if direction == "out" else context["returnWindow"]
    if wanted and not _in_window(departs, wanted):
        cost += WRONG_WINDOW_COST
        which = "departure" if direction == "out" else "return"
        notes.append(f"Departs {clock(departs)}, not {WINDOW_LABEL[wanted]} {which}")
    return cost


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------

def _stops_phrase(count):
    return "nonstop" if count == 0 else f"{count} stop{'s' if count != 1 else ''}"


def _requirement_checks(context, currency):
    """
    The things that must be true, as ``(name, description, test)``.

    ``test(offer, out, back)`` returns None when the offer is fine, or the
    sentence saying what's wrong with it.
    """
    checks = []

    if context["arriveBy"]:
        deadline = context["arriveBy"]
        latest = deadline - timedelta(minutes=ARRIVAL_BUFFER_MINUTES)

        def lands_in_time(offer, out, back):
            if not out["arrives"] or out["arrives"] <= latest:
                return None
            when = clock(out["arrives"]) + ("" if out["arrives"].date() == deadline.date() else f" on {_day(out['arrives'])}")
            return (f"Lands {when} — too late to be there by {clock(deadline)}"
                    + ("" if out["arrives"] > deadline else f" (needs about {ARRIVAL_BUFFER_MINUTES} minutes from the airport)"))
        checks.append(("arriveBy", f"Be there by {clock(deadline)} on {_day(deadline)}", lands_in_time))

    if context["leaveAfter"]:
        free = context["leaveAfter"]
        earliest = free + timedelta(minutes=DEPARTURE_BUFFER_MINUTES)

        def leaves_late_enough(offer, out, back):
            if not back or not back["departs"] or back["departs"] >= earliest:
                return None
            return (f"Flight home leaves {clock(back['departs'])} — too early to make after finishing at {clock(free)}")
        checks.append(("leaveAfter", f"Leave after {clock(free)} on {_day(free)}", leaves_late_enough))

    if context["maxStops"] is not None:
        limit = context["maxStops"]

        def few_enough_stops(offer, out, back):
            worst = max(out["stops"], back["stops"] if back else 0)
            if worst <= limit:
                return None
            asked = "nonstop" if limit == 0 else f"at most {_stops_phrase(limit)}"
            return f"{_sentence(_stops_phrase(worst))} — you asked for {asked}"
        checks.append(("maxStops", "Nonstop only" if limit == 0 else f"At most {_stops_phrase(limit)}", few_enough_stops))

    if context["maxBudget"]:
        budget = context["maxBudget"]

        def within_budget(offer, out, back):
            if offer["price"] <= budget:
                return None
            return f"{money(offer['price'], currency)} is over your {money(budget, currency)} budget"
        checks.append(("maxBudget", f"Under {money(budget, currency)}", within_budget))

    if context["policyMaxFare"]:
        cap = context["policyMaxFare"]

        def within_policy(offer, out, back):
            if offer["price"] <= cap:
                return None
            return f"{money(offer['price'], currency)} is above your company's {money(cap, currency)} fare limit"
        checks.append(("policyMaxFare", f"Within the {money(cap, currency)} company fare limit", within_policy))

    if context["policyMaxClass"] in CABIN_ORDER:
        allowed = CABIN_ORDER.index(context["policyMaxClass"])

        def allowed_cabin(offer, out, back):
            cabin = str(offer.get("cabinClass") or "economy").lower()
            if cabin not in CABIN_ORDER or CABIN_ORDER.index(cabin) <= allowed:
                return None
            return f"{cabin.replace('_', ' ').capitalize()} is above the cabin your company's policy allows"
        checks.append(("policyMaxClass", f"{context['policyMaxClass'].replace('_', ' ').capitalize()} cabin or below", allowed_cabin))

    return checks


RELAXED_NOTE = {
    "arriveBy": "No flight on this date lands in time — flying in a day earlier would.",
    "leaveAfter": "No flight home leaves late enough that day — returning a day later would work.",
    "maxStops": "There's no flight with that few stops on this route for these dates, so connections are shown.",
    "maxBudget": "Every fare for these dates is over your budget — showing the closest.",
    "policyMaxFare": "Every fare for these dates is above your company's fare limit.",
    "policyMaxClass": "No fare in a cabin your company's policy allows was returned.",
}


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def _one_per_itinerary(offers):
    """
    Collapse the same flights sold at several fares.

    An airline sells one itinerary as Basic, Main, Flexible, Refundable… — five
    rows that differ only in price and rules. Listing them all buries the real
    choices, so each itinerary keeps its cheapest fare, plus its cheapest
    *changeable* fare when the cheapest one can't be changed: that is the one
    difference a business traveler actually weighs. The cheapest fare is told
    what the flexible one costs, so it can say so.
    """
    groups = {}
    for offer in offers:
        key = (offer.get("airlineCode"), offer.get("departingAt"), offer.get("arrivingAt"),
               offer.get("returnDepartingAt"), offer.get("returnArrivingAt"),
               offer.get("flightNumber"), offer.get("returnFlightNumber"), offer.get("stops"))
        groups.setdefault(key, []).append(offer)

    kept = []
    for same in groups.values():
        same.sort(key=lambda o: o["price"])
        cheapest = dict(same[0])
        kept.append(cheapest)
        if cheapest.get("changeable") is True:
            continue
        flexible = next((o for o in same[1:] if o.get("changeable") is True), None)
        if flexible:
            cheapest["_flexible"] = {"id": flexible.get("id"), "price": flexible["price"],
                                     "fareBrand": flexible.get("fareBrand")}
            kept.append(dict(flexible))
    return kept


def _fare_facts(offer, currency, notes, pluses):
    """Dollar value of what the fare allows; appends what that is."""
    value = 0.0
    brand = offer.get("fareBrand")
    if offer.get("changeable") is True:
        fee = offer.get("changeFee")
        if fee == 0:
            value -= FREE_CHANGE_BONUS
            pluses.append("Free changes" + (f" ({brand})" if brand else ""))
        else:
            value -= CHANGEABLE_BONUS
            pluses.append("Changes allowed" + (f" for a {money(fee, currency)} fee" if fee else "")
                          + (f" ({brand})" if brand else ""))
    elif offer.get("changeable") is False:
        flexible = offer.get("_flexible")
        note = f"{brand or 'This fare'} can't be changed"
        if offer.get("refundable") is False:
            note += " or refunded"
        if flexible:
            gap = flexible["price"] - offer["price"]
            note += (f" — the {flexible.get('fareBrand') or 'changeable'} fare on the same flights is "
                     f"{money(gap, currency)} more")
        notes.append(note)
    if offer.get("refundable") is True:
        value -= REFUNDABLE_BONUS
        pluses.append("Refundable")
    if offer.get("carryOnBags") == 0:
        value += NO_CARRY_ON_COST
        notes.append("No carry-on bag included")
    if isinstance(offer.get("checkedBags"), int) and offer["checkedBags"] > 0:
        count = offer["checkedBags"]
        pluses.append(f"{count} checked bag{'s' if count != 1 else ''} included")
    return value

def rank_offers(offers, context=None, limit=30):
    """
    Order real offers best-first for this traveler and annotate each one.

    Returns ``(offers, summary)``. Each returned offer is the original dict plus
    a ``ranking`` entry; ``summary`` describes the search as a whole. At most
    ``limit`` offers come back, and the cheapest and fastest are always among
    them.
    """
    context = context or build_context()
    offers = [o for o in (offers or []) if isinstance(o, dict) and isinstance(o.get("price"), (int, float)) and o["price"] > 0]
    if not offers:
        return [], {"considered": 0, "shown": 0, "eligible": 0, "requirements": [], "notes": []}

    considered = len(offers)
    offers = _one_per_itinerary(offers)
    currency = offers[0].get("currency") or "USD"
    weight = COMFORT_WEIGHT[context["costSensitivity"]]
    checks = _requirement_checks(context, currency)

    legs = []
    for offer in offers:
        out = _leg(offer)
        back = _leg(offer, "return") if offer.get("tripType") == "round_trip" else None
        legs.append((out, back))

    def total_minutes(pair):
        out, back = pair
        if out["minutes"] is None or (back and back["minutes"] is None):
            return None
        return out["minutes"] + (back["minutes"] if back else 0)

    durations = [total_minutes(pair) for pair in legs]
    known = [d for d in durations if d is not None]
    fastest_minutes = min(known) if known else None

    # A requirement no offer can meet isn't a way to choose between them.
    failures = {name: [test(offer, out, back) for offer, (out, back) in zip(offers, legs)]
                for name, _, test in checks}
    relaxed = [name for name, results in failures.items() if all(results)]
    active = [(name, description) for name, description, _ in checks if name not in relaxed]

    scored = []
    for index, (offer, (out, back)) in enumerate(zip(offers, legs)):
        notes, pluses = [], []
        score = float(offer["price"])

        minutes = durations[index]
        if minutes is not None and fastest_minutes is not None:
            score += (minutes - fastest_minutes) / 60 * VALUE_OF_HOUR * weight

        score += _comfort_costs(out, "out", context, weight, notes)
        if back:
            score += _comfort_costs(back, "back", context, weight, notes)
            if back["destination"] and out["origin"] and back["destination"] != out["origin"]:
                score += DIFFERENT_RETURN_AIRPORT_COST
                notes.append(f"Returns to {back['destination']}, not {out['origin']}")

        score += _fare_facts(offer, currency, notes, pluses)

        requested = context["requestedAirline"] and _matches(
            offer.get("airline"), offer.get("airlineCode"), [context["requestedAirline"]])
        preferred = _matches(offer.get("airline"), offer.get("airlineCode"), context["preferredAirlines"])
        loyalty = _matches(offer.get("airline"), offer.get("airlineCode"), context["loyaltyAirlines"])
        if requested:
            score -= REQUESTED_AIRLINE_BONUS
            pluses.append(f"On {offer.get('airline')}, the airline you asked for")
        elif preferred:
            score -= PREFERRED_AIRLINE_BONUS
            pluses.append(f"On {offer.get('airline')}, one of your preferred airlines")
        if loyalty:
            score -= LOYALTY_BONUS
            pluses.append(f"Earns on your {loyalty} membership")

        all_stops = out["stops"] + (back["stops"] if back else 0)
        if all_stops == 0:
            pluses.insert(0, "Nonstop both ways" if back else "Nonstop")
        rough = ("Red-eye", "Overnight", "Departs at", "Lands at", "departs at", "lands at")
        if out["departs"] and not any(n.startswith(rough) or "eturn departs at" in n or "eturn lands at" in n for n in notes):
            pluses.append("Daytime flights both ways" if back else "Daytime flight")

        issues = [failures[name][index] for name, _ in active if failures[name][index]]
        # Still worth something when everything misses it: by how much.
        for name in relaxed:
            if name == "maxBudget":
                score += (offer["price"] - context["maxBudget"])
            elif name == "policyMaxFare":
                score += (offer["price"] - context["policyMaxFare"])
            elif name == "arriveBy" and out["arrives"]:
                late = (out["arrives"] - context["arriveBy"]).total_seconds() / 3600
                score += max(0.0, late + ARRIVAL_BUFFER_MINUTES / 60) * 60

        if context["arriveBy"] and "arriveBy" not in relaxed and out["arrives"] and not issues:
            spare = (context["arriveBy"] - out["arrives"]).total_seconds() / 60
            if out["arrives"].date() == context["arriveBy"].date():
                pluses.append(f"Lands {clock(out['arrives'])}, {span(spare)} before you need to be there")

        scored.append({
            "offer": offer, "score": round(score, 2), "eligible": not issues,
            "issues": issues, "notes": notes, "pluses": pluses, "minutes": minutes,
        })

    scored.sort(key=lambda row: (not row["eligible"], row["score"], row["offer"]["price"]))
    eligible = [row for row in scored if row["eligible"]]
    pool = eligible or scored

    best = pool[0]
    cheapest = min(pool, key=lambda row: (row["offer"]["price"], row["score"]))
    timed = [row for row in pool if row["minutes"] is not None]
    fastest = min(timed, key=lambda row: (row["minutes"], row["score"])) if timed else None

    for row in scored:
        price_gap = row["offer"]["price"] - cheapest["offer"]["price"]
        if row is not cheapest and price_gap >= 1:
            row["notes"].append(f"{money(price_gap, currency)} more than the cheapest fare")
        if fastest and row is not fastest and row["minutes"] is not None and row["minutes"] - fastest["minutes"] >= 20:
            row["notes"].append(f"{span(row['minutes'] - fastest['minutes'])} longer than the fastest option")

    # What the traveler sees first: the pick, then the two alternatives people
    # actually ask about ("what's cheapest?", "what's quickest?"), then a
    # different airline, then everything else in order.
    order = [best]
    # ...when they are real alternatives: a fare that saves a few cents, or a
    # journey five minutes shorter, isn't a different choice.
    if cheapest is not best and best["offer"]["price"] - cheapest["offer"]["price"] >= 10:
        order.append(cheapest)
    if (fastest and fastest not in order and best["minutes"] is not None
            and best["minutes"] - fastest["minutes"] >= 30):
        order.append(fastest)
    airlines = {row["offer"].get("airline") for row in order}
    for row in pool:
        if len(order) >= 3:
            break
        if row not in order and row["offer"].get("airline") not in airlines:
            order.append(row)
            airlines.add(row["offer"].get("airline"))
    order += [row for row in scored if row not in order]
    order = order[:max(1, limit)]

    ranked = []
    for position, row in enumerate(order):
        badges = []
        if row is best and row["eligible"]:
            badges.append("Best overall")
        if row is cheapest:
            badges.append("Cheapest")
        if row is fastest:
            badges.append("Fastest")
        public = {k: v for k, v in row["offer"].items() if not k.startswith("_")}
        ranked.append({**public, "ranking": {
            "position": position + 1,
            "score": row["score"],
            "eligible": row["eligible"],
            "badges": badges,
            "issues": row["issues"],
            "notes": row["notes"],
            "pluses": row["pluses"],
        }})

    notes = [RELAXED_NOTE[name] for name in relaxed]
    if context["requestedAirline"] and not any(
            _matches(o.get("airline"), o.get("airlineCode"), [context["requestedAirline"]]) for o in offers):
        notes.append(f"No {context['requestedAirline'].title()} flights came back for this route and these dates.")
    if checks and not eligible and not relaxed:
        notes.append("No single flight meets everything you asked for — these are the closest.")
    summary = {
        "considered": considered,
        "shown": len(ranked),
        "eligible": len(eligible),
        "requirements": [description for _, description in active],
        "unmet": relaxed,
        "notes": notes,
        "bestId": best["offer"].get("id"),
        "cheapestId": cheapest["offer"].get("id"),
        "fastestId": fastest["offer"].get("id") if fastest else None,
        "costSensitivity": context["costSensitivity"],
    }
    return ranked, summary


# ---------------------------------------------------------------------------
# Saying why
# ---------------------------------------------------------------------------

def _lower_first(text):
    return text[:1].lower() + text[1:] if text else text


def explain_pick(ranked, summary):
    """
    Why the first offer is the pick, from the same facts that ranked it.

    Always available — the Gemini advisor (``api.trip_advisor``) writes a better
    one when it can, and this is what's shown when it can't.
    """
    if not ranked:
        return None
    pick = ranked[0]
    facts = pick["ranking"]
    currency = pick.get("currency") or "USD"
    by_id = {offer.get("id"): offer for offer in ranked}
    cheapest = by_id.get(summary.get("cheapestId"))
    fastest = by_id.get(summary.get("fastestId"))
    is_cheapest, is_fastest = "Cheapest" in facts["badges"], "Fastest" in facts["badges"]

    if not facts["eligible"]:
        headline = "Closest match — nothing fits everything you asked for"
    elif summary.get("requirements") and summary.get("eligible") == 1:
        headline = "The only flight that fits what you asked for"
    elif is_cheapest and is_fastest:
        headline = "The cheapest and the fastest option"
    elif is_cheapest:
        headline = "The lowest fare, on a schedule that works"
    elif is_fastest:
        headline = "The fastest way there, at a fair price"
    else:
        headline = "The best balance of price, time and schedule"

    def stops(offer):
        return int(offer.get("stops") or 0) + int(offer.get("returnStops") or 0)

    why = list(facts["pluses"][:3])
    if is_cheapest and summary.get("considered", 0) > 1:
        why.insert(0, f"The lowest of the {summary['considered']:,} fares the airlines returned for these dates")
    if cheapest and cheapest is not pick:
        gap = pick["price"] - cheapest["price"]
        drawbacks = [n for n in cheapest["ranking"]["notes"]
                     if "more than the cheapest" not in n and "longer than the fastest" not in n][:2]
        extra_stops = stops(cheapest) - stops(pick)
        if extra_stops > 0:
            drawbacks.insert(0, f"{extra_stops} more stop{'s' if extra_stops != 1 else ''}")
        if cheapest["ranking"]["issues"]:
            drawbacks = cheapest["ranking"]["issues"][:1]
        line = (f"{money(gap, currency)} more than the cheapest fare ({cheapest.get('airline')}, "
                f"{money(cheapest['price'], currency)})")
        if drawbacks:
            line += f". That one: {'; '.join(_lower_first(d) for d in drawbacks[:2])}"
        why.append(line)
    def minutes(offer):
        legs = [offer.get("durationMinutes"), offer.get("returnDurationMinutes")]
        legs = [m for m in legs if isinstance(m, int)]
        return sum(legs) if legs else None

    if fastest and fastest is not pick and fastest is not cheapest and minutes(pick) and minutes(fastest):
        gap = fastest["price"] - pick["price"]
        saved = minutes(pick) - minutes(fastest)
        # Five minutes quicker isn't a different choice; half an hour is.
        if gap >= 1 and saved >= 30:
            why.append(f"The fastest option ({fastest.get('airline')}) saves {span(saved)} but costs {money(gap, currency)} more")
    if not why:
        why.append("Ranked first on fare, total travel time and connections")

    drawbacks = [n for n in facts["notes"] if "more than the cheapest" not in n and "longer than the fastest" not in n]
    tradeoff = (facts["issues"] or drawbacks or [None])[0]
    return {
        "engine": "rules",
        "pickId": pick.get("id"),
        "headline": headline,
        "why": why[:4],
        "tradeoff": tradeoff,
        "notes": summary.get("notes") or [],
    }
