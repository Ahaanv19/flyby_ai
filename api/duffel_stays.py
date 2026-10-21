"""
Flyby AI — real hotels through Duffel Stays.

Search returns real properties with real, bookable prices; booking creates a
real reservation the hotel will honour. It replaces a generator that produced
plausible-looking hotels ("Fairfield Inn Gateway Downtown", Dubai) that did not
exist and that nobody had reserved.

The flow is Duffel's, in four steps, and each maps to one function here:

    search()        properties near the destination, with their cheapest price
    fetch_rates()   every room and rate for one property
    create_quote()  lock a rate and get the final, all-in price
    create_booking()  pay and reserve — **this charges money on a live token**

As with flights, the first three are read-only and run freely. Booking is
guarded the same way a flight order is: off unless ``DUFFEL_BOOKING_ENABLED``,
refused above ``DUFFEL_MAX_BOOKING_USD``, and refused without an explicit
confirmation. The price is always re-read from Duffel, never taken from the
client.

Nothing here invents data. When Stays can't be reached, or isn't enabled on the
Duffel account, every function says so with a reason and returns no hotels —
the app then shows none and explains why.

Shapes follow Duffel's published API (``/stays/search``, ``/stays/quotes``,
``/stays/bookings``) as defined in their official SDK.
"""

import logging
import math
from datetime import date

import requests
from flask import current_app

from api.duffel import (BookingError, _headers, _request_with_retries, _url,
                        is_configured)

logger = logging.getLogger(__name__)

# How far from the search centre to look, in kilometres.
_CITY_RADIUS_KM = 10

# City centres for the places business travelers go most — the business district
# or main downtown, where a meeting is likeliest to be. Duffel's city entries
# carry no coordinates (checked: New York, London, Chicago, Dubai, San Diego,
# Tokyo, Paris, Seattle), so without these every hotel search would centre on
# an airport: JFK for a Manhattan meeting, Heathrow for one in the City.
# Anywhere not listed falls back to the arrival airport with a wider radius,
# and distances are then labelled as "from the airport".
_CITY_CENTRES = {
    "new york": (40.7549, -73.9840), "new york city": (40.7549, -73.9840), "manhattan": (40.7549, -73.9840),
    "los angeles": (34.0522, -118.2437), "san francisco": (37.7897, -122.3972), "chicago": (41.8837, -87.6324),
    "boston": (42.3555, -71.0565), "washington": (38.9007, -77.0366), "washington dc": (38.9007, -77.0366),
    "seattle": (47.6080, -122.3352), "san diego": (32.7157, -117.1611), "austin": (30.2672, -97.7431),
    "dallas": (32.7809, -96.7999), "houston": (29.7604, -95.3698), "denver": (39.7468, -104.9925),
    "atlanta": (33.7537, -84.3863), "miami": (25.7743, -80.1937), "las vegas": (36.1147, -115.1728),
    "phoenix": (33.4484, -112.0740), "philadelphia": (39.9526, -75.1652), "minneapolis": (44.9778, -93.2650),
    "detroit": (42.3314, -83.0458), "nashville": (36.1627, -86.7816), "portland": (45.5190, -122.6795),
    "salt lake city": (40.7608, -111.8910), "orlando": (28.5384, -81.3789), "charlotte": (35.2271, -80.8431),
    "toronto": (43.6487, -79.3817), "vancouver": (49.2849, -123.1186), "montreal": (45.5017, -73.5673),
    "mexico city": (19.4326, -99.1332), "sao paulo": (-23.5613, -46.6565), "são paulo": (-23.5613, -46.6565),
    "london": (51.5123, -0.0909), "paris": (48.8708, 2.3220), "frankfurt": (50.1109, 8.6821),
    "berlin": (52.5200, 13.4050), "munich": (48.1374, 11.5755), "amsterdam": (52.3676, 4.9041),
    "madrid": (40.4168, -3.7038), "barcelona": (41.3874, 2.1686), "dublin": (53.3438, -6.2546),
    "zurich": (47.3769, 8.5417), "geneva": (46.2044, 6.1432), "milan": (45.4642, 9.1900),
    "rome": (41.9028, 12.4964), "stockholm": (59.3326, 18.0649), "copenhagen": (55.6761, 12.5683),
    "brussels": (50.8467, 4.3525), "lisbon": (38.7223, -9.1393), "istanbul": (41.0422, 29.0083),
    "dubai": (25.2048, 55.2708), "abu dhabi": (24.4539, 54.3773), "doha": (25.3205, 51.5300),
    "riyadh": (24.7136, 46.6753), "tel aviv": (32.0853, 34.7818), "mumbai": (19.0600, 72.8656),
    "delhi": (28.6315, 77.2167), "new delhi": (28.6315, 77.2167), "bangalore": (12.9716, 77.5946),
    "bengaluru": (12.9716, 77.5946), "singapore": (1.2834, 103.8510), "hong kong": (22.2800, 114.1588),
    "tokyo": (35.6812, 139.7671), "osaka": (34.7025, 135.4959), "seoul": (37.5665, 126.9780),
    "shanghai": (31.2304, 121.4737), "beijing": (39.9042, 116.4074), "sydney": (-33.8688, 151.2093),
    "melbourne": (-37.8136, 144.9631), "johannesburg": (-26.1076, 28.0567),
}
# When only the airport's position is known, the centre of town can be a long
# way off (Denver's airport is ~37 km out), so cast wider.
_AIRPORT_RADIUS_KM = 30

