"""
Flyby AI — Google Calendar integration.

Reads the traveler's calendar so meetings that imply travel can be turned into
trips, and writes confirmed trips back onto their calendar.

The traveler authenticates with Google directly (OAuth); Flyby only ever holds
tokens, never a password. Calendar scopes are requested narrowly — read events,
and manage the events Flyby itself creates.

Not configured yet: without GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET every
function reports "not configured" instead of raising, so the app runs exactly as
it does today and the UI can honestly show "Coming soon". Drop the credentials
in and it starts working with no code changes.
"""

import logging
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

TIMEOUT = 30
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.6

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Read the calendar, and manage only the events Flyby creates. `email` is used
# to show which account is connected.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
]


def is_configured():
    """Whether Google OAuth credentials are present."""
    return bool(
        current_app.config.get("GOOGLE_CLIENT_ID")
        and current_app.config.get("GOOGLE_CLIENT_SECRET")
    )


def _redirect_uri():
    return current_app.config.get("GOOGLE_REDIRECT_URI") or ""


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
            logger.warning("Google request to %s failed: %s", url, error)
            return None
    logger.warning("Google unreachable at %s: %s", url, last_error)
    return None


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def build_auth_url(state):
    """
    The URL that starts Google's consent screen.

    ``access_type=offline`` + ``prompt=consent`` are what return a refresh token,
    so the connection keeps working after the first hour without re-prompting.
    """
    if not is_configured():
        return None
    from urllib.parse import urlencode
    params = {
        "client_id": current_app.config["GOOGLE_CLIENT_ID"],
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code):
    """Swap the one-time authorization code for access + refresh tokens."""
    if not is_configured():
        return None, "not_configured"
    response = _request("POST", TOKEN_URL, data={
        "code": code,
        "client_id": current_app.config["GOOGLE_CLIENT_ID"],
        "client_secret": current_app.config["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    })
    if response is None:
        return None, "network_error"
    if response.status_code >= 300:
        logger.warning("Google token exchange %s: %s", response.status_code, response.text[:200])
        return None, "exchange_failed"
    try:
        return response.json(), None
    except ValueError:
        return None, "bad_response"


def refresh_access_token(refresh_token):
    """Get a fresh access token; they expire after about an hour."""
    if not is_configured():
        return None, "not_configured"
    response = _request("POST", TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": current_app.config["GOOGLE_CLIENT_ID"],
        "client_secret": current_app.config["GOOGLE_CLIENT_SECRET"],
        "grant_type": "refresh_token",
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
    """Which Google account is connected, for display."""
    response = _request("GET", USERINFO_URL,
                        headers={"Authorization": f"Bearer {access_token}"})
    if response is None or response.status_code >= 300:
        return None
    try:
        return response.json().get("email")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def list_events(access_token, time_min=None, max_results=50):
    """Upcoming calendar events, normalized into the app's event shape."""
    params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": max_results,
    }
    if time_min:
        params["timeMin"] = time_min
    response = _request("GET", CALENDAR_URL,
                        headers={"Authorization": f"Bearer {access_token}"},
                        params=params)
    if response is None:
        return None, "network_error"
    if response.status_code == 401:
        return None, "token_expired"
    if response.status_code >= 300:
        return None, "request_failed"
    try:
        items = response.json().get("items", [])
    except ValueError:
        return None, "bad_response"
    return [normalize_event(e) for e in items], None


def normalize_event(event):
    """Google event -> the shape the calendar UI already renders."""
    start = event.get("start") or {}
    end = event.get("end") or {}
    return {
        "id": event.get("id"),
        "title": event.get("summary") or "(no title)",
        "startDate": (start.get("dateTime") or start.get("date") or "")[:10],
        "endDate": (end.get("dateTime") or end.get("date") or "")[:10],
        "location": event.get("location") or "",
        "description": event.get("description") or "",
        "attendees": [a.get("email") for a in (event.get("attendees") or []) if a.get("email")],
    }


def create_event(access_token, title, start_date, end_date, location="", description=""):
    """Put a confirmed trip on the traveler's calendar."""
    body = {
        "summary": title,
        "location": location,
        "description": description,
        # All-day span covering the trip.
        "start": {"date": start_date},
        "end": {"date": end_date},
    }
    response = _request("POST", CALENDAR_URL,
                        headers={"Authorization": f"Bearer {access_token}",
                                 "Content-Type": "application/json"},
                        json=body)
    if response is None:
        return None, "network_error"
    if response.status_code == 401:
        return None, "token_expired"
    if response.status_code >= 300:
        logger.warning("Google create event %s: %s", response.status_code, response.text[:200])
        return None, "request_failed"
    try:
        return normalize_event(response.json()), None
    except ValueError:
        return None, "bad_response"
