"""
Flyby AI — Google Calendar endpoints (``/api/google``).

    GET  /api/google/status     whether calendar sync is available yet
    POST /api/google/auth-url   start the Google consent flow
    POST /api/google/exchange   finish connecting (code -> tokens)
    POST /api/google/events     list upcoming events
    POST /api/google/event      put a confirmed trip on the calendar

Until GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are set, every route reports
``configured: false`` rather than failing, so the UI can show "Coming soon"
honestly instead of pretending to connect.
"""

from flask import Blueprint, g, jsonify, request

from api.jwt_authorize import token_required
from api.google_calendar import (build_auth_url, create_event, exchange_code,
                                 get_account_email, is_configured, list_events,
                                 refresh_access_token)

google_api = Blueprint('google_api', __name__, url_prefix='/api/google')


@google_api.route('/status', methods=['GET'])
@token_required()
def status():
    """Whether the traveler can connect Google Calendar yet."""
    return jsonify({"configured": is_configured(), "provider": "google"}), 200


@google_api.route('/auth-url', methods=['POST'])
@token_required()
def auth_url():
    """The consent URL to send the traveler to."""
    if not is_configured():
        return jsonify({"configured": False, "authUrl": None,
                        "error": "Google Calendar sync isn't set up yet."}), 200
    user = getattr(g, "current_user", None)
    state = str(getattr(user, "uuid", None) or getattr(user, "id", "anonymous"))
    return jsonify({"configured": True, "authUrl": build_auth_url(state)}), 200


@google_api.route('/exchange', methods=['POST'])
@token_required()
def exchange():
    """
    Finish connecting: turn the one-time code into tokens.

    Returns the tokens for the caller to persist against the traveler, plus the
    connected account's email for display.
    """
    if not is_configured():
        return jsonify({"configured": False, "error": "Google Calendar sync isn't set up yet."}), 200

    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip()
    if not code:
        return jsonify({"error": "Missing authorization code."}), 400

    tokens, error = exchange_code(code)
    if error:
        return jsonify({"configured": True, "error": error}), 200

    access_token = tokens.get("access_token")
    return jsonify({
        "configured": True,
        "accessToken": access_token,
        "refreshToken": tokens.get("refresh_token"),
        "expiresIn": tokens.get("expires_in"),
        "email": get_account_email(access_token) if access_token else None,
    }), 200


@google_api.route('/events', methods=['POST'])
@token_required()
def events():
    """
    Upcoming calendar events.

    Accepts a refresh token so an expired access token is renewed transparently
    rather than surfacing as a broken connection.
    """
    if not is_configured():
        return jsonify({"configured": False, "events": []}), 200

    payload = request.get_json(silent=True) or {}
    access_token = (payload.get("accessToken") or "").strip()
    refresh_token = (payload.get("refreshToken") or "").strip()
    if not access_token and not refresh_token:
        return jsonify({"error": "Missing token."}), 400

    rows, error = (None, "token_expired") if not access_token else list_events(
        access_token, payload.get("timeMin"))

    if error == "token_expired" and refresh_token:
        refreshed, refresh_error = refresh_access_token(refresh_token)
        if not refresh_error and refreshed.get("access_token"):
            access_token = refreshed["access_token"]
            rows, error = list_events(access_token, payload.get("timeMin"))
            if not error:
                return jsonify({"configured": True, "events": rows,
                                "accessToken": access_token}), 200

    if error:
        return jsonify({"configured": True, "events": [], "error": error}), 200
    return jsonify({"configured": True, "events": rows}), 200


@google_api.route('/event', methods=['POST'])
@token_required()
def add_event():
    """Add a confirmed trip to the traveler's calendar."""
    if not is_configured():
        return jsonify({"configured": False, "event": None}), 200

    payload = request.get_json(silent=True) or {}
    access_token = (payload.get("accessToken") or "").strip()
    title = (payload.get("title") or "").strip()
    start = (payload.get("startDate") or "").strip()
    end = (payload.get("endDate") or "").strip()
    if not access_token or not title or not start or not end:
        return jsonify({"error": "Missing token, title or dates."}), 400

    created, error = create_event(access_token, title, start, end,
                                  payload.get("location") or "",
                                  payload.get("description") or "")
    if error:
        return jsonify({"configured": True, "event": None, "error": error}), 200
    return jsonify({"configured": True, "event": created}), 200
