"""
Flyby AI — sign in with Google or Microsoft.

    GET  /auth/v1/authorize?provider=google|microsoft&flow=app|console
         Start signing in. Redirects to the provider's account chooser.
    POST /auth/v1/authorize          (signed in to the app)
         Same, but to connect the provider to the current account.
    POST /auth/v1/token?grant_type=oauth_code
         The app trades the one-time code it was handed for a normal session.
    GET  /auth/v1/identities         (signed in to the app)
         Which providers the account can sign in with.

It reuses the OAuth clients and the redirect addresses already registered for
calendar sync (``/auth/google/callback`` and ``/auth/outlook/callback``), so no
new URLs have to be added in Google Cloud or Azure. Those callbacks tell the two
uses apart by the ``state`` value: sign-in states start with ``login.``.

Security, in order of what can go wrong:

* **Forged or replayed sign-ins.** ``state`` is signed with the app secret and
  expires in ten minutes, and it must match a nonce set in an HttpOnly cookie
  when the sign-in started — so a callback only completes in the browser that
  began it. The ID token must carry the same nonce, the right audience and
  issuer, and be unexpired. It arrives straight from the provider's token
  endpoint over TLS in exchange for the client secret, which is what OpenID
  Connect accepts in place of checking its signature.

* **Taking over someone else's account by email.** Identities are keyed on the
  provider's stable id, not the email. An email is only trusted to match an
  existing Flyby account when the provider vouches for it: Google with
  ``email_verified``; Microsoft by the sign-in name, which Microsoft only allows
  on domains the organisation has proven it owns (the separate ``email`` claim
  can be set to anything by an organisation's admin and is ignored).

* **Accounts with a guessable password.** A blank password falls back to the
  app's default password, so accounts created here get a long random one
  nobody knows, and are recorded as having no password until one is chosen.

* **The session itself.** The app never receives tokens in a URL. It gets a
  one-time code, valid for two minutes, which it trades for the same session a
  password sign-in returns.
"""

import base64
import json
import re
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Blueprint, current_app, g, jsonify, make_response, redirect, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from api.jwt_authorize import token_required
from model.identity import PROVIDER_NAMES, UserIdentity
from model.security import AuditLog
from model.user import User

oauth_login_api = Blueprint("oauth_login_api", __name__, url_prefix="/auth/v1")

STATE_PREFIX = "login."
NONCE_COOKIE = "flyby_signin_nonce"
STATE_MAX_AGE = 600      # seconds to finish at the provider
CODE_MAX_AGE = 120       # seconds for the app to redeem its code
TIMEOUT = 20

# Microsoft's tenant for personal accounts (outlook.com, hotmail.com, live.com).
CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
EMAIL_PATTERN = re.compile(r"^[^@\s#]+@[^@\s#]+\.[^@\s#]+$")

# Codes already redeemed, so each works once. In memory: the backend runs a
# single worker (see the Dockerfile); with several, use a shared store.
_redeemed_codes = {}


