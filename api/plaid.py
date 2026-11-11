"""
Flyby AI — Plaid integration for automatic expense capture.

The traveler links a corporate or personal card once. From then on, card
transactions are pulled in, matched to the trip that was running when they were
charged, categorized, and filed as expenses — so an expense report drafts itself
instead of being typed up from receipts.

Everything here is read-only: Plaid's Transactions product only reads activity.
Flyby never sees or stores a card number — the traveler authenticates with their
bank inside Plaid Link, and we only ever hold an opaque access token.

Not configured yet: without PLAID_CLIENT_ID / PLAID_SECRET every function
returns a "not configured" result instead of raising, so the app runs exactly as
it does today. Drop the keys in and it starts working with no code changes.
"""

import logging
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

TIMEOUT = 30
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.6

# Plaid hosts per environment.
_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}


def is_configured():
    """Whether Plaid credentials are present."""
    return bool(
        current_app.config.get("PLAID_CLIENT_ID")
        and current_app.config.get("PLAID_SECRET")
    )


def _base_url():
    env = (current_app.config.get("PLAID_ENV") or "sandbox").lower()
    return _HOSTS.get(env, _HOSTS["sandbox"])


def _credentials():
    return {
        "client_id": current_app.config.get("PLAID_CLIENT_ID"),
        "secret": current_app.config.get("PLAID_SECRET"),
    }


def _post(path, payload):
    """
    POST to Plaid with retries on network-level failures only.

    Returns (data, error). ``error`` is a short machine-readable reason so the
    UI can explain itself; ``data`` is None whenever error is set.
    """
    if not is_configured():
        return None, "not_configured"

    body = {**_credentials(), **payload}
    url = f"{_base_url()}{path}"

    last_error = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            response = requests.post(url, json=body, timeout=TIMEOUT)
            break
        except (requests.ConnectionError, requests.Timeout) as error:
            last_error = error
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF * attempt)
        except requests.RequestException as error:
            logger.warning("Plaid request to %s failed: %s", path, error)
            return None, "request_failed"
    else:
        logger.warning("Plaid unreachable at %s: %s", path, last_error)
        return None, "network_error"

    if response.status_code >= 300:
        detail = ""
        try:
            detail = response.json().get("error_code") or response.text[:200]
        except ValueError:
            detail = response.text[:200]
        logger.warning("Plaid %s returned %s: %s", path, response.status_code, detail)
        return None, detail or "plaid_error"

    try:
        return response.json(), None
    except ValueError:
        return None, "bad_response"


# ---------------------------------------------------------------------------
# Link flow — how a card gets connected
# ---------------------------------------------------------------------------

def create_link_token(user_id, client_name="Flyby AI"):
    """
    Create the short-lived token that opens Plaid Link in the browser.

    The traveler picks their bank and authenticates inside Plaid — their
    credentials and card number never reach Flyby.
    """
    payload = {
        "user": {"client_user_id": str(user_id)},
        "client_name": client_name,
        "products": ["transactions"],
        "country_codes": ["US"],
        "language": "en",
    }
    # OAuth banks (Chase, Capital One, Amex…) work on the web without this —
    # Link opens the bank's sign-in in a pop-up. A redirect URI is for mobile
    # apps, must be registered in the Plaid dashboard, and makes Plaid refuse
    # to start Link if it isn't — so it is only sent when explicitly configured.
    redirect_uri = current_app.config.get("PLAID_REDIRECT_URI")
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri
    # Where Plaid announces new charges, so they're pulled in even while nobody
    # has the app open. Plaid can only reach a public HTTPS address, so this is
    # set in production and skipped on a laptop.
    backend_url = (current_app.config.get("PUBLIC_BACKEND_URL") or "").rstrip("/")
    if backend_url.startswith("https://"):
        payload["webhook"] = f"{backend_url}/api/plaid/webhook"
    return _post("/link/token/create", payload)


def get_item(access_token):
    """Which linked account a token belongs to (``item.item_id``)."""
    return _post("/item/get", {"access_token": access_token})


def remove_item(access_token):
    """Revoke Flyby's access to a linked account at Plaid."""
    return _post("/item/remove", {"access_token": access_token})


def exchange_public_token(public_token):
    """
    Swap the one-time public token from Link for a durable access token.

    The access token is what we store (per traveler) and use to pull activity.
    It is an opaque handle — it is not card data.
    """
    return _post("/item/public_token/exchange", {"public_token": public_token})


# ---------------------------------------------------------------------------
# Transactions — the automatic part
# ---------------------------------------------------------------------------

def sync_transactions(access_token, cursor=None):
    """
    Pull new card activity since the last sync.

    Plaid's cursor model returns only what changed, so this is safe to call on a
    schedule. Returns the raw added/modified/removed sets plus the next cursor.
    """
    payload = {"access_token": access_token}
    if cursor:
        payload["cursor"] = cursor
    return _post("/transactions/sync", payload)


