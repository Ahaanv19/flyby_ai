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
from datetime import datetime, timedelta, timezone
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

def list_events(access_token, time_min=None, max_results=50, window_days=180):
    """
    Upcoming events, normalized into the shape the calendar UI renders.

    Uses Graph's calendarView rather than /me/events. Two reasons, both of which
    matter for trip detection:

      * calendarView expands a recurring series into its individual occurrences,
        so a weekly on-site in Chicago produces a meeting Flyby can act on.
        /me/events returns only the series master.
      * It takes an explicit date window as parameters. Asking /me/events for
        future meetings means a $filter on start/dateTime alongside an $orderby
        on the same property, which Exchange rejects on many mailboxes ("the
        restriction or sort order is too complex"), and the whole list comes
        back empty.

    Returns (events, error). On a query Graph dislikes we retry once against
    plain /me/events with no filter and no sort — fewer guarantees about
    ordering, but the traveler sees their calendar rather than nothing.
    """
    start = (time_min or _now_iso())[:19].rstrip("Z")
    end = _plus_days(start, window_days)

    headers = {
        "Authorization": f"Bearer {access_token}",
        # Ask Graph to express every start/end in UTC so the date we slice off
        # the front is the date the meeting is actually on.
        "Prefer": 'outlook.timezone="UTC"',
    }
    params = {
        "startDateTime": start,
        "endDateTime": end,
        "$orderby": "start/dateTime",
        "$top": max_results,
        "$select": "id,subject,start,end,location,bodyPreview,attendees,isCancelled",
    }
    response = _request("GET", f"{GRAPH}/me/calendarView", headers=headers, params=params)

    if response is not None and response.status_code == 400:
        # Some mailboxes reject the sort; the events themselves are still there.
        logger.warning("Microsoft calendarView rejected the query (%s), retrying unsorted: %s",
                       response.status_code, response.text[:300])
        params.pop("$orderby", None)
        response = _request("GET", f"{GRAPH}/me/calendarView", headers=headers, params=params)

    if response is not None and 400 <= response.status_code < 500 and response.status_code != 401:
        logger.warning("Microsoft calendarView %s, falling back to /me/events: %s",
                       response.status_code, response.text[:300])
        response = _request("GET", f"{GRAPH}/me/events", headers=headers, params={
            "$top": max_results,
            "$select": params["$select"],
        })

    if response is None:
        return None, "network_error"
    if response.status_code == 401:
        return None, "token_expired"
    if response.status_code >= 300:
        logger.warning("Microsoft events %s: %s", response.status_code, response.text[:300])
        return None, _graph_error(response)
    try:
        items = response.json().get("value", [])
    except ValueError:
        return None, "bad_response"

    events = [normalize_event(e) for e in items]
    # The fallback path is unsorted and unbounded; keep the same contract the
    # caller gets from calendarView so the UI never has to care which ran.
    today = start[:10]
    events = [e for e in events if (e.get("endDate") or e.get("startDate") or "") >= today]
    events.sort(key=lambda e: (e.get("startDate") or "", e.get("title") or ""))
    return events, None


def _graph_error(response):
    """A short, machine-readable reason from a Graph error body."""
    try:
        return (response.json().get("error") or {}).get("code") or "request_failed"
    except ValueError:
        return "request_failed"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _plus_days(iso_start, days):
    """The far end of the calendar window, as Graph wants it."""
    try:
        base = datetime.strptime(iso_start[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        base = datetime.now(timezone.utc).replace(tzinfo=None)
    return (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _location_text(location):
    """
    The full place an Outlook event is at, as one string.

    Picking a suggestion in Outlook's Location box stores the venue and its
    address separately: displayName is just "San Diego International Airport",
    and the city lives in address.city. Outlook stitches them together on screen,
    but Graph doesn't — so reading displayName alone threw the city away, and a
    meeting at an airport, hotel or office never registered as travel.

    Google has no equivalent field; it keeps whatever the organizer typed.
    """
    display = (location.get("displayName") or "").strip()
    address = location.get("address") or {}
    parts = [
        address.get("street"),
        address.get("city"),
        address.get("state"),
        address.get("countryOrRegion"),
    ]
    tail = [p.strip() for p in parts if p and p.strip()]
    # Don't repeat what displayName already says (a typed "Chicago, IL" can come
    # back with the same city in address.city). Compare whole segments: a
    # substring test would drop "San Diego" for appearing inside "San Diego
    # International Airport" — the one part we actually need.
    shown = {seg.strip().lower() for seg in display.split(",")}
    tail = [p for p in tail if p.lower() not in shown]
    return ", ".join([display, *tail]) if display else ", ".join(tail)


def normalize_event(event):
    """Graph event -> the same shape the Google integration returns."""
    start = (event.get("start") or {}).get("dateTime") or ""
    end = (event.get("end") or {}).get("dateTime") or ""
    location = _location_text(event.get("location") or {})
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