class SignInError(Exception):
    """A sign-in that can't complete, with a message fit to show the person."""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def _provider(name):
    config = current_app.config
    if name == "google":
        if not (config.get("GOOGLE_CLIENT_ID") and config.get("GOOGLE_CLIENT_SECRET")):
            return None
        return {
            "name": "google",
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scope": "openid email profile",
            "client_id": config["GOOGLE_CLIENT_ID"],
            "client_secret": config["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": config.get("GOOGLE_REDIRECT_URI"),
        }
    if name == "microsoft":
        if not (config.get("MS_CLIENT_ID") and config.get("MS_CLIENT_SECRET")):
            return None
        authority = config.get("MS_AUTHORITY") or "common"
        base = f"https://login.microsoftonline.com/{authority}/oauth2/v2.0"
        return {
            "name": "microsoft",
            "authorize_url": f"{base}/authorize",
            "token_url": f"{base}/token",
            "scope": "openid email profile User.Read",
            "client_id": config["MS_CLIENT_ID"],
            "client_secret": config["MS_CLIENT_SECRET"],
            "redirect_uri": config.get("MS_REDIRECT_URI"),
        }
    return None


def available_providers():
    """Which sign-in buttons can work right now."""
    return [name for name in ("google", "microsoft") if _provider(name)]


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def _app_url():
    return (current_app.config.get("APP_URL") or "").rstrip("/")


def _secure_request():
    return request.scheme == "https" or request.headers.get("X-Forwarded-Proto", "") == "https"


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def _authorize_redirect(provider_name, flow, link_user_uuid=None):
    """Build the redirect to the provider, and the cookie that binds it to this browser."""
    provider = _provider(provider_name)
    if provider is None:
        raise SignInError(f"Signing in with {PROVIDER_NAMES.get(provider_name, provider_name)} isn't set up yet.")

    nonce = secrets.token_urlsafe(24)
    state = STATE_PREFIX + _serializer().dumps(
        {"p": provider_name, "f": flow, "n": nonce, "l": link_user_uuid}, salt="oauth-login-state")
    params = {
        "client_id": provider["client_id"],
        "redirect_uri": provider["redirect_uri"],
        "response_type": "code",
        "scope": provider["scope"],
        "state": state,
        "nonce": nonce,
        # Always show the account chooser, so someone signed in to several
        # Google or Microsoft accounts picks the one they mean.
        "prompt": "select_account",
    }
    return f"{provider['authorize_url']}?{urlencode(params)}", nonce


def _with_nonce_cookie(response, nonce):
    response.set_cookie(
        NONCE_COOKIE, nonce, max_age=STATE_MAX_AGE, httponly=True,
        secure=_secure_request(), samesite="Lax", path="/auth",
    )
    return response


@oauth_login_api.route("/authorize", methods=["GET"])
def authorize():
    """Start signing in. ``flow`` is where to land afterwards: the app or the console."""
    provider_name = (request.args.get("provider") or "").lower()
    flow = "console" if request.args.get("flow") == "console" else "app"
    link_user = None
    if request.args.get("link") and flow == "console":
        # Connecting a provider to the account signed in to the console.
        from flask_login import current_user
        if not current_user.is_authenticated:
            return redirect(url_for("login"))
        link_user = current_user.uuid
    try:
        url, nonce = _authorize_redirect(provider_name, flow, link_user)
    except SignInError as error:
        return _fail(flow, str(error))
    return _with_nonce_cookie(redirect(url), nonce)


@oauth_login_api.route("/authorize", methods=["POST"])
@token_required()
def authorize_link():
    """
    Connect a provider to the account signed in to the app.

    The app can't send its bearer token on a page navigation, so it asks here
    first and is given the provider URL to go to. The nonce cookie is set on
    the redirect hop that follows (``/auth/v1/authorize/go``).
    """
    data = request.get_json(silent=True) or {}
    provider_name = (data.get("provider") or "").lower()
    user = getattr(g, "current_user", None)
    if not user:
        return jsonify({"error": "Sign in first."}), 401
    try:
        url, nonce = _authorize_redirect(provider_name, "app", user.uuid)
    except SignInError as error:
        return jsonify({"error": str(error)}), 400
    ticket = _serializer().dumps({"u": url, "n": nonce}, salt="oauth-login-go")
    return jsonify({"url": f"{request.host_url.rstrip('/')}/auth/v1/authorize/go?t={ticket}"}), 200


@oauth_login_api.route("/authorize/go", methods=["GET"])
def authorize_go():
    """Set the browser-binding cookie, then continue to the provider."""
    try:
        data = _serializer().loads(request.args.get("t", ""), salt="oauth-login-go", max_age=120)
    except (BadSignature, SignatureExpired):
        return _fail("app", "That link has expired. Please try again.")
    return _with_nonce_cookie(redirect(data["u"]), data["n"])


# ---------------------------------------------------------------------------
# Finish (called from the existing /auth/<provider>/callback routes)
# ---------------------------------------------------------------------------

def is_login_state(state):
    return bool(state) and state.startswith(STATE_PREFIX)


def complete_login():
    """Handle the provider's redirect back for a sign-in."""
    flow, linking = "app", False
    try:
        try:
            payload = _serializer().loads(
                request.args.get("state", "")[len(STATE_PREFIX):],
                salt="oauth-login-state", max_age=STATE_MAX_AGE)
        except SignatureExpired:
            raise SignInError("That sign-in took too long. Please try again.")
        except BadSignature:
            raise SignInError("That sign-in link isn't valid. Please try again.")
        flow = payload.get("f") or "app"
        linking = bool(payload.get("l"))

        if request.args.get("error"):
            raise SignInError("Sign-in was cancelled.")

        # Only the browser that started this sign-in can finish it.
        if not secrets.compare_digest(request.cookies.get(NONCE_COOKIE, ""), payload.get("n") or ""):
            raise SignInError("This sign-in was started in a different browser or has expired. Please try again.")

        provider_name = payload.get("p")
        provider = _provider(provider_name)
        if provider is None:
            raise SignInError("This sign-in method isn't available.")

        claims = _exchange_code(provider, request.args.get("code", ""), payload["n"])
        person = _person_from_claims(provider_name, claims, provider)
        user, outcome = _resolve_user(provider_name, person, payload.get("l"))
    except SignInError as error:
        AuditLog.record("unknown", "oauth_login_failure", False, reason=str(error)[:200])
        if linking and flow == "app":
            # Connecting from Settings while signed in: go back there, not to
            # the sign-in error page.
            return _clear_cookie(redirect(f"{_app_url()}/settings?{urlencode({'signin_error': str(error)})}"))
        return _clear_cookie(_fail(flow, str(error)))

    AuditLog.record(user.uuid, f"oauth_{outcome}", True, provider=provider_name)
    return _clear_cookie(_succeed(flow, user, provider_name, outcome))


def _exchange_code(provider, code, nonce):
    if not code:
        raise SignInError("The sign-in didn't return a code. Please try again.")
    data = {
        "client_id": provider["client_id"],
        "client_secret": provider["client_secret"],
        "code": code,
        "redirect_uri": provider["redirect_uri"],
        "grant_type": "authorization_code",
    }
    if provider["name"] == "microsoft":
        data["scope"] = provider["scope"]
    try:
        response = requests.post(provider["token_url"], data=data, timeout=TIMEOUT)
    except requests.RequestException:
        raise SignInError("Couldn't reach the sign-in service. Please try again.")
    if response.status_code >= 300:
        current_app.logger.warning("OAuth token exchange failed %s: %s", response.status_code, response.text[:300])
        raise SignInError("The sign-in couldn't be completed. Please try again.")
    id_token = (response.json() or {}).get("id_token")
    if not id_token:
        raise SignInError("The sign-in didn't include your identity. Please try again.")

    claims = _decode_jwt_payload(id_token)
    now = time.time()
    if claims.get("aud") != provider["client_id"]:
        raise SignInError("The sign-in was issued for a different app.")
    if float(claims.get("exp") or 0) < now - 60:
        raise SignInError("The sign-in expired. Please try again.")
    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise SignInError("The sign-in couldn't be verified. Please try again.")
    issuer = claims.get("iss") or ""
    if provider["name"] == "google" and issuer not in ("https://accounts.google.com", "accounts.google.com"):
        raise SignInError("The sign-in came from an unexpected issuer.")
    if provider["name"] == "microsoft" and issuer != f"https://login.microsoftonline.com/{claims.get('tid')}/v2.0":
        raise SignInError("The sign-in came from an unexpected issuer.")
    return claims


def _decode_jwt_payload(token):
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment.encode()))
    except (IndexError, ValueError):
        raise SignInError("The sign-in response couldn't be read.")


