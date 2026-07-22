"""
Flyby AI — authentication API (``/auth/v1``).

This is the login system the React frontend talks to. It issues the access +
refresh token pair the frontend's auth client stores, and it is the only place
that turns a password into a session.

Endpoints:
    POST /auth/v1/signup                       create an account and sign in
    POST /auth/v1/token?grant_type=password    sign in with email + password
    POST /auth/v1/token?grant_type=refresh_token   exchange a refresh token
    POST /auth/v1/logout                       revoke the current session
    GET  /auth/v1/user                         the signed-in user
    PUT  /auth/v1/user                         change email / password / metadata
    POST /auth/v1/recover                      start a password reset
    POST /auth/v1/verify                       finish a password reset

Sign-up also provisions everything an account needs to be usable immediately:
its profile, RBAC role, travel preferences, and workspace membership.
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, request

from __init__ import db
from api.jwt_authorize import (ACCESS, REFRESH, decode_token, encode_token,
                               token_required, token_from_request)
from model.base import new_uuid, utcnow
from model.company import Company
from model.preferences import TravelPreference
from model.roles import UserRole
from model.security import ActiveSession, AuditLog
from model.user import Profile, User

auth_api = Blueprint('auth_api', __name__, url_prefix='/auth/v1')

# Consumer email providers never map to a shared company workspace.
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com", "zoho.com", "yandex.com", "msn.com",
}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(message, status=400, code=None):
    """Return the error envelope the frontend's auth client understands."""
    return jsonify({
        "error": code or ("invalid_request" if status < 500 else "server_error"),
        "error_description": message,
        "message": message,
        "msg": message,
    }), status


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _session_payload(user):
    """
    Build the session object the frontend stores after a successful sign-in.

    Shape matches what the auth client was written against: an access token, a
    refresh token, an absolute expiry, and the user record.
    """
    access_ttl = current_app.config["JWT_ACCESS_TTL_SECONDS"]
    access_token = encode_token(user, kind=ACCESS)
    refresh_token = encode_token(user, kind=REFRESH)
    expires_at = int((datetime.utcnow() + timedelta(seconds=access_ttl)).timestamp())

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": access_ttl,
        "expires_at": expires_at,
        "user": user.read_auth_user(),
    }


def _track_session(user, access_token):
    """
    Record the sign-in as an active session.

    Only a hash of the token is kept, so the Security page can list and revoke
    devices without storing anything replayable.
    """
    try:
        agent = request.headers.get("User-Agent", "")
        ActiveSession(
            id=new_uuid(),
            _user_id=user.uuid,
            _session_token_hash=hashlib.sha256(access_token.encode()).hexdigest(),
            _device_info=agent[:500],
            _browser=_detect_browser(agent),
            _ip_address=_client_ip(),
        ).create()
    except Exception:
        db.session.rollback()


def _detect_browser(agent):
    if "Edg" in agent:
        return "Edge"
    if "Chrome" in agent:
        return "Chrome"
    if "Firefox" in agent:
        return "Firefox"
    if "Safari" in agent:
        return "Safari"
    return "Unknown"


def _provision_account(user, account_mode=None):
    """
    Give a new account everything it needs to work on first load.

    Creates the profile, RBAC role and default travel preferences, and attaches
    a work-email user to the workspace for their domain. Runs on every sign-in
    too, so an account created before a feature existed is repaired on next use.
    """
    profile = user.ensure_profile()
    UserRole.sync_from_user(user)
    TravelPreference.for_user(user.uuid)

    domain = user.email.split("@")[-1].lower() if "@" in user.email else ""
    is_personal = (not domain) or domain in PERSONAL_EMAIL_DOMAINS or account_mode == "personal"

    if profile and not profile.company_id and not is_personal:
        company = Company.for_domain(domain, name=domain.split(".")[0].title())
        if company:
            profile.company_id = company.id
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
    return profile


def _validate_credentials(email, password, require_strong=False):
    """Return an error message for bad credentials, or None when they're fine."""
    if not email or not EMAIL_PATTERN.match(email):
        return "Please enter a valid email address."
    if not password:
        return "Password is required."
    if require_strong and len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