# ---------------------------------------------------------------------------
# Turning card activity into Flyby expenses
# ---------------------------------------------------------------------------

# Plaid's categories (its "personal finance category" taxonomy) that count as
# travel spend, mapped onto Flyby's expense categories. Anything not listed is
# not a travel expense: loan payments, interest, transfers, income, tax,
# shopping, groceries, subscriptions — the bulk of what a card carries. Those
# are neither stored nor offered.
_TRAVEL_CATEGORIES = {
    "TRAVEL_FLIGHTS": "flight",
    "TRAVEL_LODGING": "hotel",
    "TRAVEL_RENTAL_CARS": "transportation",
    "TRAVEL_OTHER_TRAVEL": "transportation",
    "TRANSPORTATION_TAXIS_AND_RIDE_SHARES": "transportation",
    "TRANSPORTATION_PUBLIC_TRANSIT": "transportation",
    "TRANSPORTATION_PARKING": "transportation",
    "TRANSPORTATION_TOLLS": "transportation",
    "TRANSPORTATION_GAS": "transportation",
    "TRANSPORTATION_BIKES_AND_SCOOTERS": "transportation",
    "TRANSPORTATION_OTHER_TRANSPORTATION": "transportation",
    "FOOD_AND_DRINK_RESTAURANT": "meals",
    "FOOD_AND_DRINK_FAST_FOOD": "meals",
    "FOOD_AND_DRINK_COFFEE": "meals",
    "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK": "meals",
    "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS": "entertainment",
}

_OFFERED_CATEGORIES = set(_TRAVEL_CATEGORIES.values())

# Only when Plaid gives no category at all: recognise travel merchants by name.
_MERCHANT_HINTS = [
    ("flight", ("airline", "airways", "air lines", "delta", "united", "american air", "jetblue",
                "southwest", "alaska air", "lufthansa", "emirates", "qatar", "duffel")),
    ("hotel", ("hotel", " inn", "marriott", "hilton", "hyatt", "westin", "sheraton",
               "airbnb", "lodging", "resort")),
    ("transportation", ("uber", "lyft", "taxi", "cab ", "metro", "transit", "parking",
                        "hertz", "avis", "enterprise rent")),
    ("meals", ("restaurant", "cafe", "coffee", "starbucks", "grill", "kitchen",
               "pizza", "sushi")),
]


def travel_category(transaction):
    """
    The Flyby expense category for a travel charge, or None if it isn't travel.

    Plaid's own category decides when it has one — a transaction it files as a
    tax payment is not treated as a hotel stay because "Hotel" is in the name.
    """
    pfc = transaction.get("personal_finance_category") or {}
    detailed = (pfc.get("detailed") or "").upper()
    if detailed or pfc.get("primary"):
        return _TRAVEL_CATEGORIES.get(detailed)
    name = (transaction.get("merchant_name") or transaction.get("name") or "").lower()
    for category, hints in _MERCHANT_HINTS:
        if any(hint in name for hint in hints):
            return category
    return None


def categorize(transaction):
    """Best-effort Flyby category for a charge (``office`` when it isn't travel)."""
    return travel_category(transaction) or "office"


def match_trip(transaction_date, trips):
    """
    Find the trip a charge belongs to: the one whose dates contain the charge.

    ``trips`` is a list of dicts with ``id``, ``name``, ``start`` and ``end``
    (ISO date strings). Returns the matching trip, or None for a charge that
    happened outside any trip (it still becomes an expense, just unassigned).
    """
    if not transaction_date:
        return None
    for trip in trips or []:
        start, end = trip.get("start"), trip.get("end")
        if start and end and start[:10] <= transaction_date[:10] <= end[:10]:
            return trip
    return None


def to_expense(transaction, trips=None):
    """
    Convert one Plaid transaction into the expense shape the frontend renders.

    Only outgoing charges become expenses — refunds and payments are skipped,
    so a credit never shows up as a business expense.
    """
    amount = transaction.get("amount")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    # Plaid reports outflow as positive; anything else is a refund/credit.
    if amount <= 0:
        return None

    date = transaction.get("authorized_date") or transaction.get("date")
    trip = match_trip(date, trips)

    return {
        "externalId": transaction.get("transaction_id"),
        "merchant": transaction.get("merchant_name") or transaction.get("name") or "Card charge",
        "description": transaction.get("name") or "",
        "date": date,
        "amount": round(amount, 2),
        "currency": transaction.get("iso_currency_code") or "USD",
        "category": categorize(transaction),
        "status": "pending",
        "location": ", ".join(
            part for part in [
                (transaction.get("location") or {}).get("city"),
                (transaction.get("location") or {}).get("region"),
            ] if part
        ),
        "paymentMethod": "Connected card",
        "reimbursable": True,
        "tripId": trip.get("id") if trip else None,
        "tripName": trip.get("name") if trip else None,
        "source": "plaid",
    }


def build_expenses(transactions, trips=None):
    """Convert a batch of transactions into expenses, dropping non-charges."""
    out = []
    for txn in transactions or []:
        expense = to_expense(txn, trips)
        if expense:
            out.append(expense)
    return out


