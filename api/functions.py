"""
Flyby AI — service endpoints (``/functions/v1``).

The frontend calls these by name for work that doesn't fit a table query: the
two-factor flow, travel inventory search, and voice transcription.

Endpoints:
    POST /functions/v1/twofa-check-required   is 2FA needed for this email?
    POST /functions/v1/twofa-status           the caller's 2FA state
    POST /functions/v1/twofa-send-sms         send a verification code
    POST /functions/v1/twofa-verify-sms       check a verification code
    POST /functions/v1/twofa-disable          turn 2FA off
    GET  /functions/v1/search-travel          flight / hotel / ground search
    POST /functions/v1/transcribe             voice note -> text
    POST /functions/v1/audit-login            record a sign-in from the client

``twofa-check-required`` is the only unauthenticated endpoint here, and it
answers about an email the caller already typed. It reveals nothing else: an
address with no account gets the same "no 2FA" answer as one without 2FA, so it
can't be used to discover which emails are registered.
"""

import re

from flask import Blueprint, current_app, g, jsonify, request

from __init__ import db
from api.jwt_authorize import token_required
from api.travel_search import search as run_travel_search
from api.trip_reasoning import reason_about_trip
from model.base import new_uuid, utcnow
from model.mfa import MfaCredential, PendingVerification
from model.security import AuditLog, TwoFactorAuditLog, mask_phone
from model.user import Profile, User

functions_api = Blueprint('functions_api', __name__, url_prefix='/functions/v1')

PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")
MAX_QUERY_LENGTH = 200
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean(value, limit=200):
    """Trim and length-cap a client-supplied string."""
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _agent():
    return request.headers.get("User-Agent", "")[:500]


def _normalize_phone(raw):
    """Reduce a phone number to digits with an optional leading '+'."""
    if not raw:
        return ""
    cleaned = re.sub(r"[^\d+]", "", str(raw))
    if cleaned.startswith("+"):
        return "+" + re.sub(r"\D", "", cleaned[1:])
    return re.sub(r"\D", "", cleaned)


def _deliver_sms(phone, code):
    """
    Send the verification code.

    With Twilio configured the code is sent by SMS. Without it — the normal
    local setup — the code is logged to the backend console so the flow is
    fully testable, and it is never returned in the HTTP response.
    """
    sid = current_app.config.get("TWILIO_ACCOUNT_SID")
    token = current_app.config.get("TWILIO_AUTH_TOKEN")
    sender = current_app.config.get("TWILIO_FROM_NUMBER")

    if sid and token and sender:
        try:
            import requests
            response = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, token),
                data={
                    "From": sender,
                    "To": phone,
                    "Body": f"Your Flyby AI verification code is {code}. It expires in 10 minutes.",
                },
                timeout=10,
            )
            if response.status_code < 300:
                return True, None
            return False, f"SMS provider returned {response.status_code}"
        except Exception as error:  # noqa: BLE001
            return False, str(error)

    current_app.logger.warning(
        "[2FA] No SMS provider configured — verification code for %s is %s",
        mask_phone(phone), code,
    )
    return True, None


# ---------------------------------------------------------------------------
# Two-factor authentication
# ---------------------------------------------------------------------------

@functions_api.route('/twofa-check-required', methods=['POST', 'OPTIONS'])
def twofa_check_required():
    """
    Report whether an email's account requires a second factor at sign-in.

    Unknown emails get the same shape as accounts without 2FA, so this can't be
    used to enumerate users.
    """
    if request.method == 'OPTIONS':
        return ('', 204)

    data = request.get_json(silent=True) or {}
    email = _clean(data.get("email"), 255).lower()

    result = {"requires2FA": False, "maskedPhone": None}

    user = User.by_email(email)
    if user and user.profile and user.profile._two_factor_enabled:
        result["requires2FA"] = True
        result["maskedPhone"] = mask_phone(user.profile._two_factor_phone)

    return jsonify(result), 200


@functions_api.route('/twofa-status', methods=['POST'])
@token_required()
def twofa_status():
    """Return the caller's two-factor state."""
    profile = g.current_user.profile
    if profile is None:
        return jsonify({"enabled": False, "maskedPhone": None, "verifiedAt": None}), 200

    verified = profile._two_factor_verified_at
    return jsonify({
        "enabled": bool(profile._two_factor_enabled),
        "maskedPhone": mask_phone(profile._two_factor_phone),
        "verifiedAt": verified.isoformat() + "Z" if verified else None,
    }), 200


