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

from flask import Blueprint, current_app, g, jsonify, request

from api.jwt_authorize import token_required
from api.duffel import (BookingError, create_component_client_key, create_order,
                        get_offer, is_configured, is_live, search_offers)
from model.security import AuditLog

duffel_api = Blueprint('duffel_api', __name__, url_prefix='/api/duffel')


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "unknown")


@duffel_api.route('/status', methods=['GET'])
@token_required()
def status():
    """Report what's available, so the UI can label real vs simulated flights."""
    return jsonify({
        "configured": is_configured(),
        "live": is_live(),
        "bookingEnabled": bool(current_app.config.get("DUFFEL_BOOKING_ENABLED")),
        "maxBookingUsd": current_app.config.get("DUFFEL_MAX_BOOKING_USD"),
    }), 200


@duffel_api.route('/search', methods=['POST'])
@token_required()
def search():
    """
    Search real flight offers. Read-only — never charges.

    Returns ``{offers, source}`` where source is "duffel" (real inventory) or
    "unavailable" (the caller should fall back to its local flight generator).
    """
    data = request.get_json(silent=True) or {}

    offers = search_offers(
        origin=(data.get("origin") or "").strip().upper(),
        destination=(data.get("destination") or "").strip().upper(),
        departure_date=data.get("departureDate") or data.get("departure_date"),
        return_date=data.get("returnDate") or data.get("return_date"),
        passengers=data.get("passengers") or 1,
        cabin_class=data.get("cabinClass") or "economy",
    )

    if offers is None:
        return jsonify({"offers": [], "source": "unavailable"}), 200
    return jsonify({"offers": offers, "source": "duffel", "live": is_live()}), 200


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
