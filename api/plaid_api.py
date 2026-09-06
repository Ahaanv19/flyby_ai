"""
Flyby AI — Plaid endpoints (``/api/plaid``).

    GET  /api/plaid/status       whether card linking is available
    POST /api/plaid/link-token   start the Plaid Link flow
    POST /api/plaid/exchange     finish linking, store the access token
    POST /api/plaid/sync         pull new charges and return them as expenses

All read-only with respect to the traveler's bank: Plaid's Transactions product
can only read activity. No card number ever reaches Flyby.

Until PLAID_CLIENT_ID / PLAID_SECRET are set, every route reports
``configured: false`` rather than failing, so the app behaves exactly as it does
today and the UI can explain that card linking isn't live yet.
"""

from flask import Blueprint, g, jsonify, request

from api.jwt_authorize import token_required
from api.plaid import (build_expenses, create_link_token, exchange_public_token,
                       is_configured, sync_transactions)

plaid_api = Blueprint('plaid_api', __name__, url_prefix='/api/plaid')


@plaid_api.route('/status', methods=['GET'])
@token_required()
def status():
    """Whether the traveler can link a card yet."""
    return jsonify({"configured": is_configured()}), 200


@plaid_api.route('/link-token', methods=['POST'])
@token_required()
def link_token():
    """Create the token that opens Plaid Link in the browser."""
    if not is_configured():
        return jsonify({"configured": False, "linkToken": None,
                        "error": "Card linking isn't set up yet."}), 200

    user = getattr(g, "current_user", None)
    user_id = getattr(user, "uuid", None) or getattr(user, "id", "anonymous")
    data, error = create_link_token(user_id)
    if error:
        return jsonify({"configured": True, "linkToken": None, "error": error}), 200
    return jsonify({"configured": True, "linkToken": data.get("link_token")}), 200


@plaid_api.route('/exchange', methods=['POST'])
@token_required()
def exchange():
    """
    Finish linking: swap Link's one-time public token for a durable access token.

    The access token is an opaque handle used to read activity — it is not card
    data. It is returned to the caller to persist against the traveler.
    """
    if not is_configured():
        return jsonify({"configured": False, "error": "Card linking isn't set up yet."}), 200

    payload = request.get_json(silent=True) or {}
    public_token = (payload.get("publicToken") or payload.get("public_token") or "").strip()
    if not public_token:
        return jsonify({"error": "Missing public token."}), 400

    data, error = exchange_public_token(public_token)
    if error:
        return jsonify({"configured": True, "error": error}), 200
    return jsonify({
        "configured": True,
        "accessToken": data.get("access_token"),
        "itemId": data.get("item_id"),
    }), 200


@plaid_api.route('/sync', methods=['POST'])
@token_required()
def sync():
    """
    Pull new card charges and return them already shaped as Flyby expenses,
    matched to the trip that was running when each charge happened.

    Body: ``{accessToken, cursor?, trips?: [{id, name, start, end}]}``
    Returns ``{expenses, cursor, hasMore}`` — the caller persists the expenses
    and stores the cursor so the next sync only fetches what's new.
    """
    if not is_configured():
        return jsonify({"configured": False, "expenses": [], "cursor": None,
                        "hasMore": False}), 200

    payload = request.get_json(silent=True) or {}
    access_token = (payload.get("accessToken") or payload.get("access_token") or "").strip()
    if not access_token:
        return jsonify({"error": "Missing access token."}), 400

    data, error = sync_transactions(access_token, payload.get("cursor"))
    if error:
        return jsonify({"configured": True, "expenses": [], "cursor": payload.get("cursor"),
                        "hasMore": False, "error": error}), 200

    added = data.get("added") or []
    expenses = build_expenses(added, payload.get("trips") or [])
    return jsonify({
        "configured": True,
        "expenses": expenses,
        "cursor": data.get("next_cursor"),
        "hasMore": bool(data.get("has_more")),
        "removed": [r.get("transaction_id") for r in (data.get("removed") or [])],
    }), 200