# ---------------------------------------------------------------------------
# The import engine — runs on the server, per linked account
# ---------------------------------------------------------------------------

_MAX_PAGES = 20  # Plaid returns up to 100 changes per page


def _trip_windows(user_uuid):
    """
    The user's trips as date windows to attribute charges to.

    Each window is widened by a day on either side: a charge the evening before
    departure (airport parking, a ride to the airport) or on the morning after
    landing home belongs to the trip, and stored trip dates can sit a day off
    once converted to UTC.
    """
    from datetime import date, timedelta
    from model.trip import Trip

    windows = []
    for trip in Trip.query.filter_by(_user_id=user_uuid).all():
        data = dict(getattr(trip, "_client_data", None) or {})
        if data.get("status") in ("cancelled", "archived"):
            continue
        try:
            start = date.fromisoformat((data.get("startDate") or "")[:10]) - timedelta(days=1)
            end = date.fromisoformat((data.get("endDate") or "")[:10]) + timedelta(days=1)
        except ValueError:
            continue
        windows.append({
            "id": data.get("id") or trip.client_id,
            "name": data.get("destination") or "Trip",
            "start": start.isoformat(),
            "end": end.isoformat(),
        })
    return windows


def offerable(rows, windows):
    """
    The stored charges to offer as expenses: those that fall during a trip.

    A linked card — especially a personal one — carries everyday spending that
    has nothing to do with travel. Only charges made during a trip (the day
    before departure through the day after return) become expenses. The rest
    stay stored and are checked again on every sync, so a trip added later
    still picks up the charges from its dates.

    Re-matches each row against the current trips, updating its trip as it goes.
    """
    chosen = []
    for row in rows:
        expense = dict(row._expense or {})
        if expense.get("category") not in _OFFERED_CATEGORIES:
            continue
        trip = match_trip(expense.get("date"), windows)
        if trip is None:
            continue
        if expense.get("tripId") != trip.get("id"):
            expense["tripId"], expense["tripName"] = trip.get("id"), trip.get("name")
            row._expense = expense
        chosen.append(expense)
    return chosen


def pull_item(item):
    """
    Bring one linked account up to date: fetch every change since its cursor
    and store the resulting expenses, undelivered. Returns an error code or None.

    The cursor only advances after the charges it covers are safely stored, so a
    failure part-way leaves the next pull to fetch the same changes again.
    """
    from __init__ import db
    from model.base import utcnow
    from model.plaid import PlaidTransaction

    def fetch_all(start_cursor):
        added, modified, removed = [], [], []
        cursor = start_cursor
        for _ in range(_MAX_PAGES):
            data, error = sync_transactions(item._access_token, cursor)
            if error:
                return None, error
            added += data.get("added") or []
            modified += data.get("modified") or []
            removed += data.get("removed") or []
            cursor = data.get("next_cursor") or cursor
            if not data.get("has_more"):
                break
        return (added, modified, removed, cursor), None

    result, error = fetch_all(item._cursor)
    for _ in range(3):
        if error != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
            break
        # The account changed while being read (Plaid is still applying an
        # update). Plaid's instruction: start the whole read over.
        time.sleep(1.5)
        result, error = fetch_all(item._cursor)
    if error:
        item._last_error = str(error)[:255]
        db.session.commit()
        return error

    added, modified, removed, next_cursor = result
    windows = _trip_windows(item._user_id)

    for txn in added + modified:
        # Only posted charges become expenses. A pending charge is withdrawn and
        # re-issued under a new id when it posts; importing both would file the
        # same purchase twice. It is picked up once it posts.
        if txn.get("pending"):
            continue
        txn_id = txn.get("transaction_id")
        # Not travel spend: not stored. Flyby has no reason to keep someone's
        # loan payments or grocery shopping. (If Plaid recategorised a stored
        # charge as non-travel before it was filed, it is dropped.)
        expense = to_expense(txn, windows) if travel_category(txn) else None
        if not expense or not txn_id:
            stale = PlaidTransaction.query.get(txn_id) if txn_id else None
            if stale is not None and not stale._delivered:
                db.session.delete(stale)
            continue  # refunds, credits and non-travel spend aren't expenses
        expense["externalId"] = txn_id
        row = PlaidTransaction.query.get(txn_id)
        if row is None:
            db.session.add(PlaidTransaction(
                id=txn_id, _user_id=item._user_id, _item_id=item._item_id,
                _expense=expense, _delivered=False))
        elif not row._delivered:
            row._expense = expense  # not filed yet — take the latest details

    for gone in removed:
        row = PlaidTransaction.query.get(gone.get("transaction_id") or "")
        if row is None:
            continue
        if row._delivered:
            row._removed = True  # the app removes it if it hasn't been submitted
        else:
            db.session.delete(row)

    item._cursor = next_cursor
    item._last_synced_at = utcnow()
    item._last_error = None
    db.session.commit()
    return None