# ---------------------------------------------------------------------------
# Sign-up
# ---------------------------------------------------------------------------

@auth_api.route('/signup', methods=['POST'])
def signup():
    """
    Create an account and return a signed-in session.

    Duplicate emails are rejected explicitly rather than silently signing the
    existing user in, so a typo in an email can never hand someone another
    person's account.
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    metadata = data.get("data") or {}
    full_name = (metadata.get("full_name") or "").strip() or email.split("@")[0]

    invalid = _validate_credentials(email, password, require_strong=True)
    if invalid:
        return _error(invalid, 400, "validation_failed")

    if User.by_email(email):
        return _error("An account with this email already exists.", 409, "user_already_exists")

    user = User(name=full_name, uid=email, password=password, role="User", email=email)
    created = user.create()
    if created is None:
        return _error("Could not create the account. Please try again.", 500, "signup_failed")

    _provision_account(created, account_mode=metadata.get("account_mode"))

    session = _session_payload(created)
    _track_session(created, session["access_token"])
    created.touch_login()
    AuditLog.record(created.uuid, "signup", True, ip_address=_client_ip(),
                    user_agent=request.headers.get("User-Agent", "")[:500])

    return jsonify(session), 200


# ---------------------------------------------------------------------------
# Sign-in / refresh
# ---------------------------------------------------------------------------

@auth_api.route('/token', methods=['POST'])
def token():
    """
    Exchange credentials for a session.

    ``?grant_type=password`` signs in with email + password;
    ``?grant_type=refresh_token`` renews an expiring session.
    """
    grant_type = request.args.get("grant_type") or (request.get_json(silent=True) or {}).get("grant_type") or "password"

    if grant_type == "refresh_token":
        return _refresh_grant()
    if grant_type == "password":
        return _password_grant()
    return _error(f"Unsupported grant_type '{grant_type}'.", 400, "unsupported_grant_type")


def _password_grant():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    user = User.by_email(email)
    # One message for both "no such user" and "wrong password" so the response
    # can't be used to discover which emails have accounts.
    if not user or not user.is_password(password):
        AuditLog.record(user.uuid if user else "unknown", "login_failure", False,
                        ip_address=_client_ip(), email=email)
        return _error("Invalid login credentials.", 400, "invalid_grant")

    _provision_account(user)
    session = _session_payload(user)
    _track_session(user, session["access_token"])
    user.touch_login()
    AuditLog.record(user.uuid, "login_success", True, ip_address=_client_ip(),
                    user_agent=request.headers.get("User-Agent", "")[:500])

    return jsonify(session), 200


def _refresh_grant():
    data = request.get_json(silent=True) or {}
    refresh_token = data.get("refresh_token") or ""

    payload, error = decode_token(refresh_token, expected=REFRESH)
    if error or not payload:
        return _error("Invalid refresh token.", 401, "invalid_grant")

    user = User.by_uuid(payload.get("uuid"))
    if not user:
        return _error("Invalid refresh token.", 401, "invalid_grant")

    return jsonify(_session_payload(user)), 200


@auth_api.route('/logout', methods=['POST'])
def logout():
    """
    End the current session.

    Always reports success: a client that is signing out should end up signed
    out locally even if its token was already expired or revoked.
    """
    token_value = token_from_request()
    payload, _ = decode_token(token_value, expected=ACCESS)
    if payload:
        try:
            token_hash = hashlib.sha256(token_value.encode()).hexdigest()
            session_row = ActiveSession.query.filter_by(
                _user_id=payload.get("uuid"), _session_token_hash=token_hash,
            ).first()
            if session_row:
                session_row._revoked = True
                session_row._revoked_at = utcnow()
                db.session.commit()
        except Exception:
            db.session.rollback()
        AuditLog.record(payload.get("uuid"), "logout", True, ip_address=_client_ip())

    return jsonify({}), 204


# ---------------------------------------------------------------------------
# The signed-in user
# ---------------------------------------------------------------------------

@auth_api.route('/user', methods=['GET'])
@token_required()
def get_user():
    """Return the signed-in user."""
    return jsonify(g.current_user.read_auth_user()), 200


@auth_api.route('/user', methods=['PUT'])
@token_required()
def update_user():
    """
    Update the signed-in user's email, password or metadata.

    A password change requires the current password unless the request carries
    a valid recovery token, so a stolen session alone cannot lock the owner out.
    """
    user = g.current_user
    data = request.get_json(silent=True) or {}

    new_password = data.get("password")
    if new_password:
        if len(new_password) < MIN_PASSWORD_LENGTH:
            return _error(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                400, "weak_password",
            )
        recovery = data.get("recovery_token")
        if recovery:
            payload, error = decode_token(recovery, expected="recovery")
            if error or payload.get("uuid") != user.uuid:
                return _error("This reset link is invalid or has expired.", 401, "invalid_token")
        else:
            current_password = data.get("current_password") or data.get("currentPassword")
            if not current_password or not user.is_password(current_password):
                return _error("Your current password is incorrect.", 400, "invalid_credentials")
        user.set_password(new_password)
        AuditLog.record(user.uuid, "password_changed", True, ip_address=_client_ip())

    new_email = (data.get("email") or "").strip().lower()
    if new_email and new_email != user.email:
        if not EMAIL_PATTERN.match(new_email):
            return _error("Please enter a valid email address.", 400, "validation_failed")
        if User.by_email(new_email):
            return _error("That email is already in use.", 409, "email_exists")
        user.set_uid(new_email)
        if user.profile:
            user.profile.email = new_email

    metadata = data.get("data") or {}
    if metadata.get("full_name"):
        user.name = metadata["full_name"]
        if user.profile:
            user.profile.full_name = metadata["full_name"]

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return _error("Could not save your changes.", 500, "update_failed")

    return jsonify(user.read_auth_user()), 200


# ---------------------------------------------------------------------------
# Password recovery
# ---------------------------------------------------------------------------

@auth_api.route('/recover', methods=['POST'])
def recover():
    """
    Start a password reset.

    Always returns success, whether or not the email has an account, so the
    response cannot be used to enumerate users. In local development, where no
    mail provider is configured, the reset link is logged to the console and
    returned so the flow is testable end to end.
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    redirect_to = data.get("redirect_to") or data.get("redirectTo") or ""

    response = {"message": "If that email has an account, a reset link is on its way."}

    user = User.by_email(email)
    if user:
        reset_token = encode_token(user, kind="recovery", ttl_seconds=60 * 30)
        base = redirect_to or "http://localhost:8080/reset-password"
        link = f"{base}{'&' if '?' in base else '?'}token={reset_token}&type=recovery"
        AuditLog.record(user.uuid, "password_reset_requested", True, ip_address=_client_ip())

        current_app.logger.info("Password reset link for %s: %s", email, link)
        if not current_app.config.get("DB_ENDPOINT"):
            # Development convenience only — never returned once a real
            # deployment (external database) is configured.
            response["reset_link"] = link
            response["token"] = reset_token

    return jsonify(response), 200


@auth_api.route('/verify', methods=['POST'])
def verify_recovery():
    """Exchange a valid recovery token for a signed-in session."""
    data = request.get_json(silent=True) or {}
    recovery_token = data.get("token") or ""

    payload, error = decode_token(recovery_token, expected="recovery")
    if error or not payload:
        return _error("This reset link is invalid or has expired.", 401, "invalid_token")

    user = User.by_uuid(payload.get("uuid"))
    if not user:
        return _error("This reset link is invalid or has expired.", 401, "invalid_token")

    session = _session_payload(user)
    # Carried through so the reset screen can set a new password without also
    # having to know the old one.
    session["recovery_token"] = recovery_token
    return jsonify(session), 200


@auth_api.route('/health', methods=['GET'])
def health():
    """Liveness probe for the auth service."""
    return jsonify({"status": "ok", "service": "flyby-auth"}), 200
