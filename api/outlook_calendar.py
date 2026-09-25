"""
Flyby AI — Outlook / Microsoft 365 calendar integration (Microsoft Graph).

Reads the traveler's Outlook calendar so meetings that imply travel can become
trips, and writes confirmed trips back onto their calendar.

The traveler signs in with Microsoft directly; Flyby only ever holds tokens, and
the client secret never leaves the backend. Permissions are delegated and narrow:
read calendars, manage calendar events, and read the signed-in user's profile.

The app is registered multi-tenant, so employees at any customer company can
connect — the OAuth authority defaults to "organizations" (any work or school
account) rather than a single company's tenant.

Not configured: without MS_CLIENT_ID / MS_CLIENT_SECRET every function reports
"not configured" instead of raising, so the app behaves exactly as it does
without it and the UI can honestly show "Coming soon".
"""

import logging
import time
from urllib.parse import urlencode

import requests
from flask import current_app

logger = logging.getLogger(__name__)

TIMEOUT = 30
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.6

GRAPH = "https://graph.microsoft.com/v1.0"

# Delegated, least-privilege. offline_access is what returns a refresh token so
# the connection survives past the first hour without re-prompting.
SCOPES = [
    "offline_access",
    "User.Read",
    "Calendars.Read",
    "Calendars.ReadWrite",
]


def is_configured():
    """Whether Microsoft OAuth credentials are present."""
    return bool(
        current_app.config.get("MS_CLIENT_ID")
        and current_app.config.get("MS_CLIENT_SECRET")
    )


def _authority():
    """
    Which accounts may sign in.

    "organizations" = any work/school tenant, which is what a multi-tenant B2B
    app wants. Set MS_AUTHORITY to a specific tenant id to lock it to one
    company.
    """
    return current_app.config.get("MS_AUTHORITY") or "organizations"


def _redirect_uri():
    return current_app.config.get("MS_REDIRECT_URI") or ""


def _oauth_url(leaf):
    return f"https://login.microsoftonline.com/{_authority()}/oauth2/v2.0/{leaf}"


def _request(method, url, **kwargs):
    """HTTP with retries on connection/TLS/timeout errors only."""
    last_error = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return requests.request(method, url, timeout=TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as error:
            last_error = error
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF * attempt)
        except requests.RequestException as error:
            logger.warning("Microsoft request to %s failed: %s", url, error)
            return None
    logger.warning("Microsoft unreachable at %s: %s", url, last_error)
    return None


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def build_auth_url(state):
    """The URL that starts Microsoft's consent screen."""
    if not is_configured():
        return None
    params = {
        "client_id": current_app.config["MS_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return f"{_oauth_url('authorize')}?{urlencode(params)}"


def exchange_code(code):
    """Swap the one-time authorization code for access + refresh tokens."""
    if not is_configured():
        return None, "not_configured"
    response = _request("POST", _oauth_url("token"), data={
        "client_id": current_app.config["MS_CLIENT_ID"],
        "client_secret": current_app.config["MS_CLIENT_SECRET"],
        "code": code,
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
        "scope": " ".join(SCOPES),
    })
    if response is None:
        return None, "network_error"
    if response.status_code >= 300:
        logger.warning("Microsoft token exchange %s: %s", response.status_code, response.text[:200])
        return None, "exchange_failed"
    try:
        return response.json(), None
    except ValueError:
        return None, "bad_response"


def refresh_access_token(refresh_token):
    """Get a fresh access token; Microsoft's expire after about an hour."""
    if not is_configured():
        return None, "not_configured"
    response = _request("POST", _oauth_url("token"), data={
        "client_id": current_app.config["MS_CLIENT_ID"],
        "client_secret": current_app.config["MS_CLIENT_SECRET"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": " ".join(SCOPES),
    })
    if response is None:
        return None, "network_error"
    if response.status_code >= 300:
        return None, "refresh_failed"
    try:
        return response.json(), None
    except ValueError:
        return None, "bad_response"


def get_account_email(access_token):
    """Which Microsoft account is connected, for display."""
    response = _request("GET", f"{GRAPH}/me",
                        headers={"Authorization": f"Bearer {access_token}"})
    if response is None or response.status_code >= 300:
        return None
    try:
        body = response.json()
        return body.get("mail") or body.get("userPrincipalName")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def list_events(access_token, time_min=None, max_results=50):
    """Upcoming events, normalized into the shape the calendar UI renders."""
    params = {
        "$orderby": "start/dateTime",
        "$top": max_results,
        "$select": "id,subject,start,end,location,bodyPreview,attendees,isCancelled",
    }
    if time_min:
        # Only future meetings — past ones can't produce a trip to book.
        params["$filter"] = f"start/dateTime ge '{time_min}'"
    response = _request("GET", f"{GRAPH}/me/events",
                        headers={"Authorization": f"Bearer {access_token}"},
                        params=params)
    if response is None:
        return None, "network_error"
    if response.status_code == 401:
        return None, "token_expired"
    if response.status_code >= 300:
        logger.warning("Microsoft events %s: %s", response.status_code, response.text[:200])
        return None, "request_failed"
    try:
        items = response.json().get("value", [])
    except ValueError:
        return None, "bad_response"
    return [normalize_event(e) for e in items], None


def normalize_event(event):
    """Graph event -> the same shape the Google integration returns."""
    start = (event.get("start") or {}).get("dateTime") or ""
    end = (event.get("end") or {}).get("dateTime") or ""
    location = (event.get("location") or {}).get("displayName") or ""
    return {
        "id": event.get("id"),
        "title": event.get("subject") or "(no title)",
        "startDate": start[:10],
        "endDate": end[:10],
        "location": location,
        "description": event.get("bodyPreview") or "",
        "attendees": [
            (a.get("emailAddress") or {}).get("address")
            for a in (event.get("attendees") or [])
            if (a.get("emailAddress") or {}).get("address")
        ],
        "cancelled": bool(event.get("isCancelled")),
    }


def create_event(access_token, title, start_date, end_date, location="", description=""):
    """Put a confirmed trip on the traveler's Outlook calendar."""
    body = {
        "subject": title,
        "body": {"contentType": "text", "content": description},
        "location": {"displayName": location},
        # All-day span covering the trip. Graph treats `end` as exclusive.
        "isAllDay": True,
        "start": {"dateTime": f"{start_date}T00:00:00", "timeZone": "UTC"},
        "end": {"dateTime": f"{end_date}T00:00:00", "timeZone": "UTC"},
    }
    response = _request("POST", f"{GRAPH}/me/events",
                        headers={"Authorization": f"Bearer {access_token}",
                                 "Content-Type": "application/json"},
                        json=body)
    if response is None:
        return None, "network_error"
    if response.status_code == 401:
        return None, "token_expired"
    if response.status_code >= 300:
        logger.warning("Microsoft create event %s: %s", response.status_code, response.text[:200])
        return None, "request_failed"
    try:
        return normalize_event(response.json()), None
    except ValueError:
        return None, "bad_response"
