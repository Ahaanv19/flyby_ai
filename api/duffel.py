"""
Flyby AI — Duffel flight search and booking.

Real airline inventory and real orders through Duffel. Two very different risk
levels live here, and the code treats them differently:

* **Search & pricing are read-only** — an offer request or offer lookup never
  charges anything, so they run freely. When Duffel is unavailable the caller
  gets nothing back and says so; there is no generated substitute, because a
  flight that didn't come from an airline is a flight that may not exist.

* **Booking creates a real order.** With a ``duffel_live_`` token that charges
  real money and issues a real ticket, so it is gated three ways: it is disabled
  unless ``DUFFEL_BOOKING_ENABLED`` is set, it refuses any order above
  ``DUFFEL_MAX_BOOKING_USD``, and it requires the caller to pass an explicit
  confirmation. A bug or bad input therefore cannot silently book a flight.

Everything the frontend needs to book — most importantly the Duffel ``offer_id``
and its expiry — is carried through the normalized offer shape.
"""

import logging
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

TIMEOUT = 30

# Duffel is reached over the public internet, so a request can occasionally be
# dropped mid-handshake (flaky Wi-Fi, a captive portal, a transient TLS reset).
# One dropped request shouldn't force the whole search onto the fake local
# generator, so network-level failures are retried a few times before giving up.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.6  # seconds, grows linearly per attempt


def _request_with_retries(method, url, **kwargs):
    """
    Do an HTTP request, retrying only on connection/TLS/timeout errors (never on
    an HTTP response — a 4xx/5xx is returned as-is for the caller to handle).

    Returns the ``requests.Response`` on success, or ``None`` if every attempt
    failed to reach Duffel.
    """
    timeout = kwargs.pop("timeout", TIMEOUT)
    last_error = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return requests.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as error:
            last_error = error
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF * attempt)
        except requests.RequestException as error:
            # Anything else (e.g. malformed request) won't be fixed by retrying.
            last_error = error
            break
    logger.warning("Duffel request to %s failed after %d attempt(s): %s",
                   url, _RETRY_ATTEMPTS, last_error)
    return None


def is_configured():
    """Whether a Duffel token is set (search can use the real API)."""
    return bool(current_app.config.get("DUFFEL_ACCESS_TOKEN"))


def is_live():
    """Whether the configured token books real (paid) tickets."""
    return bool(current_app.config.get("DUFFEL_LIVE"))


