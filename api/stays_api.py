"""
Flyby AI — hotel endpoints (``/api/stays``), backed by Duffel Stays.

    POST /api/stays/search   real hotels near a destination (read-only)
    POST /api/stays/rates    rooms and rates for one hotel (read-only)
    POST /api/stays/quote    lock a rate, get the final price (read-only)
    POST /api/stays/book     reserve and pay — guarded, charges money live

Mirrors ``/api/duffel``: the first three are safe to call freely, and booking is
refused unless every guard in ``api.duffel_stays.create_booking`` passes.

When hotels can't be searched, the response carries ``source: "unavailable"``
and a ``reason`` — and no hotels. Nothing is generated in their place.
"""

from flask import Blueprint, g, jsonify, request

from api.jwt_authorize import token_required
from api.duffel import BookingError
from api.duffel_stays import create_booking, create_quote, fetch_rates, search
from api import notify
from model.security import AuditLog

stays_api = Blueprint('stays_api', __name__, url_prefix='/api/stays')


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "unknown")


@stays_api.route('/search', methods=['POST'])
@token_required()
def search_hotels():
    """Real hotels for a destination and dates. Read-only — never charges."""
    data = request.get_json(silent=True) or {}
    hotels, reason = search(
        iata_code=(data.get("destinationCode") or "").strip().upper(),
        city=(data.get("destinationCity") or "").strip(),
        check_in=(data.get("checkIn") or "").strip(),
        check_out=(data.get("checkOut") or "").strip(),
        guests=data.get("guests") or 1,
        rooms=data.get("rooms") or 1,
    )
    if hotels is None:
        return jsonify({"hotels": [], "source": "unavailable", "reason": reason}), 200
    return jsonify({"hotels": hotels, "source": "duffel"}), 200


@stays_api.route('/rates', methods=['POST'])
@token_required()
def rates():
    """Rooms and bookable rates for one hotel from a search."""
    data = request.get_json(silent=True) or {}
    detail, reason = fetch_rates((data.get("searchResultId") or "").strip())
    if detail is None:
        return jsonify({"hotel": None, "rates": [], "error": reason}), 200
    return jsonify(detail), 200


@stays_api.route('/quote', methods=['POST'])
@token_required()
def quote():
    """Lock a rate and return its final, all-in price. Nothing is charged."""
    data = request.get_json(silent=True) or {}
    result, reason = create_quote((data.get("rateId") or "").strip())
    if result is None:
        return jsonify({"quote": None, "error": reason}), 200
    return jsonify({"quote": result}), 200


@stays_api.route('/book', methods=['POST'])
@token_required()
def book():
    """
    Reserve a quoted stay. Charges money on a live token.

    Every guard lives in ``create_booking``. Both the attempt and the outcome
    are written to the audit log.
    """
    data = request.get_json(silent=True) or {}
    quote_id = (data.get("quoteId") or "").strip()
    confirmed = bool(data.get("confirm") or data.get("confirmed"))
    three_ds = (data.get("threeDSecureSessionId") or "").strip() or None

    AuditLog.record(
        g.user_id, "hotel_booking_attempt", True,
        ip_address=_client_ip(), quote_id=quote_id, confirmed=confirmed,
        payment="card" if three_ds else "balance",
    )

    try:
        booking = create_booking(
            quote_id, data.get("guests"), data.get("email"), data.get("phoneNumber"),
            confirmed=confirmed, three_d_secure_session_id=three_ds,
            special_requests=data.get("specialRequests"),
        )
    except BookingError as error:
        AuditLog.record(g.user_id, "hotel_booking_refused", False,
                        ip_address=_client_ip(), reason=error.code)
        return jsonify({"error": error.message, "code": error.code}), error.status

    AuditLog.record(
        g.user_id, "hotel_booked", True, ip_address=_client_ip(),
        booking_id=booking.get("id"), booking_reference=booking.get("reference"),
    )
    notify.hotel_reserved(g.current_user, booking)
    return jsonify({"booking": booking}), 201