def _person_from_claims(provider_name, claims, provider):
    """Who signed in, and which of their details can be trusted."""
    if provider_name == "google":
        email = (claims.get("email") or "").strip().lower()
        return {
            "subject": claims.get("sub"),
            "email": email if EMAIL_PATTERN.match(email) else None,
            "email_trusted": bool(claims.get("email_verified")),
            "name": claims.get("name") or "",
        }
    # Microsoft: the sign-in name. Microsoft only allows it on a domain the
    # organisation has verified (or the person's own Microsoft account), unlike
    # the `email` claim, which an organisation admin can set to anything.
    username = (claims.get("preferred_username") or "").strip().lower()
    tenant, object_id = claims.get("tid"), claims.get("oid") or claims.get("sub")
    usable = bool(EMAIL_PATTERN.match(username)) and "#ext#" not in username
    return {
        "subject": f"{tenant}:{object_id}",
        "email": username if usable else None,
        "email_trusted": usable,
        "name": claims.get("name") or "",
        "personal": tenant == CONSUMER_TENANT,
    }


def _resolve_user(provider_name, person, link_user_uuid):
    """Find, link or create the Flyby account. Returns (user, what happened)."""
    label = PROVIDER_NAMES.get(provider_name, provider_name)
    if not person.get("subject"):
        raise SignInError(f"{label} didn't identify the account. Please try again.")
    existing_identity = UserIdentity.find(provider_name, person["subject"])

    # Connecting to an account that is already signed in.
    if link_user_uuid:
        user = User.by_uuid(link_user_uuid)
        if user is None:
            raise SignInError("Your session ended. Sign in and try connecting again.")
        if existing_identity and existing_identity._user_id != user.uuid:
            raise SignInError(f"That {label} account already signs in to a different Flyby account.")
        if existing_identity is None:
            UserIdentity.link(user.uuid, provider_name, person["subject"], person.get("email"))
        return user, "linked"

    # Signed in this way before.
    if existing_identity:
        user = User.by_uuid(existing_identity._user_id)
        if user is None:
            raise SignInError("That account no longer exists.")
        existing_identity.touch()
        return user, "login"

    email = person.get("email")
    if not email or not person.get("email_trusted"):
        raise SignInError(f"{label} didn't confirm an email address for this account, so it can't be used to sign in.")

    # An existing Flyby account with this email — the provider vouches for it.
    user = User.by_email(email)
    if user is not None:
        UserIdentity.link(user.uuid, provider_name, person["subject"], email)
        return user, "login"

    # New to Flyby: create the account.
    from api.auth import _provision_account
    created = User(
        name=(person.get("name") or email.split("@")[0]).strip(),
        uid=email,
        # Never blank: a blank password becomes the app's default password.
        password=secrets.token_urlsafe(32),
        role="User",
        email=email,
    ).create()
    if created is None:
        raise SignInError("Couldn't create your account. Please try again.")
    _provision_account(created)
    UserIdentity.link(created.uuid, provider_name, person["subject"], email, created_account=True)
    return created, "signup"


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------

