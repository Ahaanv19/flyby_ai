"""
Flyby AI — trip reasoning engine.

Produces the four scored panels the app shows for a trip (cost efficiency, time
efficiency, policy compliance, risk) plus a short summary. When a Gemini key is
configured this is a real model judgement of the specific trip; without one — or
if the call fails — it falls back to a deterministic local analysis so the
feature always returns a complete, sensible result.

The output shape matches the frontend's ``TripAIReasoning`` type exactly:

    {
      "costEfficiency":   {"score": int, "label": str, "detail": str},
      "timeEfficiency":   {"score": int, "label": str, "detail": str},
      "policyCompliance": {"score": int, "label": str, "detail": str},
      "riskLevel":        {"score": int, "label": str, "detail": str},
      "summary": str
    }

Policy compliance is grounded in the caller's actual travel policy (flight-price
cap, hotel nightly cap, approval threshold), so the score reflects real limits
rather than a guess.
"""

from api.gemini import call_gemini

# Score bands shared by the model prompt and the local fallback, so both speak
# the same language in the UI.
POSITIVE_LABELS = [(90, "Excellent"), (75, "High"), (55, "Good"), (35, "Fair"), (0, "Low")]
RISK_LABELS = [(70, "High"), (45, "Medium"), (20, "Low"), (0, "Minimal")]


def _label_for(score, risk=False):
    """Map a 0-100 score to its band label."""
    table = RISK_LABELS if risk else POSITIVE_LABELS
    for threshold, label in table:
        if score >= threshold:
            return label
    return table[-1][1]


def _clamp_score(value, default=70):
    """Coerce a model/JSON value into an int in [0, 100]."""
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def _build_prompt(trip, policy):
    """Build the system prompt, embedding the traveler's real policy limits."""
    if policy:
        policy_text = (
            "COMPANY TRAVEL POLICY (score policyCompliance against these):\n"
            f"- Max flight price: ${policy.get('max_flight_price') or 'no limit'}\n"
            f"- Max flight class: {policy.get('max_flight_class') or 'any'}\n"
            f"- Max nightly hotel rate: ${policy.get('max_nightly_hotel_rate') or 'no limit'}\n"
            f"- Trips above ${policy.get('approval_required_above') or 'n/a'} need manager approval\n"
        )
    else:
        policy_text = (
            "COMPANY TRAVEL POLICY: none on file. Score policyCompliance against "
            "typical corporate norms (economy flights, hotels under ~$350/night, "
            "approval for trips over ~$2500).\n"
        )

    return f"""You are Flyby AI's trip analyst. You evaluate a planned business trip and return a concise, honest assessment across four dimensions.

Return ONLY a JSON object with EXACTLY these fields:

{{
  "costEfficiency":   {{"score": <0-100>, "label": <one word>, "detail": <one specific sentence>}},
  "timeEfficiency":   {{"score": <0-100>, "label": <one word>, "detail": <one specific sentence>}},
  "policyCompliance": {{"score": <0-100>, "label": <one word>, "detail": <one specific sentence>}},
  "riskLevel":        {{"score": <0-100, where HIGHER MEANS MORE RISK>, "label": <one word>, "detail": <one specific sentence>}},
  "summary": <2-3 sentence plain-English summary of why this plan is or isn't a good choice>
}}

SCORING GUIDANCE:
- costEfficiency: higher = better value. Consider the total cost against the destination and trip length.
- timeEfficiency: higher = better use of time. Consider flight timing, layovers, and how the schedule fits a work trip.
- policyCompliance: higher = more compliant. 100 = fully within policy; drop the score for each limit exceeded and mention which.
- riskLevel: higher = MORE risk. Consider destination, timing, cost overrun, and approval status. A safe, cheap, in-policy trip scores LOW.

{policy_text}
Be specific in every "detail" — reference the actual destination, dates, cost, airline or hotel from the trip. Never use generic filler.

Return ONLY valid JSON. No markdown, no commentary."""


