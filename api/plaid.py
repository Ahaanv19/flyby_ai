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
    return _post("/link/token/create", {
        "user": {"client_user_id": str(user_id)},
        "client_name": client_name,
        "products": ["transactions"],
        "country_codes": ["US"],
        "language": "en",
    })


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

# Plaid's category strings mapped onto the categories Flyby's expense UI uses.
_CATEGORY_MAP = {
    "TRAVEL_FLIGHTS": "flight",
    "TRAVEL_LODGING": "hotel",
    "TRAVEL_TAXIS_AND_RIDE_SHARES": "transportation",
    "TRAVEL_PUBLIC_TRANSIT": "transportation",
    "TRAVEL_PARKING": "transportation",
    "TRAVEL_RENTAL_CARS": "transportation",
    "TRAVEL_CAR_SERVICE": "transportation",
    "FOOD_AND_DRINK_RESTAURANT": "meals",
    "FOOD_AND_DRINK_FAST_FOOD": "meals",
    "FOOD_AND_DRINK_COFFEE": "meals",
    "FOOD_AND_DRINK_GROCERIES": "meals",
    "ENTERTAINMENT": "entertainment",
}

# Fallback keyword matching for merchants when Plaid gives no useful category.
_MERCHANT_HINTS = [
    ("flight", ("airline", "airways", "delta", "united", "american air", "jetblue",
                "southwest", "alaska air", "lufthansa", "emirates", "qatar")),
    ("hotel", ("hotel", "inn", "marriott", "hilton", "hyatt", "westin", "sheraton",
               "airbnb", "lodging", "resort")),
    ("transportation", ("uber", "lyft", "taxi", "cab", "metro", "transit", "parking",
                        "hertz", "avis", "enterprise rent")),
    ("meals", ("restaurant", "cafe", "coffee", "starbucks", "grill", "kitchen",
               "pizza", "sushi", "bar ")),
]


def categorize(transaction):
    """Best-effort Flyby category for a Plaid transaction."""
    detailed = ((transaction.get("personal_finance_category") or {}).get("detailed") or "").upper()
    if detailed in _CATEGORY_MAP:
        return _CATEGORY_MAP[detailed]

    primary = ((transaction.get("personal_finance_category") or {}).get("primary") or "").upper()
    if primary == "TRAVEL":
        return "transportation"
    if primary == "FOOD_AND_DRINK":
        return "meals"

    name = (transaction.get("merchant_name") or transaction.get("name") or "").lower()
    for category, hints in _MERCHANT_HINTS:
        if any(hint in name for hint in hints):
            return category
    return "office"


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