# IATA code / city -> (lat, lng, radius). Places don't move; cache for the life
# of the process so a trip search doesn't pay for a lookup every time.
_COORDINATE_CACHE = {}

# What the last hotel search found out, for the operations console. Travelers
# only see "hotel booking isn't available"; whoever runs Flyby needs the real
# reason, in Duffel's own words, to get it fixed.
LAST_STATUS = {"checked_at": None, "ok": None, "reason": None, "message": None}


def _record_status(ok, reason=None, message=None):
    from datetime import datetime, timezone
    LAST_STATUS.update(checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                       ok=ok, reason=reason, message=(message or "")[:300] or None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nights(check_in, check_out):
    try:
        return max(1, (date.fromisoformat(check_out[:10]) - date.fromisoformat(check_in[:10])).days)
    except (TypeError, ValueError):
        return 1


def _distance_km(lat1, lng1, lat2, lng2):
    """Great-circle distance between two points."""
    try:
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = p2 - p1, math.radians(lng2 - lng1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    except (TypeError, ValueError):
        return None


def _error_reason(response):
    """A short machine-readable reason from a Duffel error response."""
    if response is None:
        return "network_error"
    # Stays is switched on per account. A token that works for flights but has
    # no Stays access is refused here — worth telling apart from "no hotels".
    if response.status_code in (401, 403):
        return "not_enabled"
    try:
        errors = response.json().get("errors") or []
        code = (errors[0].get("code") or errors[0].get("type") or "") if errors else ""
    except ValueError:
        code = ""
    if code in ("unavailable_feature", "access_restricted", "feature_not_enabled"):
        return "not_enabled"
    return code or "request_failed"


def _error_message(response):
    """Duffel's explanation. Some refusals come as plain text, not JSON —
    "This feature is not enabled for your account…" among them."""
    try:
        errors = response.json().get("errors") or []
        return (errors[0].get("message") or "") if errors else ""
    except (ValueError, AttributeError):
        try:
            return (response.text or "").strip()[:300]
        except AttributeError:
            return ""


# ---------------------------------------------------------------------------
# Where to search
# ---------------------------------------------------------------------------

def coordinates_for(iata_code=None, city=None):
    """
    Find the point to search around: the city centre if Duffel knows it,
    otherwise the airport.

    Returns ``(centre, reason)``. ``centre`` is ``(latitude, longitude,
    radius_km, kind)`` — ``kind`` is "city" or "airport", so a distance can be
    labelled with what it is measured from — or ``None`` when the place can't be
    located, in which case there is no honest way to search. ``reason`` then
    says whether Duffel couldn't be reached ("network_error") or was reached and
    doesn't know the place ("location_unknown"): to the traveler those are very
    different problems.
    """
    code = (iata_code or "").strip().upper()
    city_name = (city or "").split(",")[0].strip()
    cache_key = (code, city_name.lower())
    if cache_key in _COORDINATE_CACHE:
        return _COORDINATE_CACHE[cache_key], None

    reached = {"any": False}

    def lookup(query):
        response = _request_with_retries(
            "GET", _url("/places/suggestions"), params={"query": query}, headers=_headers(),
        )
        if response is None:
            return []
        reached["any"] = True
        if response.status_code >= 300:
            return []
        try:
            return response.json().get("data") or []
        except ValueError:
            return []

    found = None

    # 0. A known business-centre for the city.
    if city_name.lower() in _CITY_CENTRES:
        lat, lng = _CITY_CENTRES[city_name.lower()]
        found = (lat, lng, _CITY_RADIUS_KM, "city")
        _COORDINATE_CACHE[cache_key] = found
        return found, None

    # 1. The city itself, by name, should Duffel ever give it coordinates.
    if city_name:
        for place in lookup(city_name):
            if place.get("type") == "city" and place.get("latitude") is not None \
                    and place.get("longitude") is not None \
                    and (place.get("name") or "").lower() == city_name.lower():
                found = (float(place["latitude"]), float(place["longitude"]), _CITY_RADIUS_KM, "city")
                break

    # 2. Otherwise the airport the traveler is flying into.
    if found is None and code:
        for place in lookup(code):
            candidates = [place] + list(place.get("airports") or [])
            for candidate in candidates:
                if (candidate.get("iata_code") or "").upper() == code \
                        and candidate.get("latitude") is not None \
                        and candidate.get("longitude") is not None:
                    found = (float(candidate["latitude"]), float(candidate["longitude"]),
                             _AIRPORT_RADIUS_KM, "airport")
                    break
            if found:
                break

    if found is not None:
        _COORDINATE_CACHE[cache_key] = found
        return found, None
    return None, ("location_unknown" if reached["any"] else "network_error")


# ---------------------------------------------------------------------------
# Normalization — Duffel shapes -> what the hotel UI renders
# ---------------------------------------------------------------------------

def _address_line(address):
    parts = [address.get("line_one"), address.get("city_name"), address.get("postal_code")]
    return ", ".join(p for p in parts if p)


def normalize_result(result, centre=None):
    """One Duffel search result -> a hotel the UI can list."""
    accommodation = result.get("accommodation") or {}
    location = accommodation.get("location") or {}
    address = location.get("address") or {}
    coordinates = location.get("geographic_coordinates") or {}

    check_in = result.get("check_in_date") or ""
    check_out = result.get("check_out_date") or ""
    nights = _nights(check_in, check_out)
    total = _to_float(result.get("cheapest_rate_total_amount")) or 0.0
    due_at_hotel = _to_float(result.get("cheapest_rate_due_at_accommodation_amount")) or 0.0

    # Guest reviews are scored out of 10; the UI shows a score out of 5. The
    # official star class is carried separately — they are different things.
    review_score = _to_float(accommodation.get("review_score"))
    stars = accommodation.get("rating")

    distance_km = None
    if centre and coordinates.get("latitude") is not None and coordinates.get("longitude") is not None:
        distance_km = _distance_km(centre[0], centre[1],
                                   coordinates["latitude"], coordinates["longitude"])

    return {
        "id": result.get("id"),                      # search result id — fetch rates with this
        "accommodationId": accommodation.get("id"),
        "name": accommodation.get("name") or "",
        "area": address.get("city_name") or "",
        "address": _address_line(address),
        "chain": (accommodation.get("chain") or {}).get("name"),
        "brand": (accommodation.get("brand") or {}).get("name"),
        "pricePerNight": round(total / nights, 2) if total else 0.0,
        "totalPrice": round(total, 2),
        "totalAmount": result.get("cheapest_rate_total_amount"),
        "currency": result.get("cheapest_rate_currency") or "USD",
        "dueAtAccommodation": round(due_at_hotel, 2),
        "nights": nights,
        "checkInDate": check_in,
        "checkOutDate": check_out,
        "starRating": stars,
        "reviewScore": review_score,
        "rating": round(review_score / 2, 1) if review_score is not None else (stars or 0),
        "reviewCount": accommodation.get("review_count") or 0,
        "images": [p.get("url") for p in (accommodation.get("photos") or []) if p.get("url")][:8],
        "amenities": [a.get("description") for a in (accommodation.get("amenities") or [])
                      if a.get("description")],
        "description": accommodation.get("description") or "",
        "distanceKm": round(distance_km, 1) if distance_km is not None else None,
        # What the distance is from, so the UI never implies "from your meeting".
        "distanceFrom": (centre[3] if centre and len(centre) > 3 else None) if distance_km is not None else None,
        "expiresAt": result.get("expires_at"),
        "source": "duffel",
    }


def normalize_rate(rate, room_name=None):
    """One bookable rate for a room."""
    total = _to_float(rate.get("total_amount")) or 0.0
    timeline = rate.get("cancellation_timeline") or []
    # Free cancellation means a full refund is still on offer at some point.
    refundable_until = None
    for step in timeline:
        if _to_float(step.get("refund_amount")) is not None \
                and abs((_to_float(step.get("refund_amount")) or 0) - total) < 0.01:
            refundable_until = step.get("before")
    return {
        "id": rate.get("id"),
        "roomName": room_name or rate.get("name") or "Room",
        "boardType": rate.get("board_type") or "room_only",
        "totalPrice": round(total, 2),
        "totalAmount": rate.get("total_amount"),
        "currency": rate.get("total_currency") or "USD",
        "taxAmount": _to_float(rate.get("tax_amount")),
        "feeAmount": _to_float(rate.get("fee_amount")),
        "dueAtAccommodation": round(_to_float(rate.get("due_at_accommodation_amount")) or 0.0, 2),
        "paymentType": rate.get("payment_type"),
        "paymentMethods": rate.get("available_payment_methods") or [],
        "loyaltyProgrammeRequired": bool(rate.get("loyalty_programme_required")),
        "refundableUntil": refundable_until,
        "cancellationTimeline": [
            {"before": step.get("before"),
             "refundAmount": _to_float(step.get("refund_amount")),
             "currency": step.get("currency")}
            for step in timeline
        ],
        "conditions": [
            {"title": c.get("title") or "", "description": c.get("description") or ""}
            for c in (rate.get("conditions") or [])
        ],
        "quantityAvailable": rate.get("quantity_available"),
        "expiresAt": rate.get("expires_at"),
    }


def _payment_method():
    """The Duffel payment method Flyby is configured to use for bookings."""
    return "card" if current_app.config.get("DUFFEL_PAYMENT_TYPE", "card") == "card" else "balance"


# ---------------------------------------------------------------------------
# Search & pricing (read-only, no charge)
# ---------------------------------------------------------------------------

def search(iata_code, check_in, check_out, city=None, guests=1, rooms=1, limit=60):
    """
    Real hotels near the destination. Read-only — never charges.

    Returns ``(hotels, reason)``. ``hotels`` is a list (possibly empty, when
    Duffel was reached and simply had nothing) or ``None`` when no search could
    be made; ``reason`` then says why.
    """
    if not is_configured():
        return None, "not_configured"
    if not check_in or not check_out or check_out <= check_in:
        return None, "invalid_dates"

    centre, location_reason = coordinates_for(iata_code, city)
    if centre is None:
        if location_reason == "network_error":
            _record_status(False, "network_error", "Couldn't reach Duffel.")
        return None, location_reason

    try:
        guest_count = max(1, min(int(guests or 1), 8))
        room_count = max(1, min(int(rooms or 1), guest_count))
    except (TypeError, ValueError):
        guest_count, room_count = 1, 1

    payload = {"data": {
        "rooms": room_count,
        "location": {
            "radius": centre[2],
            "geographic_coordinates": {"latitude": centre[0], "longitude": centre[1]},
        },
        "check_in_date": check_in[:10],
        "check_out_date": check_out[:10],
        "guests": [{"type": "adult"} for _ in range(guest_count)],
    }}

    response = _request_with_retries(
        "POST", _url("/stays/search"), json=payload, headers=_headers(), timeout=60,
    )
    if response is None or response.status_code >= 300:
        reason = _error_reason(response)
        if response is not None:
            logger.warning("Duffel Stays search %s: %s", response.status_code, response.text[:300])
        _record_status(False, reason,
                       (f"HTTP {response.status_code}: {_error_message(response)}" if response is not None
                        else "Couldn't reach Duffel."))
        return None, reason

    try:
        results = (response.json().get("data") or {}).get("results") or []
    except ValueError:
        return None, "bad_response"

    _record_status(True)
    hotels = [normalize_result(r, centre) for r in results]
    # A result with no price can't be booked, so it isn't offered.
    hotels = [h for h in hotels if h["id"] and h["name"] and h["totalPrice"] > 0]
    hotels.sort(key=lambda h: h["totalPrice"])
    return hotels[:limit], None


def fetch_rates(search_result_id):
    """
    Every room and rate for one property. Read-only.

    Returns ``(detail, reason)`` where ``detail`` is ``{hotel, rates}`` with
    rates Flyby can actually pay for, cheapest first.
    """
    if not is_configured():
        return None, "not_configured"
    if not search_result_id:
        return None, "missing_search_result"

    # No request body — the search result id in the path is the whole request.
    response = _request_with_retries(
        "POST", _url(f"/stays/search_results/{search_result_id}/actions/fetch_all_rates"),
        headers=_headers(), timeout=60,
    )
    if response is None or response.status_code >= 300:
        if response is not None and response.status_code in (404, 410, 422):
            # Search results are short-lived; an old one is gone, not broken.
            return None, "search_expired"
        return None, _error_reason(response)

    try:
        result = response.json().get("data") or {}
    except ValueError:
        return None, "bad_response"

    method = _payment_method()
    rates = []
    for room in (result.get("accommodation") or {}).get("rooms") or []:
        for rate in room.get("rates") or []:
            normalized = normalize_rate(rate, room.get("name"))
            # Only rates this account can actually pay for, none that need a
            # loyalty number we don't collect, and only "pay now" rates: those
            # are charged in full at booking, so the price shown is the price
            # paid. Guarantee and deposit rates settle partly or wholly at the
            # property, on terms this flow doesn't present.
            if method in normalized["paymentMethods"] \
                    and not normalized["loyaltyProgrammeRequired"] \
                    and normalized["paymentType"] == "pay_now":
                rates.append(normalized)
    rates.sort(key=lambda r: r["totalPrice"])
    return {"hotel": normalize_result(result), "rates": rates}, None


def create_quote(rate_id):
    """
    Lock a rate and get its final price. Read-only — a quote reserves nothing
    and charges nothing; it is what a booking is then made against.
    """
    if not is_configured():
        return None, "not_configured"
    if not rate_id:
        return None, "missing_rate"

    response = _request_with_retries(
        "POST", _url("/stays/quotes"), json={"data": {"rate_id": rate_id}},
        headers=_headers(), timeout=60,
    )
    if response is None or response.status_code >= 300:
        if response is not None and response.status_code in (404, 410, 422):
            return None, "rate_unavailable"
        return None, _error_reason(response)
    try:
        return normalize_quote(response.json().get("data") or {}), None
    except ValueError:
        return None, "bad_response"


def get_quote(quote_id):
    """Re-read a quote, to confirm its price immediately before paying."""
    if not is_configured() or not quote_id:
        return None
    response = _request_with_retries("GET", _url(f"/stays/quotes/{quote_id}"), headers=_headers())
    if response is None or response.status_code >= 300:
        return None
    try:
        return normalize_quote(response.json().get("data") or {})
    except ValueError:
        return None


def normalize_quote(quote):
    accommodation = quote.get("accommodation") or {}
    address = ((accommodation.get("location") or {}).get("address") or {})
    check_in = quote.get("check_in_date") or ""
    check_out = quote.get("check_out_date") or ""
    total = _to_float(quote.get("total_amount")) or 0.0
    return {
        "id": quote.get("id"),
        "hotelName": accommodation.get("name") or "",
        "address": _address_line(address),
        "checkInDate": check_in,
        "checkOutDate": check_out,
        "nights": _nights(check_in, check_out),
        "totalPrice": round(total, 2),
        "totalAmount": quote.get("total_amount"),
        "currency": quote.get("total_currency") or "USD",
        "taxAmount": _to_float(quote.get("tax_amount")),
        "feeAmount": _to_float(quote.get("fee_amount")),
        "dueAtAccommodation": round(_to_float(quote.get("due_at_accommodation_amount")) or 0.0, 2),
        "rooms": quote.get("rooms") or 1,
        "guestCount": len(quote.get("guests") or []) or 1,
    }


# ---------------------------------------------------------------------------
# Booking (creates a REAL reservation — guarded like a flight order)
# ---------------------------------------------------------------------------

def create_booking(quote_id, guests, email, phone_number, *, confirmed=False,
                   three_d_secure_session_id=None, special_requests=None):
    """
    Reserve and pay for a quoted stay. **This charges money on a live token.**

    Raises ``BookingError`` (no charge) when a guard fails; returns Duffel's
    booking on success.
    """
    if not is_configured():
        raise BookingError("Hotel booking isn't configured on this backend.", "not_configured", 503)

    if not current_app.config.get("DUFFEL_BOOKING_ENABLED"):
        raise BookingError(
            "Real booking is turned off. Set DUFFEL_BOOKING_ENABLED=true in the "
            "backend .env to enable it.",
            "booking_disabled", 403,
        )

    if not confirmed:
        raise BookingError("Booking must be explicitly confirmed.", "not_confirmed", 400)

    if not quote_id:
        raise BookingError("Missing the hotel quote to book.", "missing_quote", 400)

    # Re-price against Duffel right now — never trust a price the client sends.
    quote = get_quote(quote_id)
    if quote is None:
        raise BookingError(
            "That hotel rate is no longer available — please choose it again.",
            "quote_expired", 409,
        )

    cap = current_app.config.get("DUFFEL_MAX_BOOKING_USD", 5000)
    if quote["totalPrice"] > cap:
        raise BookingError(
            f"This stay (${quote['totalPrice']:,.0f}) is above the ${cap:,.0f} booking "
            f"limit and was not booked.",
            "over_cap", 422,
        )

    clean_guests = []
    for guest in guests if isinstance(guests, list) else []:
        given = str((guest or {}).get("given_name") or "").strip()
        family = str((guest or {}).get("family_name") or "").strip()
        if given and family:
            clean_guests.append({"given_name": given, "family_name": family})
    if not clean_guests:
        raise BookingError("The guest's first and last name are required.", "missing_guests", 400)

    email = str(email or "").strip()
    phone_number = str(phone_number or "").strip()
    if not email or not phone_number:
        raise BookingError("An email and phone number are required for the reservation.",
                           "missing_contact", 400)

    data = {
        "quote_id": quote_id,
        "guests": clean_guests,
        "email": email,
        "phone_number": phone_number,
    }
    if special_requests:
        data["accommodation_special_requests"] = str(special_requests)[:500]

    if three_d_secure_session_id:
        data["payment"] = {"three_d_secure_session_id": three_d_secure_session_id}
    elif _payment_method() == "card":
        # Card model is configured but no card step was completed — refuse
        # rather than silently charging Flyby's balance.
        raise BookingError(
            "Card payment wasn't completed. Enter card details to finish booking.",
            "payment_required", 402,
        )
    # Balance model: Duffel charges the account balance when no payment is given.

    # Sent exactly once. Every other call here retries a dropped connection, but
    # a booking must not: if the connection drops after Duffel has accepted it,
    # a retry would reserve — and charge for — the same room twice.
    try:
        response = requests.post(
            _url("/stays/bookings"), json={"data": data}, headers=_headers(), timeout=90,
        )
    except requests.RequestException as error:
        logger.error("Duffel Stays booking request failed: %s", error)
        raise BookingError(
            "The connection dropped while reserving, so it's unclear whether the hotel "
            "accepted it. Check with the hotel before trying again.",
            "network_error", 502,
        )
    if response.status_code >= 300:
        detail = _error_message(response)
        logger.error("Duffel Stays booking failed %s: %s", response.status_code, detail)
        raise BookingError(detail or "The hotel declined the reservation.", "booking_failed", 502)

    try:
        booking = response.json().get("data") or {}
    except ValueError:
        raise BookingError("The reservation response couldn't be read. Check your bookings "
                           "before trying again.", "bad_response", 502)

    return {
        "id": booking.get("id"),
        "reference": booking.get("reference"),
        "status": booking.get("status"),
        "hotelName": (booking.get("accommodation") or {}).get("name") or quote["hotelName"],
        "checkInDate": booking.get("check_in_date") or quote["checkInDate"],
        "checkOutDate": booking.get("check_out_date") or quote["checkOutDate"],
        "totalAmount": quote["totalAmount"],
        "totalPrice": quote["totalPrice"],
        "currency": quote["currency"],
        "keyCollection": ((booking.get("key_collection") or {}).get("instructions")),
    }
