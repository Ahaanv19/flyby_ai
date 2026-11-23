"""
Flyby AI — when an email goes out, and to whom.

The rest of the backend calls one of these at the moment something happens —
``notify.signed_in(user)``, ``notify.flight_booked(user, order)`` — and gets on
with the request. Each function decides whether the email should be sent at
all, builds it (``api.email_templates``) and queues it (``api.mailer``).

Which emails can be turned off, and which can't:

* **Sign-in alerts** follow the traveler's "Email me when my account is signed
  in to" setting.
* **Trip emails** that Flyby merely records (a trip added from another site, a
  trip marked cancelled) follow "Trip updates".
* **Confirmations of a charge** (a flight or hotel Flyby booked) and **security
  notices** (password changed, password reset, two-step verification changed)
  are always sent. Those are the ones a person needs most if the account isn't
  in their hands.

Every function here is safe to call from anywhere: none of them raise, and with
no mail server configured they do nothing.
"""

import logging
from datetime import datetime
from urllib.parse import urlsplit

from flask import current_app

from api import email_templates as templates
from api import mailer

logger = logging.getLogger(__name__)


def _app_url():
    return (current_app.config.get("APP_URL") or "").rstrip("/")


def _setting(user, attribute, default=True):
    """One of the traveler's notification switches."""
    try:
        profile = user.profile
        value = getattr(profile, attribute, None) if profile else None
        return default if value is None else bool(value)
    except Exception:  # noqa: BLE001
        return default


def _send(user, kind, built):
    try:
        subject, text, html = built
        return mailer.send(user.email, subject, text, html, kind=kind, user_id=user.uuid)
    except Exception as error:  # noqa: BLE001 — an email must never break what caused it
        logger.warning("Could not queue %s email: %s", kind, error)
        return False


def _guard(function):
    """Building an email must never raise into the request that triggered it."""
    def wrapper(*args, **kwargs):
        try:
            if not mailer.is_configured():
                return False
            return function(*args, **kwargs)
        except Exception as error:  # noqa: BLE001
            logger.warning("%s email skipped: %s", function.__name__, error)
            return False
    wrapper.__name__ = function.__name__
    wrapper.__doc__ = function.__doc__
    return wrapper


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@_guard
def welcome(user):
    """A new account was created."""
    return _send(user, "welcome", templates.welcome(user.name, _app_url()))


@_guard
def signed_in(user, *, ip_address=None, browser=None, method=None):
    """Someone signed in to the account. Follows the sign-in alert setting."""
    if not _setting(user, "_notify_sign_in"):
        return False
    return _send(user, "sign_in", templates.sign_in(
        user.name, datetime.utcnow(), browser=browser, ip_address=ip_address,
        app_url=_app_url(), method=method))


@_guard
def password_changed(user, *, ip_address=None):
    """The password was changed. Always sent."""
    return _send(user, "password_changed", templates.password_changed(
        user.name, datetime.utcnow(), ip_address=ip_address, app_url=_app_url()))


@_guard
def password_reset(user, link, minutes=30):
    """A reset link was requested. Always sent."""
    return _send(user, "password_reset", templates.password_reset(user.name, link, minutes))


@_guard
def two_factor_changed(user, enabled):
    """Two-step verification was turned on or off. Always sent."""
    return _send(user, "two_factor_changed", templates.two_factor_changed(user.name, enabled, datetime.utcnow()))


def reset_link_base(requested):
    """
    Where a password-reset link may point.

    The app tells the backend which page to send the traveler to, but the
    backend puts a live reset token on the end of it and emails it — so an
    address taken on trust would let anyone have Flyby send a victim a genuine
    reset email whose link goes somewhere else, token included. Only a page on
    one of Flyby's own front-end addresses is accepted; anything else falls
    back to the configured app address.
    """
    fallback = f"{_app_url()}/reset-password"
    candidate = (requested or "").strip()
    if not candidate:
        return fallback
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return fallback
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        return fallback
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    allowed = {o.rstrip("/").lower() for o in (current_app.config.get("FRONTEND_ORIGINS") or [])}
    app_parts = urlsplit(_app_url())
    if app_parts.netloc:
        allowed.add(f"{app_parts.scheme}://{app_parts.netloc}".lower())
    if current_app.config.get("IS_PRODUCTION"):
        allowed = {o for o in allowed if not o.startswith(("http://localhost", "http://127.0.0.1"))}
    if origin not in allowed:
        return fallback
    # The right site but outside the app: the app is served from a folder
    # (/flyby on GitHub Pages), and a link to the bare origin doesn't reach it.
    app_origin = f"{app_parts.scheme}://{app_parts.netloc}".lower()
    app_path = app_parts.path.rstrip("/")
    if origin == app_origin and app_path and not parts.path.startswith(f"{app_path}/"):
        return fallback
    return candidate


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------