def _describe_trip(trip):
    """Render the trip as the user-turn text the model analyses."""
    flight = trip.get("flight") or {}
    hotel = trip.get("hotel") or {}
    lines = [
        f"Destination: {trip.get('destination') or 'unspecified'}",
        f"Dates: {trip.get('startDate') or '?'} to {trip.get('endDate') or '?'}",
        f"Purpose: {trip.get('purpose') or 'business'}",
        f"Estimated total cost: ${trip.get('estimatedCost') or 0}",
    ]
    if flight:
        lines.append(
            f"Flight: {flight.get('airline') or 'TBD'} "
            f"{flight.get('flightNumber') or ''}".strip()
            + (f", departs {flight.get('departTime')}" if flight.get('departTime') else "")
            + (f", ${flight.get('price')}" if flight.get('price') else "")
        )
    if hotel:
        lines.append(
            f"Hotel: {hotel.get('name') or 'TBD'}"
            + (f" in {hotel.get('location')}" if hotel.get('location') else "")
        )
    if trip.get("groundTransport"):
        lines.append(f"Ground transport: {trip['groundTransport']}")
    if trip.get("approvalStatus"):
        lines.append(f"Approval status: {trip['approvalStatus']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def _reason_with_gemini(trip, policy):
    """Call Gemini and return the parsed reasoning dict, or None on any failure."""
    parsed = call_gemini(
        _build_prompt(trip, policy),
        _describe_trip(trip),
        max_output_tokens=4096,
        temperature=0.6,
    )
    if parsed is None:
        return None
    return _normalize(parsed)


def _normalize(parsed):
    """
    Coerce a model response into the exact TripAIReasoning shape.

    Scores are clamped and labels are re-derived from the score, so a model that
    returns an out-of-range number or a mismatched label can never produce a
    malformed panel.
    """
    if not isinstance(parsed, dict):
        return None

    def panel(key, risk=False, default=70):
        raw = parsed.get(key) or {}
        score = _clamp_score(raw.get("score"), default)
        detail = str(raw.get("detail") or "").strip() or "No detail available."
        return {"score": score, "label": _label_for(score, risk), "detail": detail}

    result = {
        "costEfficiency": panel("costEfficiency"),
        "timeEfficiency": panel("timeEfficiency"),
        "policyCompliance": panel("policyCompliance"),
        "riskLevel": panel("riskLevel", risk=True, default=20),
        "summary": str(parsed.get("summary") or "").strip(),
    }
    if not result["summary"]:
        result["summary"] = (
            "This plan looks reasonable overall. Review the individual scores "
            "for cost, timing, policy fit and risk before booking."
        )
    return result


# ---------------------------------------------------------------------------
# Local fallback
# ---------------------------------------------------------------------------

def _reason_locally(trip, policy):
    """
    Deterministic reasoning used when Gemini is unavailable.

    Not random: the scores follow from the trip's real cost against the policy
    limits, so the fallback is still a defensible assessment rather than filler.
    """
    destination = trip.get("destination") or "the destination"
    cost = float(trip.get("estimatedCost") or 0)
    flight = trip.get("flight") or {}
    hotel = trip.get("hotel") or {}

    # -- policy compliance: driven by the real limits --------------------
    max_flight = (policy or {}).get("max_flight_price")
    approval_above = (policy or {}).get("approval_required_above")
    breaches = []
    compliance = 100
    flight_price = flight.get("price")
    if max_flight and flight_price and float(flight_price) > float(max_flight):
        compliance -= 35
        breaches.append(f"flight ${flight_price} exceeds the ${max_flight} cap")
    if approval_above and cost > float(approval_above):
        compliance -= 25
        breaches.append(f"total ${cost:,.0f} needs manager approval")
    compliance = max(0, compliance)
    compliance_detail = (
        "Within policy on price and approval limits." if not breaches
        else "Out of policy: " + "; ".join(breaches) + "."
    )

    # -- cost efficiency: cheaper trips score higher ---------------------
    if cost <= 0:
        cost_score, cost_detail = 70, "No cost estimate yet; add one for a sharper read."
    elif cost < 1200:
        cost_score, cost_detail = 90, f"${cost:,.0f} is a lean total for {destination}."
    elif cost < 2200:
        cost_score, cost_detail = 74, f"${cost:,.0f} is about average for {destination}."
    else:
        cost_score, cost_detail = 52, f"${cost:,.0f} is on the high side for {destination}."

    # -- time efficiency: a booked flight is a good sign -----------------
    if flight.get("airline"):
        time_score = 86
        time_detail = f"{flight['airline']} flight is booked; schedule looks workable for a business trip."
    else:
        time_score = 62
        time_detail = "No flight selected yet, so timing can't be fully assessed."

    # -- risk: rises with cost overrun and policy breaches ---------------
    risk = 15 + len(breaches) * 20 + (15 if cost > 2500 else 0)
    risk = min(100, risk)
    risk_detail = (
        "Low-risk: in policy, reasonable cost, nothing unusual." if risk <= 20
        else "Elevated: " + (breaches[0] if breaches else "cost is above the usual range") + "."
    )

    hotel_bit = f", staying at {hotel['name']}" if hotel.get("name") else ""
    summary = (
        f"This trip to {destination}{hotel_bit} totals about ${cost:,.0f}. "
        + ("It stays within policy and looks like a sound plan. "
           if not breaches else
           "Note the policy exceptions above before booking. ")
        + ("A flight is selected, so the schedule is largely set."
           if flight.get("airline") else
           "Selecting a flight will firm up the timing.")
    )

    return {
        "costEfficiency": {"score": cost_score, "label": _label_for(cost_score), "detail": cost_detail},
        "timeEfficiency": {"score": time_score, "label": _label_for(time_score), "detail": time_detail},
        "policyCompliance": {"score": compliance, "label": _label_for(compliance), "detail": compliance_detail},
        "riskLevel": {"score": risk, "label": _label_for(risk, risk=True), "detail": risk_detail},
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def reason_about_trip(trip, policy=None):
    """
    Return a complete TripAIReasoning object plus which engine produced it.

    Tries Gemini first, falls back to the local engine, and always returns a
    valid result — the caller never has to handle a failure.
    """
    reasoning = _reason_with_gemini(trip, policy)
    engine = "gemini"
    if reasoning is None:
        reasoning = _reason_locally(trip, policy)
        engine = "local"
    return reasoning, engine
