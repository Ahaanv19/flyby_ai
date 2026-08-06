"""
Flyby AI — Duffel flight search and booking.

Real airline inventory and real orders through Duffel. Two very different risk
levels live here, and the code treats them differently:

* **Search & pricing are read-only** — an offer request or offer lookup never
  charges anything, so they run freely (and fall back to the local generator
  when Duffel is unavailable, so the app always shows flights).

* **Booking creates a real order.** With a ``duffel_live_`` token that charges
  real money and issues a real ticket, so it is gated three ways: it is disabled
  unless ``DUFFEL_BOOKING_ENABLED`` is set, it refuses any order above
  ``DUFFEL_MAX_BOOKING_USD``, and it requires the caller to pass an explicit
  confirmation. A bug or bad input therefore cannot silently book a flight.

Everything the frontend needs to book — most importantly the Duffel ``offer_id``
and its expiry — is carried through the normalized offer shape.
"""

import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

TIMEOUT = 30


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
    """Turn an ISO-8601 duration like 'PT5H30M' into '5h 30m'."""
    if not iso or not iso.startswith("PT"):
        return ""
    body = iso[2:]
    hours = minutes = 0
    num = ""
    for ch in body:
        if ch.isdigit():
            num += ch
        elif ch == "H":
            hours = int(num or 0)
            num = ""
        elif ch == "M":
            minutes = int(num or 0)
            num = ""
    return f"{hours}h {minutes:02d}m"


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
    """
    slices = offer.get("slices") or []
    first_slice = slices[0] if slices else {}
    segments = first_slice.get("segments") or []
    first_seg = segments[0] if segments else {}
    last_seg = segments[-1] if segments else {}

    owner = offer.get("owner") or {}
    carrier = first_seg.get("marketing_carrier") or owner
    flight_number = first_seg.get("marketing_carrier_flight_number") or ""

    stops = max(0, len(segments) - 1)
    stop_city = None
    if stops:
        mid = segments[0].get("destination") or {}
        stop_city = mid.get("iata_code")

    try:
        price = round(float(offer.get("total_amount") or 0), 2)
    except (TypeError, ValueError):
        price = 0.0

    tags = []
    if stops == 0:
        tags.append("Nonstop")

    # Order creation must echo back the offer's own passenger ids, so carry them.
    passenger_ids = [p.get("id") for p in (offer.get("passengers") or []) if p.get("id")]

    return {
        "id": offer.get("id"),                       # Duffel offer id — book with this
        "passengerIds": passenger_ids,               # required when creating the order
        "expiresAt": offer.get("expires_at"),        # offer expiry
        "airline": owner.get("name") or carrier.get("name") or "Airline",
        "airlineLogo": owner.get("logo_symbol_url"),
        "airlineCode": owner.get("iata_code") or carrier.get("iata_code") or "",
        "flightNumber": f"{carrier.get('iata_code', '')}{flight_number}".strip(),
        "departTime": _time_label(first_seg.get("departing_at")),
        "arriveTime": _time_label(last_seg.get("arriving_at")),
        "departureTime": _time_label(first_seg.get("departing_at")),
        "arrivalTime": _time_label(last_seg.get("arriving_at")),
        "departingAt": first_seg.get("departing_at"),
        "arrivingAt": last_seg.get("arriving_at"),
        "duration": _iso_duration_to_label(first_slice.get("duration")),
        "stops": stops,
        "stopCity": stop_city,
        "price": price,
        "currency": offer.get("total_currency") or "USD",
        "origin": (first_seg.get("origin") or {}).get("iata_code") or "",
        "destination": (last_seg.get("destination") or {}).get("iata_code") or "",
        "cabinClass": (offer.get("cabin_class") or "economy"),
        "passengerCount": len(offer.get("passengers") or []) or 1,
        "source": "duffel",
    }


# ---------------------------------------------------------------------------
# Search & pricing (read-only, no charge)
# ---------------------------------------------------------------------------

def search_offers(origin, destination, departure_date, return_date=None,
                  passengers=1, cabin_class="economy", limit=30):
    """
    Search real flight offers. Read-only — never charges.

    Returns a list of normalized offers, or None on any failure so the caller
    can fall back to the local generator.
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

    try:
        response = requests.post(
            _url("/air/offer_requests?return_offers=true"),
            json=payload, headers=_headers(), timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        logger.warning("Duffel search failed (%s); falling back to local flights", error)
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
    # Cheapest first, with a "Recommended" tag on the top pick.
    normalized.sort(key=lambda o: o["price"])
    if normalized:
        normalized[0].setdefault("tags", [])
    return normalized[:limit]


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

    # Duffel Payments (traveler's card) when a 3DS session is supplied; otherwise
    # the balance model. The card path is the default for a real product — the
    # traveler pays, Flyby never fronts money or handles card data.
    payment_type = current_app.config.get("DUFFEL_PAYMENT_TYPE", "card")
    if three_d_secure_session_id:
        payment = {"type": "card", "three_d_secure_session_id": three_d_secure_session_id}
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
            "amount": str(offer["price"]),
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