def _headers():
    return {
        "Authorization": f"Bearer {current_app.config['DUFFEL_ACCESS_TOKEN']}",
        "Duffel-Version": current_app.config.get("DUFFEL_VERSION", "v2"),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _url(path):
    base = current_app.config.get("DUFFEL_API_URL", "https://api.duffel.com").rstrip("/")
    return f"{base}{path}"


# ---------------------------------------------------------------------------
# Normalization — Duffel offer -> the shape the frontend flight UI renders
# ---------------------------------------------------------------------------

def _iso_duration_to_label(iso):
    """
    Turn an ISO-8601 duration like 'PT5H30M' into '5h 30m'.

    Journeys of a day or more arrive as 'P1DT2H10M'. Those used to fall through
    as an empty string, so the longest itineraries — the ones where the duration
    matters most — showed no duration at all. Days are folded into hours.
    """
    if not iso or not iso.startswith("P"):
        return ""
    days = hours = minutes = 0
    num = ""
    in_time = False
    for ch in iso[1:]:
        if ch == "T":
            in_time, num = True, ""
        elif ch.isdigit():
            num += ch
        elif ch == "D" and not in_time:
            days, num = int(num or 0), ""
        elif ch == "H" and in_time:
            hours, num = int(num or 0), ""
        elif ch == "M" and in_time:
            minutes, num = int(num or 0), ""
        else:
            num = ""
    if not (days or hours or minutes):
        return ""
    return f"{days * 24 + hours}h {minutes:02d}m"


def _day_offset(depart_iso, arrive_iso):
    """
    How many calendar days after departure a flight lands (0, 1, 2…).

    "7:45 PM → 8:05 AM" reads as an overnight hop but can be a two-day journey;
    without this the traveler can't tell which day they arrive.
    """
    try:
        from datetime import date
        d = date.fromisoformat((depart_iso or "")[:10])
        a = date.fromisoformat((arrive_iso or "")[:10])
        return max(0, (a - d).days)
    except ValueError:
        return 0


def _slice_summary(flight_slice, owner):
    """The traveler-facing facts about one direction of travel."""
    segments = (flight_slice or {}).get("segments") or []
    first_seg = segments[0] if segments else {}
    last_seg = segments[-1] if segments else {}
    carrier = first_seg.get("marketing_carrier") or owner or {}
    stops = max(0, len(segments) - 1)
    return {
        "flightNumber": f"{carrier.get('iata_code', '')}{first_seg.get('marketing_carrier_flight_number') or ''}".strip(),
        "carrier": carrier,
        "departTime": _time_label(first_seg.get("departing_at")),
        "arriveTime": _time_label(last_seg.get("arriving_at")),
        "departingAt": first_seg.get("departing_at"),
        "arrivingAt": last_seg.get("arriving_at"),
        "arriveDayOffset": _day_offset(first_seg.get("departing_at"), last_seg.get("arriving_at")),
        "duration": _iso_duration_to_label((flight_slice or {}).get("duration")),
        "stops": stops,
        # Every connection point, in order — a two-stop itinerary names both.
        "stopCities": [
            (seg.get("destination") or {}).get("iata_code")
            for seg in segments[:-1]
            if (seg.get("destination") or {}).get("iata_code")
        ],
        "origin": (first_seg.get("origin") or {}).get("iata_code") or "",
        "destination": (last_seg.get("destination") or {}).get("iata_code") or "",
    }


def _time_label(iso_datetime):
    """Extract a 'H:MM AM/PM' label from an ISO datetime string."""
    if not iso_datetime or "T" not in iso_datetime:
        return ""
    try:
        hh, mm = iso_datetime.split("T")[1][:5].split(":")
        hour = int(hh)
        suffix = "AM" if hour < 12 else "PM"
        display = hour % 12 or 12
        return f"{display}:{mm} {suffix}"
    except (ValueError, IndexError):
        return ""


def normalize_offer(offer):
    """
    Convert a Duffel offer into Flyby's flight shape.

    Carries the Duffel ``id`` (the offer id needed to book) and ``expiresAt``
    (offers expire — the frontend must book before then or re-search).

    A round-trip offer has two slices. The top-level fields describe the
    outbound journey, as they always have; the ``return*`` fields describe the
    journey home. The price is Duffel's total for everything in the offer.
    """
    slices = offer.get("slices") or []
    owner = offer.get("owner") or {}
    out = _slice_summary(slices[0] if slices else {}, owner)
    back = _slice_summary(slices[1], owner) if len(slices) > 1 else None
    carrier = out["carrier"]

    try:
        price = round(float(offer.get("total_amount") or 0), 2)
    except (TypeError, ValueError):
        price = 0.0

    tags = []
    if out["stops"] == 0:
        tags.append("Nonstop")

    # Order creation must echo back the offer's own passenger ids, so carry them.
    passenger_ids = [p.get("id") for p in (offer.get("passengers") or []) if p.get("id")]

    normalized = {
        "id": offer.get("id"),                       # Duffel offer id — book with this
        "passengerIds": passenger_ids,               # required when creating the order
        "expiresAt": offer.get("expires_at"),        # offer expiry
        "airline": owner.get("name") or carrier.get("name") or "Airline",
        "airlineLogo": owner.get("logo_symbol_url"),
        "airlineCode": owner.get("iata_code") or carrier.get("iata_code") or "",
        "flightNumber": out["flightNumber"],
        "departTime": out["departTime"],
        "arriveTime": out["arriveTime"],
        "departureTime": out["departTime"],
        "arrivalTime": out["arriveTime"],
        "departingAt": out["departingAt"],
        "arrivingAt": out["arrivingAt"],
        "arriveDayOffset": out["arriveDayOffset"],
        "duration": out["duration"],
        "stops": out["stops"],
        "stopCity": out["stopCities"][0] if out["stopCities"] else None,
        "stopCities": out["stopCities"],
        "price": price,
        # The fare exactly as the airline quoted it ("1021.40"). Payments must
        # echo this back, so it is kept as the original string rather than
        # rebuilt from the rounded float above.
        "totalAmount": offer.get("total_amount"),
        "currency": offer.get("total_currency") or "USD",
        "origin": out["origin"],
        "destination": out["destination"],
        "cabinClass": (offer.get("cabin_class") or "economy"),
        "passengerCount": len(offer.get("passengers") or []) or 1,
        "tripType": "round_trip" if back else "one_way",
        # Some airlines won't issue a ticket without passport details (most
        # international routes). The booking form must collect them when set.
        "identityDocumentsRequired": bool(offer.get("passenger_identity_documents_required")),
        "source": "duffel",
    }
    if back:
        normalized.update({
            "returnFlightNumber": back["flightNumber"],
            "returnDepartTime": back["departTime"],
            "returnArriveTime": back["arriveTime"],
            "returnDepartingAt": back["departingAt"],
            "returnArrivingAt": back["arrivingAt"],
            "returnArriveDayOffset": back["arriveDayOffset"],
            "returnDuration": back["duration"],
            "returnStops": back["stops"],
            "returnStopCities": back["stopCities"],
            "returnOrigin": back["origin"],
            "returnDestination": back["destination"],
        })
    return normalized


# ---------------------------------------------------------------------------
# Search & pricing (read-only, no charge)
# ---------------------------------------------------------------------------

def search_offers(origin, destination, departure_date, return_date=None,
                  passengers=1, cabin_class="economy", limit=30, airline=None):
    """
    Search real flight offers. Read-only — never charges.

    Returns a list of normalized offers, or None on any failure. There is no
    generated fallback: the caller shows nothing and explains why.

    When ``airline`` is given (a carrier name or IATA code, e.g. "emirates" or
    "EK"), the results are filtered to that carrier and returned cheapest-first —
    this powers a follow-up like "show me Emirates", which re-queries live
    inventory instead of just filtering whatever was already on screen.
    """
    if not is_configured():
        return None
    if not origin or not destination or not departure_date:
        return None

    slices = [{"origin": origin, "destination": destination, "departure_date": departure_date}]
    if return_date:
        slices.append({"origin": destination, "destination": origin, "departure_date": return_date})

    try:
        count = max(1, min(int(passengers or 1), 9))
    except (TypeError, ValueError):
        count = 1

    payload = {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(count)],
            "cabin_class": cabin_class or "economy",
        }
    }

    # A round trip is priced by more airlines over more combinations, so it takes
    # noticeably longer than a one-way. A read timeout that suits a one-way
    # would cut these off and report "unreachable" for a search that was working.
    response = _request_with_retries(
        "POST",
        _url("/air/offer_requests?return_offers=true"),
        json=payload, headers=_headers(), timeout=75 if return_date else 45,
    )
    if response is None:
        return None

    if response.status_code >= 300:
        logger.warning("Duffel search returned %s: %s", response.status_code, response.text[:300])
        return None

    try:
        offers = response.json()["data"].get("offers", [])
    except (KeyError, ValueError) as error:
        logger.warning("Duffel search parse failed: %s", error)
        return None

    normalized = [normalize_offer(o) for o in offers]
    normalized.sort(key=lambda o: o["price"])

    # Duffel treats an airport code as a metro and may return departures from
    # nearby airports (e.g. ONT/BUR for a LAX search). Those nearby airports
    # often have thin, single-carrier inventory whose cheap fares crowd out the
    # real variety at the airport the user actually asked for. Prefer offers that
    # depart the requested airport so results match the chip on screen — only
    # fall back to the wider metro set if the exact airport has no offers.
    req_o = (origin or "").upper()
    req_d = (destination or "").upper()
    exact = [
        o for o in normalized
        if (o.get("origin") or "").upper() == req_o
        and (o.get("destination") or "").upper() == req_d
    ]
    if exact:
        normalized = exact

    # The same for the journey home: a round trip searched from LAX should come
    # back to LAX, not to Ontario (ONT) 50 miles away. Prefer offers that do;
    # keep the others only if no offer returns to the airport the traveler left.
    if return_date:
        home_again = [
            o for o in normalized
            if (o.get("returnOrigin") or "").upper() == req_d
            and (o.get("returnDestination") or "").upper() == req_o
        ]
        if home_again:
            normalized = home_again

    # Airline-filtered search: keep only the requested carrier (matched on name
    # or IATA code), cheapest first. Returns an empty list when that airline
    # doesn't serve the route, so the caller can say so honestly.
    if airline:
        needle = str(airline).strip().lower()
        filtered = [
            o for o in normalized
            if needle and (
                needle in (o.get("airline") or "").lower()
                or needle == (o.get("airlineCode") or "").lower()
            )
        ]
        if filtered:
            filtered[0].setdefault("tags", [])
        return filtered[:limit]

    # Return a DIVERSE set, not just the absolute cheapest `limit` offers.
    # On long-haul routes there can be dozens of cheap multi-stop fares that
    # would otherwise crowd out a nonstop from a premium carrier (e.g. a
    # nonstop Emirates LAX→DXB sitting behind many cheap 2-stop itineraries).
    # Keep the cheapest offer for each (airline, stops) combination first so the
    # frontend always has good nonstop candidates to rank, then fill the rest
    # by price. Everything stays read-only — this only affects which offers we
    # surface, never what gets booked.
    seen = set()
    diverse = []
    for o in normalized:
        # Stops in each direction: a nonstop out with a two-stop return is a
        # different trip from nonstop both ways, and both deserve a slot.
        key = (o.get("airlineCode"), o.get("stops"), o.get("returnStops"))
        if key not in seen:
            seen.add(key)
            diverse.append(o)
    if len(diverse) < limit:
        chosen = {id(o) for o in diverse}
        for o in normalized:
            if id(o) not in chosen:
                diverse.append(o)
                if len(diverse) >= limit:
                    break

    diverse.sort(key=lambda o: o["price"])
    if diverse:
        diverse[0].setdefault("tags", [])
    return diverse[:limit]