@functions_api.route('/twofa-send-sms', methods=['POST'])
@token_required()
def twofa_send_sms():
    """
    Send a verification code.

    During setup the target number comes in the request; during sign-in the
    body is empty and the enrolled number is used instead.
    """
    user = g.current_user
    profile = user.profile
    data = request.get_json(silent=True) or {}

    phone = _normalize_phone(data.get("phone")) or _normalize_phone(
        profile._two_factor_phone if profile else ""
    )

    if not phone:
        return jsonify({"error": "No phone number on file. Add one to enable two-step verification."}), 400
    if not PHONE_PATTERN.match(phone):
        TwoFactorAuditLog.record(user.uuid, "send_code", False, phone, "invalid_phone", _client_ip(), _agent())
        return jsonify({"error": "That phone number doesn't look right."}), 400

    row, code = PendingVerification.issue(user.uuid, phone)
    if row is None:
        return jsonify({"error": "Could not start verification. Please try again."}), 500

    sent, error = _deliver_sms(phone, code)
    TwoFactorAuditLog.record(user.uuid, "send_code", sent, phone, error, _client_ip(), _agent())
    if not sent:
        return jsonify({"error": "We couldn't send the code. Please try again."}), 502

    return jsonify({"success": True, "maskedPhone": mask_phone(phone)}), 200


@functions_api.route('/twofa-verify-sms', methods=['POST'])
@token_required()
def twofa_verify_sms():
    """
    Check a verification code.

    With ``enableAfterVerify`` the number is enrolled and 2FA switched on; at
    sign-in the flag is false and the code is simply confirmed. Either way the
    challenge is consumed, so a code can never be replayed.
    """
    user = g.current_user
    data = request.get_json(silent=True) or {}
    code = _clean(data.get("code"), 10)
    enable_after = bool(data.get("enableAfterVerify"))

    if not code or not code.isdigit() or len(code) != 6:
        return jsonify({"error": "Enter the 6-digit code we sent you."}), 400

    pending = PendingVerification.query.filter_by(_user_id=user.uuid).first()
    if pending is None:
        return jsonify({"error": "That code has expired. Request a new one."}), 400

    outcome = pending.verify(code)

    if outcome != "ok":
        messages = {
            "expired": "That code has expired. Request a new one.",
            "too_many_attempts": "Too many incorrect attempts. Request a new code.",
            "invalid": "That code isn't right. Please try again.",
        }
        TwoFactorAuditLog.record(user.uuid, "verify_code", False, pending._phone_number,
                                 outcome, _client_ip(), _agent())
        if outcome in ("expired", "too_many_attempts"):
            pending.delete()
        return jsonify({"error": messages.get(outcome, "Verification failed.")}), 400

    phone = pending._phone_number
    pending.delete()

    if enable_after:
        profile = user.ensure_profile()
        profile._two_factor_enabled = True
        profile._two_factor_phone = phone
        profile._two_factor_verified_at = utcnow()

        existing = MfaCredential.active_for(user.uuid)
        if existing is None:
            MfaCredential(
                id=new_uuid(), _user_id=user.uuid, _type="sms",
                _device_name="SMS", _phone_number=phone, _is_active=True,
            ).create()
        else:
            existing._phone_number = phone
            existing._is_active = True

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"error": "Could not enable two-step verification."}), 500

        AuditLog.record(user.uuid, "2fa_enabled", True, ip_address=_client_ip())

    TwoFactorAuditLog.record(user.uuid, "verify_code", True, phone, None, _client_ip(), _agent())
    return jsonify({"success": True, "maskedPhone": mask_phone(phone)}), 200


@functions_api.route('/twofa-disable', methods=['POST'])
@token_required()
def twofa_disable():
    """Turn off two-factor authentication for the caller."""
    user = g.current_user
    profile = user.ensure_profile()

    profile._two_factor_enabled = False
    profile._two_factor_phone = None
    profile._two_factor_verified_at = None

    for credential in MfaCredential.query.filter_by(_user_id=user.uuid).all():
        credential._is_active = False
    PendingVerification.query.filter_by(_user_id=user.uuid).delete()

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not disable two-step verification."}), 500

    AuditLog.record(user.uuid, "2fa_disabled", True, ip_address=_client_ip())
    TwoFactorAuditLog.record(user.uuid, "disable", True, None, None, _client_ip(), _agent())
    return jsonify({"success": True}), 200


