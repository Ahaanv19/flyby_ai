"""
Flyby AI — request authentication.

Every API request that touches user data passes through ``@token_required()``.
It resolves the caller from a JWT (``Authorization: Bearer …``, or the auth
cookie for the server-rendered pages) and puts the ``User`` on Flask's ``g``.

Tokens are minted here too, so issuing and verifying stay in one place and can
never drift apart.
"""

from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

from model.user import User

# Token kinds. Access tokens authorize requests; refresh tokens may only be
# exchanged for a new pair, never used to authorize a request directly.
ACCESS = "access"
REFRESH = "refresh"


def _now():
    return datetime.utcnow()


def encode_token(user, kind=ACCESS, ttl_seconds=None):
    """
    Mint a signed token for ``user``.

    The payload carries the public UUID (what the frontend calls ``user.id``),
    the login handle and the role, so most requests need no database lookup
    beyond loading the user itself.
    """
    if ttl_seconds is None:
        config_key = "JWT_ACCESS_TTL_SECONDS" if kind == ACCESS else "JWT_REFRESH_TTL_SECONDS"
        ttl_seconds = current_app.config[config_key]

    issued = _now()
    payload = {
        "sub": user.uuid,
        "uuid": user.uuid,
        "uid": user.uid,
        "email": user.email,
        "role": user.role,
        "type": kind,
        "iat": issued,
        "exp": issued + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def decode_token(token, expected=ACCESS):
    """
    Verify a token and return ``(payload, error)``.

    ``error`` is a short machine-readable reason (``expired``, ``invalid``,
    ``wrong_type``) so callers can respond appropriately without inspecting
    exception types.
    """
    if not token:
        return None, "missing"
    try:
        payload = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None, "expired"
    except jwt.InvalidTokenError:
        return None, "invalid"

    if expected and payload.get("type") != expected:
        return None, "wrong_type"
    return payload, None


def token_from_request():
    """Pull the bearer token from the Authorization header, then the cookie."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        candidate = header[7:].strip()
        # The frontend sends the public key as a bearer token on unauthenticated
        # calls; treat that as "no user" rather than a malformed token.
        if candidate and candidate != "public":
            return candidate
    return request.cookies.get(current_app.config["JWT_TOKEN_NAME"])


def current_user_from_request():
    """
    Resolve the signed-in user, or None.

    Used by endpoints that serve both signed-in and anonymous callers; guarded
    endpoints should use ``@token_required()`` instead.
    """
    payload, error = decode_token(token_from_request(), expected=ACCESS)
    if error or not payload:
        return None
    return User.by_uuid(payload.get("uuid"))


def token_required(roles=None):
    """
    Guard an endpoint so only authenticated users (optionally in ``roles``) reach it.

    On success ``g.current_user`` is the ``User`` and ``g.user_id`` its UUID.

    Responses:
        401 — token missing, expired, or invalid
        403 — authenticated but lacking the required role
    """
    def decorator(func_to_guard):
        @wraps(func_to_guard)
        def decorated(*args, **kwargs):
            payload, error = decode_token(token_from_request(), expected=ACCESS)
            if error == "expired":
                return jsonify({
                    "message": "Token has expired",
                    "error": "token_expired",
                }), 401
            if error or not payload:
                return jsonify({
                    "message": "Authentication required",
                    "error": "unauthorized",
                }), 401

            user = User.by_uuid(payload.get("uuid"))
            if not user:
                return jsonify({
                    "message": "User not found",
                    "error": "unauthorized",
                }), 401

            if roles and user.role not in roles:
                return jsonify({
                    "message": "User does not have the required role",
                    "error": "forbidden",
                }), 403

            g.current_user = user
            g.user_id = user.uuid
            return func_to_guard(*args, **kwargs)
        return decorated
    return decorator
