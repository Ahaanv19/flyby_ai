"""
Flyby AI — Duffel flight endpoints (``/api/duffel``).

    POST /api/duffel/search   real flight offers (read-only, no charge)
    POST /api/duffel/offer    re-price one offer before booking (read-only)
    POST /api/duffel/book     create a real order — guarded, charges money live
    GET  /api/duffel/status   whether real search/booking are available

Search and pricing are safe to call freely. Booking is refused unless every
guard in ``api.duffel.create_order`` passes; a refusal returns a clear error and
never charges anything.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date

from flask import Blueprint, current_app, g, jsonify, request

from api.jwt_authorize import token_required
from api.duffel import (BookingError, create_component_client_key, create_order,
                        get_offer, is_configured, is_live, search_offers)
from api.flight_ranker import build_context, explain_pick, rank_offers
from api.trip_advisor import advise, lead_with
from api import notify
from model.security import AuditLog

duffel_api = Blueprint('duffel_api', __name__, url_prefix='/api/duffel')

# Searches for other dates run alongside the main one, so checking them costs
# the traveler no extra wait.
_DATE_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="date-check")
MAX_OTHER_DATES = 2


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "unknown")


@duffel_api.route('/status', methods=['GET'])
@token_required()
def status():
    """Report whether live search and booking are available."""
    return jsonify({
        "configured": is_configured(),
        "live": is_live(),
        "bookingEnabled": bool(current_app.config.get("DUFFEL_BOOKING_ENABLED")),
        "maxBookingUsd": current_app.config.get("DUFFEL_MAX_BOOKING_USD"),
    }), 200


def _traveler_context(raw):
    """
    What the ranker should know about whoever is searching: what they asked for
    (``raw``, from the request), their saved preferences and loyalty programs,
    and their company's travel policy.

    The policy is read here, from the caller's own company, never from the
    request — a client can't grant itself a higher fare limit.
    """
    preferences = loyalty = policy = None
    try:
        from model.preferences import LoyaltyProgram, TravelPreference
        saved = TravelPreference.query.filter_by(_user_id=g.user_id).first()
        preferences = saved.read() if saved else None
        loyalty = [row._program_name for row in
                   LoyaltyProgram.query.filter_by(_user_id=g.user_id, _kind="airline").all()]
        profile = g.current_user.profile
        if profile and profile.company_id:
            from model.company import TravelPolicy
            row = TravelPolicy.for_company(profile.company_id)
            policy = row.read() if row else None
    except Exception as error:  # noqa: BLE001 — a search must work without these
        current_app.logger.warning("Couldn't load traveler context for ranking: %s", error)
    return build_context(raw, preferences=preferences, loyalty=loyalty, policy=policy)


def _other_dates(raw_context, params, has_return):
    """
    The other travel dates the app asked to have checked, cleaned up.

    Used when the traveler gave no dates and Flyby suggested some: the same trip
    a week or two later can cost half as much, and the only honest way to know
    is to search it. At most ``MAX_OTHER_DATES``, future dates only.
    """
    wanted = raw_context.get("alsoCheck")
    if not isinstance(wanted, list):
        return []
    today = date.today().isoformat()
    cleaned = []
    for entry in wanted:
        if not isinstance(entry, dict):
            continue
        out, back = str(entry.get("departureDate") or "")[:10], str(entry.get("returnDate") or "")[:10]
        try:
            date.fromisoformat(out)
            if has_return:
                date.fromisoformat(back)
        except ValueError:
            continue
        if out <= today or out == params["departure_date"] or (has_return and back < out):
            continue
        cleaned.append((out, back if has_return else None))
        if len(cleaned) >= MAX_OTHER_DATES:
            break
    return cleaned


def _best_on(app, context, params, out, back):
    """The pick for the same trip on other dates: one real fare, ranked the same way."""
    with app.app_context():
        offers = search_offers(return_date=back, everything=True, **{**params, "departure_date": out})
    if not offers:
        return None
    # A time to be somewhere belongs to the original day, not to these.
    ranked, summary = rank_offers(offers, {**context, "arriveBy": None, "leaveAfter": None}, limit=1)
    if not ranked or not ranked[0]["ranking"]["eligible"]:
        return None
    best = ranked[0]
    return {
        "departureDate": out,
        "returnDate": back,
        "price": best["price"],
        "currency": best.get("currency") or "USD",
        "airline": best.get("airline"),
        "stops": best.get("stops") or 0,
        "returnStops": best.get("returnStops") if back else None,
        "departTime": best.get("departTime"),
        "fareBrand": best.get("fareBrand"),
        "considered": summary.get("considered"),
    }


@duffel_api.route('/search', methods=['POST'])
@token_required()
def search():
    """
    Search real flight offers. Read-only — never charges.

    Returns ``{offers, source, tripType}``. ``source`` is "duffel" (real airline
    inventory) or "unavailable" — in which case there are no offers and the
    caller shows none; nothing is generated in their place.

    With a ``returnDate`` the search is for a round trip, and each offer's price
    covers both directions. If the airlines return no round-trip fares at all,
    the outbound is searched on its own and ``returnUnavailable`` is set so the
    UI can say the return still needs booking, rather than showing nothing.

    With a ``context`` object the offers come back **ranked for this traveler**
    (see ``api.flight_ranker``): best first, each carrying a ``ranking`` entry
    that says what's good and bad about it, plus a ``ranking`` summary and an
    ``advice`` object explaining the pick. ``context.advise`` asks Gemini to make
    the final call among the top few (``api.trip_advisor``). Without ``context``
    the response is what it always was, cheapest first.

    ``context.alsoCheck`` — a list of up to two ``{departureDate, returnDate}`` —
    has the same trip searched on those dates too, at the same time, and
    reported as ``dateOptions``: the best fare on each, so the app can say when
    different dates would cost much less.
    """
    data = request.get_json(silent=True) or {}

    departure_date = data.get("departureDate") or data.get("departure_date")
    return_date = data.get("returnDate") or data.get("return_date") or None
    # A return before the outbound can't be priced; treat it as one-way rather
    # than sending Duffel a request it will reject.
    if return_date and departure_date and str(return_date) < str(departure_date):
        return_date = None

    params = dict(
        origin=(data.get("origin") or "").strip().upper(),
        destination=(data.get("destination") or "").strip().upper(),
        departure_date=departure_date,
        passengers=data.get("passengers") or 1,
        cabin_class=data.get("cabinClass") or "economy",
        airline=(data.get("airline") or None),
    )

    raw_context = data.get("context") if isinstance(data.get("context"), dict) else None
    # The ranker chooses from everything the airlines returned, not from a
    # price-ordered cut of it.
    everything = raw_context is not None and not params["airline"]

    # Other dates are searched while this one is, on their own threads.
    context = _traveler_context(raw_context) if everything else None
    app = current_app._get_current_object()
    pending_dates = [
        (out, back, _DATE_POOL.submit(_best_on, app, context, params, out, back))
        for out, back in (_other_dates(raw_context, params, bool(return_date)) if everything else [])
    ]

    offers = search_offers(return_date=return_date, everything=everything, **params)
    return_unavailable = False
    if return_date and offers == []:
        # Reachable, but nothing sold as a round trip. Only retried in this case:
        # if Duffel itself couldn't be reached (None), asking again won't help.
        one_way = search_offers(return_date=None, everything=everything, **params)
        if one_way:
            offers, return_unavailable = one_way, True

    if offers is None:
        return jsonify({"offers": [], "source": "unavailable"}), 200

    response = {
        "source": "duffel",
        "live": is_live(),
        "tripType": "round_trip" if (return_date and not return_unavailable) else "one_way",
        "returnUnavailable": return_unavailable,
    }
    if not everything:
        return jsonify({**response, "offers": offers}), 200

    ranked, summary = rank_offers(offers, context, limit=30)
    advice = explain_pick(ranked, summary)
    if raw_context.get("advise") and ranked:
        request_text = str(raw_context.get("requestText") or "").strip()[:400] or None
        advice, pick_id = advise(ranked, summary, context, request_text=request_text)
        ranked = lead_with(ranked, pick_id)

    date_options = []
    for out, back, future in pending_dates:
        try:
            # They started with the main search, so they are usually done; a slow
            # one is left out rather than waited for.
            option = future.result(timeout=12)
        except FutureTimeout:
            continue
        except Exception as error:  # noqa: BLE001 — a hint must never fail the search
            current_app.logger.warning("Checking %s failed: %s", out, error)
            continue
        if option:
            date_options.append(option)

    return jsonify({**response, "offers": ranked, "ranking": summary, "advice": advice,
                    "dateOptions": date_options}), 200


@duffel_api.route('/offer', methods=['POST'])
@token_required()
def offer():
    """Re-fetch one offer to confirm its current price before booking."""
    data = request.get_json(silent=True) or {}
    result = get_offer((data.get("offerId") or data.get("offer_id") or "").strip())
    if result is None:
        return jsonify({"offer": None, "error": "That fare is no longer available."}), 200
    return jsonify({"offer": result}), 200


@duffel_api.route('/component-key', methods=['POST'])
@token_required()
def component_key():
    """
    Mint a Duffel component client key so the frontend can show the card form.

    Read-only — creating a key never charges. Returns ``{clientKey}`` on success,
    or ``{clientKey: null, error}`` when Duffel Payments isn't enabled yet (which
    is the common case until the account is set up).
    """
    key, error = create_component_client_key()
    if key is None:
        return jsonify({"clientKey": None, "error": error}), 200
    return jsonify({"clientKey": key}), 200


@duffel_api.route('/book', methods=['POST'])
@token_required()
def book():
    """
    Create a real flight order. Charges money on a live token.

    Every guard lives in ``create_order``; this endpoint just carries the
    request through and turns a refusal into a clean error. Both the attempt and
    the outcome are written to the audit log.
    """
    data = request.get_json(silent=True) or {}
    offer_id = (data.get("offerId") or data.get("offer_id") or "").strip()
    passengers = data.get("passengers")
    confirmed = bool(data.get("confirm") or data.get("confirmed"))
    three_ds = (data.get("threeDSecureSessionId") or data.get("three_d_secure_session_id") or "").strip() or None

    AuditLog.record(
        g.user_id, "flight_booking_attempt", True,
        ip_address=_client_ip(), offer_id=offer_id, confirmed=confirmed,
        payment="card" if three_ds else "balance",
    )

    try:
        order = create_order(offer_id, passengers, confirmed=confirmed,
                             three_d_secure_session_id=three_ds)
    except BookingError as error:
        AuditLog.record(g.user_id, "flight_booking_refused", False,
                        ip_address=_client_ip(), reason=error.code)
        return jsonify({"error": error.message, "code": error.code}), error.status

    AuditLog.record(
        g.user_id, "flight_booked", True, ip_address=_client_ip(),
        order_id=order.get("id"), booking_reference=order.get("booking_reference"),
    )
    # The ticket exists and the card is charged; the confirmation email follows
    # on its own and can't affect either.
    notify.flight_booked(g.current_user, order)
    return jsonify({
        "order": {
            "id": order.get("id"),
            "bookingReference": order.get("booking_reference"),
            "totalAmount": order.get("total_amount"),
            "totalCurrency": order.get("total_currency"),
            "passengers": order.get("passengers"),
            "documents": order.get("documents"),
        }
    }), 201
