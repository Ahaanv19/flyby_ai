"""
Flyby AI — which address of the app to send someone back to.

The app can be served from more than one place at once: GitHub Pages
(https://rbrandt2006-hash.github.io/flyby), Flyby's own domain
(https://flybyai.net), and localhost in development. When the backend sends a
browser back to the app (after Google/Microsoft sign-in or connecting a
calendar), it should be the site the person started from, not one fixed
address.

The app says where it lives; the backend only accepts that when the origin is
one of Flyby's own front-end addresses (``FRONTEND_ORIGINS`` and ``APP_URL``).
Anything else falls back to ``APP_URL``, so nobody can use Flyby's sign-in to
send a browser to another site.
"""

import re
from urllib.parse import urlsplit

from flask import current_app

# A path is a folder the app is served from, like "/flyby". Nothing that could
# smuggle in a query, fragment or another host.
_SAFE_PATH = re.compile(r"^(/[A-Za-z0-9._~-]+)*/?$")


def configured_app_url():
    """``APP_URL`` without a trailing slash: the default place to send people."""
    return (current_app.config.get("APP_URL") or "").rstrip("/")


def allowed_app_origins():
    """Every origin the app may be served from, lower-case."""
    allowed = {o.rstrip("/").lower() for o in (current_app.config.get("FRONTEND_ORIGINS") or [])}
    app_parts = urlsplit(configured_app_url())
    if app_parts.netloc:
        allowed.add(f"{app_parts.scheme}://{app_parts.netloc}".lower())
    if current_app.config.get("IS_PRODUCTION"):
        allowed = {o for o in allowed if not o.startswith(("http://localhost", "http://127.0.0.1"))}
    return allowed


def app_base(requested):
    """
    The app address to return to: ``requested`` if it is one of Flyby's own,
    otherwise ``APP_URL``. Always without a trailing slash, never with a query.
    """
    fallback = configured_app_url()
    candidate = (requested or "").strip()
    if not candidate or len(candidate) > 300:
        return fallback
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return fallback
    if (parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password
            or parts.query or parts.fragment or not _SAFE_PATH.match(parts.path or "/")):
        return fallback
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    if origin not in allowed_app_origins():
        return fallback
    return f"{origin}{parts.path.rstrip('/')}"


# ---------------------------------------------------------------------------
# Connecting a calendar
# ---------------------------------------------------------------------------
# Google and Microsoft hand the ``state`` value back unchanged on the return
# trip, so it carries the app address to land on. It is signed, so a state made
# up by someone else is ignored and the browser goes to APP_URL instead.

_CALENDAR_SALT = "calendar-connect-state"


def _calendar_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def calendar_state(user, return_to):
    """The ``state`` for a calendar consent URL."""
    user_id = str(getattr(user, "uuid", None) or getattr(user, "id", "anonymous"))
    return _calendar_serializer().dumps({"u": user_id, "r": app_base(return_to)}, salt=_CALENDAR_SALT)


def calendar_return_base(state):
    """The app address a calendar consent should come back to."""
    from itsdangerous import BadSignature
    try:
        data = _calendar_serializer().loads(state or "", salt=_CALENDAR_SALT, max_age=3600)
    except BadSignature:  # also covers an expired state
        return configured_app_url()
    return app_base(data.get("r"))