@_guard
def flight_booked(user, order):
    """The airline ticketed a flight Flyby booked and charged for. Always sent."""
    # A confirmation with nothing to confirm would be worse than none.
    if not (order or {}).get("booking_reference") and not (order or {}).get("slices"):
        return False
    return _send(user, "flight_booked", templates.flight_booked(user.name, order or {}, _app_url()))


@_guard
def hotel_reserved(user, booking):
    """A hotel accepted a reservation Flyby made. Always sent."""
    if not (booking or {}).get("reference") and not (booking or {}).get("hotelName"):
        return False
    return _send(user, "hotel_reserved", templates.hotel_reserved(user.name, booking or {}, _app_url()))


@_guard
def trip_added(user, trip):
    """A flight booked on an airline's own site was added. Follows "Trip updates"."""
    if not _setting(user, "_notify_trip_updates"):
        return False
    return _send(user, "trip_added", templates.trip_added(user.name, trip or {}, _app_url()))


@_guard
def trip_cancelled(user, trip):
    """A trip was marked cancelled. Follows "Trip updates"."""
    if not _setting(user, "_notify_trip_updates"):
        return False
    return _send(user, "trip_cancelled", templates.trip_cancelled(user.name, trip or {}, _app_url()))


# ---------------------------------------------------------------------------
# Company portals
# ---------------------------------------------------------------------------

@_guard
def portal_requested(user, company):
    """
    Tell Flyby's reviewers a portal is waiting. Only when ``PORTAL_REVIEW_EMAIL``
    is set — otherwise the console's Companies page is where requests show up.
    """
    reviewer = (current_app.config.get("PORTAL_REVIEW_EMAIL") or "").strip()
    if not reviewer:
        return False
    console = (current_app.config.get("PUBLIC_BACKEND_URL") or "").rstrip("/") + "/companies"
    subject, text, html = templates.portal_requested(company._name, company._domain, user.name, user.email, console)
    return mailer.send(reviewer, subject, text, html, kind="portal_requested", user_id=user.uuid)


@_guard
def portal_decided(user, company, decision):
    """The person who asked is told whether their portal was verified. Always sent."""
    from model.company import format_join_code
    if decision == "verify":
        built = templates.portal_verified(user.name, company._name, format_join_code(company._join_code), _app_url())
    else:
        built = templates.portal_rejected(user.name, company._name, company._review_note)
    return _send(user, f"portal_{decision}", built)


@_guard
def member_joined(company, member):
    """Every admin of the company is told when someone joins it."""
    from model.user import Profile, User
    admins = Profile.query.filter_by(_company_id=company.id, _company_role="admin").all()
    sent = False
    for admin_profile in admins:
        admin = User.by_uuid(admin_profile.user_id)
        if not admin or admin.uuid == member.uuid:
            continue
        sent = _send(admin, "member_joined", templates.member_joined(
            admin.name, company._name, member.name, member.email, _app_url())) or sent
    return sent


# ---------------------------------------------------------------------------
# Expense approval
# ---------------------------------------------------------------------------

@_guard
def expense_submitted(approver, member, company, expense, note=None):
    """A traveler sent an expense to this company admin to review. Always sent."""
    return _send(approver, "expense_submitted", templates.expense_submitted(
        approver.name, member.name, company._name, expense, note, _app_url()))


@_guard
def expense_decided(member, expense, decision, reviewer_name, note=None):
    """The traveler hears that their expense was approved or sent back. Always sent."""
    return _send(member, f"expense_{decision}", templates.expense_decided(
        member.name, expense, decision, reviewer_name, note, _app_url()))
