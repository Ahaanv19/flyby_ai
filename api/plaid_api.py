"""
Flyby AI — connected-card endpoints (``/api/plaid``).

    GET  /api/plaid/status       is card linking available, and which cards are linked
    POST /api/plaid/link-token   start Plaid Link
    POST /api/plaid/exchange     finish linking; the access token stays on the server
    POST /api/plaid/sync         pull new charges; return the ones not yet filed
    POST /api/plaid/ack          the app filed these charges — don't offer them again
    POST /api/plaid/disconnect   unlink a card (revokes Flyby's access at Plaid)
    POST /api/plaid/webhook      Plaid says new charges are ready (production only)

Read-only with respect to the traveler's bank: Plaid's Transactions product can
only read activity. No card number ever reaches Flyby.

The Plaid access token is a standing key to someone's bank transactions, so it
is stored server-side, per user, and never returned to the browser. (It used to
be handed to the browser and kept in localStorage: readable by any script on the
page, tied to one device, and shared by whoever signed in on it next. No card
was ever linked that way — Plaid keys weren't configured — so nothing migrates.)

Until PLAID_CLIENT_ID / PLAID_SECRET are set, every route reports
``configured: false`` rather than failing.
"""

from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request

from __init__ import db
from api.jwt_authorize import token_required
from api.plaid import (_trip_windows, create_link_token, exchange_public_token,
                       is_configured, offerable, pull_item, remove_item)
from model.plaid import PlaidItem, PlaidTransaction
from model.security import AuditLog

plaid_api = Blueprint('plaid_api', __name__, url_prefix='/api/plaid')


def _user():
    return getattr(g, "current_user", None)


def _cards(user_uuid):
    return [item.read() for item in PlaidItem.for_user(user_uuid)]


@plaid_api.route('/status', methods=['GET'])
@token_required()
def status():
    """Whether card linking is available, and the cards this account has linked."""
    user = _user()
    cards = _cards(user.uuid) if (user and is_configured()) else []
    return jsonify({"configured": is_configured(), "connected": bool(cards), "cards": cards}), 200


@plaid_api.route('/link-token', methods=['POST'])
@token_required()
def link_token():
    """Create the short-lived token that opens Plaid Link in the browser."""
    if not is_configured():
        return jsonify({"configured": False, "linkToken": None,
                        "error": "Card linking isn't set up yet."}), 200
    user = _user()
    data, error = create_link_token(getattr(user, "uuid", None) or "anonymous")
    if error:
        return jsonify({"configured": True, "linkToken": None, "error": error}), 200
    return jsonify({"configured": True, "linkToken": data.get("link_token")}), 200


def _store_item(user, access_token, item_id, institution):
    """Save a linked account against this user (or update it if re-linked)."""
    item = PlaidItem.query.filter_by(_item_id=item_id).first()
    if item is not None and item._user_id != user.uuid:
        return None, "This account is already connected to a different Flyby account."
    if item is None:
        item = PlaidItem(_user_id=user.uuid, _item_id=item_id, _access_token=access_token,
                         _institution=(institution or "")[:255] or None)
        db.session.add(item)
    else:
        item._access_token = access_token
        if institution:
            item._institution = institution[:255]
    db.session.commit()
    return item, None


@plaid_api.route('/exchange', methods=['POST'])
@token_required()
def exchange():
    """
    Finish linking: swap Link's one-time public token for an access token, keep
    it on the server, and pull the account's charges straight away.
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

    user = _user()
    item, problem = _store_item(user, data.get("access_token"), data.get("item_id"),
                                payload.get("institutionName"))
    if problem:
        return jsonify({"configured": True, "error": problem}), 409
    AuditLog.record(user.uuid, "card_linked", True, institution=item._institution)
    pull_item(item)  # best effort; /sync retries anything that didn't land
    return jsonify({"configured": True, "connected": True, "card": item.read()}), 200


@plaid_api.route('/sync', methods=['POST'])
@token_required()
def sync():
    """
    Bring every linked card up to date, then return the charges not yet filed
    as expenses — already matched to the trip that was running when each one
    happened — plus any filed charges the bank has since withdrawn.

    The app files them, then confirms with ``/ack``. Until it does, the same
    charges are offered again, so a failed save can't lose them.
    """
    if not is_configured():
        return jsonify({"configured": False, "expenses": [], "removed": [], "cards": []}), 200

    user = _user()
    errors = {}
    for item in PlaidItem.for_user(user.uuid):
        error = pull_item(item)
        if error:
            errors[item.id] = error

    pending = (PlaidTransaction.query
               .filter_by(_user_id=user.uuid, _delivered=False)
               .order_by(PlaidTransaction._created_at).all())
    # Only charges made during a trip become expenses; the rest wait in case a
    # trip covering them is added later.
    expenses = offerable(pending, _trip_windows(user.uuid))
    db.session.commit()
    withdrawn = PlaidTransaction.query.filter_by(_user_id=user.uuid, _delivered=True, _removed=True).all()
    return jsonify({
        "configured": True,
        "expenses": expenses,
        "removed": [row.id for row in withdrawn],
        "cards": _cards(user.uuid),
        "errors": errors,
    }), 200


@plaid_api.route('/ack', methods=['POST'])
@token_required()
def ack():
    """The app filed these charges (or removed these withdrawn ones)."""
    payload = request.get_json(silent=True) or {}
    user = _user()
    filed = [str(i) for i in (payload.get("filed") or [])][:1000]
    cleared = [str(i) for i in (payload.get("removed") or [])][:1000]
    if filed:
        (PlaidTransaction.query
         .filter(PlaidTransaction._user_id == user.uuid, PlaidTransaction.id.in_(filed))
         .update({"_delivered": True}, synchronize_session=False))
    if cleared:
        (PlaidTransaction.query
         .filter(PlaidTransaction._user_id == user.uuid, PlaidTransaction.id.in_(cleared))
         .delete(synchronize_session=False))
    db.session.commit()
    return jsonify({"ok": True}), 200


@plaid_api.route('/disconnect', methods=['POST'])
@token_required()
def disconnect():
    """Unlink a card: revoke Flyby's access at Plaid and forget the token."""
    payload = request.get_json(silent=True) or {}
    user = _user()
    item = PlaidItem.query.filter_by(id=payload.get("id") or "", _user_id=user.uuid).first()
    if item is None:
        return jsonify({"error": "That card isn't connected."}), 404
    remove_item(item._access_token)  # best effort: forget it here either way
    # Charges not yet filed go with it; filed ones stay as expenses.
    PlaidTransaction.query.filter_by(_item_id=item._item_id, _delivered=False).delete()
    db.session.delete(item)
    db.session.commit()
    AuditLog.record(user.uuid, "card_unlinked", True)
    return jsonify({"ok": True, "cards": _cards(user.uuid)}), 200


@plaid_api.route('/webhook', methods=['POST'])
def webhook():
    """
    Plaid announces new activity for a linked account.

    The announcement is used only as a cue to pull — nothing in it is trusted
    as data; charges are always fetched from Plaid with the stored token. So a
    forged call can at most trigger an extra pull, and pulls for the same
    account are spaced at least a minute apart.
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("webhook_type") != "TRANSACTIONS":
        return jsonify({"ok": True}), 200
    item = PlaidItem.query.filter_by(_item_id=payload.get("item_id") or "").first()
    if item is None or not is_configured():
        return jsonify({"ok": True}), 200
    if item._last_synced_at and datetime.utcnow() - item._last_synced_at < timedelta(minutes=1):
        return jsonify({"ok": True, "skipped": "recent"}), 200
    pull_item(item)
    return jsonify({"ok": True}), 200