def _succeed(flow, user, provider_name, outcome):
    if flow == "console":
        from flask_login import login_user
        login_user(user)
        user.touch_login()
        return redirect(url_for("account", linked=provider_name) if outcome == "linked" else url_for("account"))
    if outcome == "linked":
        return redirect(f"{_app_url()}/settings?{urlencode({'signin_linked': provider_name})}")
    code = _serializer().dumps(
        {"u": user.uuid, "j": secrets.token_urlsafe(12), "s": outcome == "signup"}, salt="oauth-login-code")
    return redirect(f"{_app_url()}/signed-in?{urlencode({'login_code': code})}")


def _fail(flow, message):
    if flow == "console":
        return redirect(url_for("login", oauth_error=message))
    return redirect(f"{_app_url()}/signed-in?{urlencode({'error': message})}")


def _clear_cookie(response):
    response = make_response(response)
    response.delete_cookie(NONCE_COOKIE, path="/auth")
    return response


# ---------------------------------------------------------------------------
# The app redeems its code for a session
# ---------------------------------------------------------------------------

def redeem_login_code(code):
    """Return ``(user, is_new_account)`` for a valid, unused code — or ``(None, False)``."""
    try:
        data = _serializer().loads(code or "", salt="oauth-login-code", max_age=CODE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None, False
    now = time.time()
    for jti, used_at in list(_redeemed_codes.items()):
        if now - used_at > CODE_MAX_AGE * 2:
            del _redeemed_codes[jti]
    if data.get("j") in _redeemed_codes:
        return None, False
    _redeemed_codes[data["j"]] = now
    return User.by_uuid(data.get("u")), bool(data.get("s"))


@oauth_login_api.route("/identities", methods=["GET"])
@token_required()
def identities():
    user = getattr(g, "current_user", None)
    if not user:
        return jsonify({"identities": []}), 200
    return jsonify({
        "identities": [i.read() for i in UserIdentity.for_user(user.uuid)],
        "available": available_providers(),
        "hasPassword": not UserIdentity.password_was_never_set(user.uuid),
    }), 200


@oauth_login_api.route("/providers", methods=["GET"])
def providers():
    """Which sign-in buttons to show. Public: the sign-in page needs it."""
    return jsonify({"providers": available_providers()}), 200