# ---------------------------------------------------------------------------
# Travel search
# ---------------------------------------------------------------------------

@functions_api.route('/search-travel', methods=['GET', 'POST'])
def search_travel():
    """
    Search flights, hotels and ground transport.

    Accepts parameters from the query string (how the search box calls it) or a
    JSON body, and returns ``{flights, hotels, ground}``.
    """
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
    else:
        body = {}

    def param(name, limit=MAX_QUERY_LENGTH):
        return _clean(request.args.get(name) or body.get(name), limit)

    search_type = param("type", 20) or "all"
    if search_type not in ("flights", "hotels", "ground", "all"):
        return jsonify({"error": "Invalid search type. Use: flights, hotels, ground, or all."}), 400

    check_in, check_out, date = param("checkIn", 10), param("checkOut", 10), param("date", 10)
    for label, value in (("check-in", check_in), ("check-out", check_out), ("date", date)):
        if value and not DATE_PATTERN.match(value):
            return jsonify({"error": f"Invalid {label} date format. Use YYYY-MM-DD."}), 400

    results = run_travel_search(
        search_type=search_type,
        query=param("q"),
        city=param("city", 100),
        origin=param("origin", 10),
        dest=param("dest", 10),
        check_in=check_in,
        check_out=check_out,
        date=date,
    )
    return jsonify(results), 200


# ---------------------------------------------------------------------------
# Trip reasoning
# ---------------------------------------------------------------------------

@functions_api.route('/trip-reasoning', methods=['POST'])
@token_required()
def trip_reasoning():
    """
    Score a planned trip and explain the plan.

    Returns the four-panel ``TripAIReasoning`` object the app renders. Policy
    compliance is judged against the caller's own company travel policy, looked
    up here so the client can't spoof a more lenient one. Uses Gemini when a key
    is configured and falls back to a deterministic local analysis otherwise, so
    a result always comes back.
    """
    trip = request.get_json(silent=True) or {}
    if not isinstance(trip, dict):
        return jsonify({"error": "Expected a trip object."}), 400

    # Ground policy compliance in the caller's real policy, not anything they send.
    policy = None
    profile = g.current_user.profile
    company_id = profile.company_id if profile else None
    if company_id:
        from model.company import TravelPolicy
        row = TravelPolicy.for_company(company_id)
        if row:
            policy = row.read()

    reasoning, engine = reason_about_trip(trip, policy)
    return jsonify({"reasoning": reasoning, "engine": engine}), 200


# ---------------------------------------------------------------------------
# Voice transcription
# ---------------------------------------------------------------------------

@functions_api.route('/transcribe', methods=['POST'])
def transcribe():
    """
    Transcribe a recorded voice note.

    Uses OpenAI Whisper when an API key is configured. Without one it returns a
    clear, actionable message instead of silently producing nothing, so the UI
    can tell the user why dictation is unavailable.
    """
    audio = request.files.get("audio")
    if audio is None:
        return jsonify({"error": "No audio was received."}), 400

    api_key = current_app.config.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({
            "text": "",
            "transcript": "",
            "error": "Voice transcription needs an OPENAI_API_KEY in the backend .env file.",
        }), 503

    try:
        import requests
        response = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (audio.filename or "recording.webm", audio.stream, audio.mimetype)},
            data={"model": "whisper-1"},
            timeout=60,
        )
        if response.status_code >= 300:
            current_app.logger.error("Transcription failed: %s %s", response.status_code, response.text[:300])
            return jsonify({"error": "Transcription failed. Please try again."}), 502
        # Returned under both keys: the recorder reads `transcript`, while
        # `text` matches the transcription provider's own field name.
        text = response.json().get("text", "")
        return jsonify({"text": text, "transcript": text}), 200
    except Exception as error:  # noqa: BLE001
        current_app.logger.error("Transcription error: %s", error)
        return jsonify({"error": "Transcription failed. Please try again."}), 502


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@functions_api.route('/audit-login', methods=['POST'])
@token_required()
def audit_login():
    """Record a client-observed sign-in event."""
    data = request.get_json(silent=True) or {}
    AuditLog.record(
        g.user_id,
        _clean(data.get("action"), 100) or "login",
        bool(data.get("success", True)),
        ip_address=_client_ip(),
        user_agent=_agent(),
    )
    return jsonify({"success": True}), 200