def create_component_client_key():
    """
    Create a component client key for Duffel's card form + 3-D Secure.

    The frontend needs this to render the card form and create the 3DS session.
    Returns ``(key, error)`` — ``error`` is a short reason (e.g. Duffel Payments
    isn't enabled on the account) so the UI can say why card entry is unavailable.
    """
    if not is_configured():
        return None, "not_configured"
    try:
        response = requests.post(
            _url("/identity/component_client_keys"),
            json={"data": {}}, headers=_headers(), timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        logger.warning("Duffel component key request failed (%s)", error)
        return None, "network_error"

    if response.status_code >= 300:
        detail = ""
        try:
            errors = response.json().get("errors", [])
            detail = errors[0].get("message", "") if errors else ""
        except ValueError:
            detail = response.text[:200]
        logger.warning("Duffel component key %s: %s", response.status_code, detail)
        # A 403/422 here almost always means Duffel Payments isn't enabled yet.
        return None, detail or "payments_not_enabled"

    try:
        return response.json()["data"]["component_client_key"], None
    except (KeyError, ValueError):
        return None, "bad_response"


def get_offer(offer_id):
    """
    Re-fetch a single offer to confirm its live price before booking.

    Read-only. Returns the normalized offer, or None if it's gone/expired.
    """
    if not is_configured() or not offer_id:
        return None
    try:
        response = requests.get(
            _url(f"/air/offers/{offer_id}?return_available_services=false"),
            headers=_headers(), timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        logger.warning("Duffel get_offer failed (%s)", error)
        return None
    if response.status_code >= 300:
        return None
    try:
        return normalize_offer(response.json()["data"])
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Booking (creates a REAL order — heavily guarded)
# ---------------------------------------------------------------------------

class BookingError(Exception):
    """A booking was refused before any charge was attempted."""

    def __init__(self, message, code="booking_error", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def create_order(offer_id, passengers, *, confirmed=False, three_d_secure_session_id=None):
    """
    Create a real order for an offer. **This charges money on a live token.**

    Payment model:
      * **Duffel Payments (card):** pass ``three_d_secure_session_id`` (obtained
        by the frontend from Duffel's card component + 3-D Secure). The traveler
        pays with their own card; Flyby never touches card data or fronts money.
      * **Balance:** with no 3DS session, the order is paid from Flyby's Duffel
        balance. Only used if you deliberately run the balance model.

    Refuses unless every guard passes:
      * booking is explicitly enabled (``DUFFEL_BOOKING_ENABLED``)
      * the caller passed ``confirmed=True``
      * the offer's current price is at or below the spend cap
      * full passenger details are supplied

    Raises ``BookingError`` (no charge) when a guard fails; returns Duffel's
    order object on success.
    """
    if not is_configured():
        raise BookingError("Flight booking isn't configured on this backend.", "not_configured", 503)

    if not current_app.config.get("DUFFEL_BOOKING_ENABLED"):
        raise BookingError(
            "Real booking is turned off. Set DUFFEL_BOOKING_ENABLED=true in the "
            "backend .env to enable it.",
            "booking_disabled", 403,
        )

    if not confirmed:
        # The frontend must send an explicit confirmation, so a stray call can
        # never create an order.
        raise BookingError("Booking must be explicitly confirmed.", "not_confirmed", 400)

    if not offer_id:
        raise BookingError("Missing offer to book.", "missing_offer", 400)

    # Re-price against Duffel right now — never trust a price the client sends.
    offer = get_offer(offer_id)
    if offer is None:
        raise BookingError(
            "That fare is no longer available — please search again.",
            "offer_expired", 409,
        )

    cap = current_app.config.get("DUFFEL_MAX_BOOKING_USD", 5000)
    if offer["price"] > cap:
        raise BookingError(
            f"This fare (${offer['price']:,.0f}) is above the ${cap:,.0f} booking "
            f"limit and was not booked.",
            "over_cap", 422,
        )

    # Duffel needs a full passenger record per traveler.
    if not passengers or not isinstance(passengers, list):
        raise BookingError("Passenger details are required to book.", "missing_passengers", 400)

    # Each passenger must carry the offer's own passenger id. Assign them here by
    # position so the client never has to know Duffel's internal ids, and reject
    # a mismatch rather than sending a malformed order.
    offer_passenger_ids = offer.get("passengerIds") or []
    if len(passengers) != len(offer_passenger_ids):
        raise BookingError(
            f"This fare is for {len(offer_passenger_ids)} traveler(s); "
            f"{len(passengers)} were provided.",
            "passenger_count_mismatch", 400,
        )
    for passenger, pid in zip(passengers, offer_passenger_ids):
        passenger["id"] = pid

    # The airline decides whether it needs passport details. Sending the order
    # without them when it does is a guaranteed rejection after the traveler has
    # already authenticated their card, so stop here with something they can fix.
    if offer.get("identityDocumentsRequired"):
        for index, passenger in enumerate(passengers, start=1):
            documents = passenger.get("identity_documents") or []
            complete = any(
                d.get("unique_identifier") and d.get("issuing_country_code") and d.get("expires_on")
                for d in documents
            )
            if not complete:
                raise BookingError(
                    f"This airline requires passport details for traveler {index} "
                    f"(number, issuing country and expiry date).",
                    "identity_document_required", 400,
                )
    else:
        # Not asked for — don't send documents the airline didn't request.
        for passenger in passengers:
            passenger.pop("identity_documents", None)

    # Duffel Payments (traveler's card) when a 3DS session is supplied; otherwise
    # the balance model. The card path is the default for a real product — the
    # traveler pays, Flyby never fronts money or handles card data.
    payment_type = current_app.config.get("DUFFEL_PAYMENT_TYPE", "card")
    # Duffel requires the amount and currency on every payment, and they must
    # match the offer's total. Use the offer's own figure, re-fetched above — not
    # anything the client sent.
    amount = offer.get("totalAmount") or f"{offer['price']:.2f}"
    if three_d_secure_session_id:
        # The card payment used to be sent with no amount or currency. Duffel
        # rejects that as an invalid request, so a traveler would have entered
        # their card, passed 3-D Secure, and then had the booking fail.
        payment = {
            "type": "card",
            "amount": str(amount),
            "currency": offer["currency"],
            "three_d_secure_session_id": three_d_secure_session_id,
        }
    elif payment_type == "card":
        # Card model is configured but the frontend didn't complete the card +
        # 3DS step, so there's nothing to charge — refuse rather than silently
        # falling back to Flyby's balance.
        raise BookingError(
            "Card payment wasn't completed. Enter card details to finish booking.",
            "payment_required", 402,
        )
    else:
        payment = {
            "type": "balance",
            "amount": str(amount),
            "currency": offer["currency"],
        }

    payload = {
        "data": {
            "type": "instant",
            "selected_offers": [offer_id],
            "passengers": passengers,
            "payments": [payment],
        }
    }

    try:
        response = requests.post(
            _url("/air/orders"), json=payload, headers=_headers(), timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        raise BookingError(f"Could not reach the airline: {error}", "network_error", 502)

    if response.status_code >= 300:
        detail = ""
        try:
            errors = response.json().get("errors", [])
            detail = errors[0].get("message", "") if errors else ""
        except ValueError:
            detail = response.text[:200]
        logger.error("Duffel order failed %s: %s", response.status_code, detail)
        raise BookingError(detail or "The airline declined the booking.", "order_failed", 502)

    return response.json().get("data", {})
