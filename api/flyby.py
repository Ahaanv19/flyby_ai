"""
Flyby AI — feature API (``/api``).

These endpoints back the parts of the app that keep a whole collection in
memory and save it as a unit: trips, expenses, conversations, notifications,
itineraries, travel preferences and client companies.

Each collection supports the same two operations:

    GET  /api/<collection>    load everything the caller owns
    PUT  /api/<collection>    save the collection as it now stands

``PUT`` replaces the caller's collection: rows that are gone from the payload
are deleted, new ones are inserted, and the rest are updated in place. That
matches how the app already thinks about this data, and because the replace is
scoped to the signed-in user it can never touch anyone else's rows.

Objects are stored in the app's own shape (``read_client``/``apply_client``) so
nothing is lost in translation, while the columns underneath stay populated for
the reports and admin console.
"""

from flask import Blueprint, g, jsonify, request

from __init__ import db
from api.jwt_authorize import token_required
from model.base import new_uuid, utcnow
from model.chat import Chat
from model.expense import Expense
from model.notification import Notification
from model.preferences import ClientCompany, TravelPreference
from model.trip import Itinerary, Trip
from model.user import User

flyby_api = Blueprint('flyby_api', __name__, url_prefix='/api')


def _fail(message, status=400):
    return jsonify({"error": message}), status


def _payload_list(key):
    """Read a list from the request body, accepting a bare list or {key: [...]}."""
    body = request.get_json(silent=True)
    if isinstance(body, list):
        return body, None
    if isinstance(body, dict):
        rows = body.get(key)
        if isinstance(rows, list):
            return rows, None
        if rows is None:
            return [], None
    return None, "Expected a list of items."


def _sync_collection(model, rows, user_id, id_prefix=""):
    """
    Replace the caller's rows for ``model`` with ``rows``.

    Existing rows are updated in place (so created_at and any server-side
    columns survive), missing ones are deleted, and new ones are inserted.
    Everything is scoped to ``user_id``, and the whole sync is one transaction.
    """
    existing = {row.id: row for row in model.query.filter_by(_user_id=user_id).all()}
    seen = set()

    for item in rows:
        if not isinstance(item, dict):
            continue
        row_id = str(item.get("id") or "").strip() or f"{id_prefix}{new_uuid()}"
        seen.add(row_id)

        row = existing.get(row_id)
        if row is None:
            row = model()
            row.id = row_id
            row._user_id = user_id
            db.session.add(row)
        row.apply_client(item)

    for row_id, row in existing.items():
        if row_id not in seen:
            db.session.delete(row)

    db.session.commit()


def _collection_endpoint(model, key, id_prefix=""):
    """Build the GET/PUT pair for one collection."""

    def handler():
        user_id = g.user_id

        if request.method == 'GET':
            rows = model.query.filter_by(_user_id=user_id).all()
            return jsonify({key: [row.read_client() for row in rows]}), 200

        rows, error = _payload_list(key)
        if error:
            return _fail(error)
        try:
            _sync_collection(model, rows, user_id, id_prefix)
        except Exception as err:  # noqa: BLE001
            db.session.rollback()
            return _fail(f"Could not save {key}: {err}", 500)

        saved = model.query.filter_by(_user_id=user_id).all()
        return jsonify({key: [row.read_client() for row in saved]}), 200

    return handler


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

@flyby_api.route('/trips', methods=['GET', 'PUT'])
@token_required()
def trips():
    """Load or save the caller's trips."""
    return _collection_endpoint(Trip, "trips", "trip_")()


@flyby_api.route('/expenses', methods=['GET', 'PUT'])
@token_required()
def expenses():
    """Load or save the caller's expenses."""
    return _collection_endpoint(Expense, "expenses", "exp_")()


@flyby_api.route('/chats', methods=['GET', 'PUT'])
@token_required()
def chats():
    """Load or save the caller's conversations."""
    return _collection_endpoint(Chat, "chats", "chat_")()


@flyby_api.route('/notifications', methods=['GET', 'PUT'])
@token_required()
def notifications():
    """Load or save the caller's notifications."""
    return _collection_endpoint(Notification, "notifications", "notif_")()


@flyby_api.route('/client-companies', methods=['GET', 'PUT'])
@token_required()
def client_companies():
    """Load or save the caller's client company list."""
    user_id = g.user_id

    if request.method == 'GET':
        rows = ClientCompany.query.filter_by(_user_id=user_id).all()
        return jsonify({"companies": [row.read_client() for row in rows]}), 200

    rows, error = _payload_list("companies")
    if error:
        return _fail(error)

    try:
        existing = {row.id: row for row in ClientCompany.query.filter_by(_user_id=user_id).all()}
        seen = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            row_id = str(item.get("id") or "").strip() or f"co_{new_uuid()}"
            seen.add(row_id)
            row = existing.get(row_id)
            if row is None:
                row = ClientCompany(id=row_id, _user_id=user_id)
                db.session.add(row)
            row._name = item.get("name") or row._name or ""
            row._industry = item.get("industry")
            row._notes = item.get("notes")
        for row_id, row in existing.items():
            if row_id not in seen:
                db.session.delete(row)
        db.session.commit()
    except Exception as err:  # noqa: BLE001
        db.session.rollback()
        return _fail(f"Could not save companies: {err}", 500)

    saved = ClientCompany.query.filter_by(_user_id=user_id).all()
    return jsonify({"companies": [row.read_client() for row in saved]}), 200


# ---------------------------------------------------------------------------
# Itineraries — keyed by trip rather than a flat list
# ---------------------------------------------------------------------------

@flyby_api.route('/itineraries', methods=['GET', 'PUT'])
@token_required()
def itineraries():
    """
    Load or save every itinerary the caller owns, keyed by trip id.

    The editor works with one itinerary at a time but stores them together, so
    the payload is a ``{tripId: itinerary}`` map.
    """
    user_id = g.user_id

    if request.method == 'GET':
        rows = Itinerary.query.filter_by(_user_id=user_id).all()
        return jsonify({"itineraries": {row._trip_id: row._data for row in rows if row._trip_id}}), 200

    body = request.get_json(silent=True) or {}
    payload = body.get("itineraries", body)
    if not isinstance(payload, dict):
        return _fail("Expected a map of trip id to itinerary.")

    try:
        existing = {row._trip_id: row for row in Itinerary.query.filter_by(_user_id=user_id).all()}
        for trip_id, data in payload.items():
            row = existing.get(trip_id)
            if row is None:
                row = Itinerary(id=new_uuid(), _user_id=user_id, _trip_id=trip_id)
                db.session.add(row)
            row._data = data
            row._updated_at = utcnow()
        for trip_id, row in existing.items():
            if trip_id not in payload:
                db.session.delete(row)
        db.session.commit()
    except Exception as err:  # noqa: BLE001
        db.session.rollback()
        return _fail(f"Could not save itineraries: {err}", 500)

    rows = Itinerary.query.filter_by(_user_id=user_id).all()
    return jsonify({"itineraries": {row._trip_id: row._data for row in rows if row._trip_id}}), 200


# ---------------------------------------------------------------------------
# Learned travel preferences
# ---------------------------------------------------------------------------

@flyby_api.route('/preferences', methods=['GET', 'PUT'])
@token_required()
def preferences():
    """
    Load or save the caller's travel preferences.

    Explicit choices map onto real columns; the behavioural counters the app
    learns from ride along in ``learning_data``.
    """
    prefs = TravelPreference.for_user(g.user_id)
    if prefs is None:
        return _fail("Could not load preferences.", 500)

    if request.method == 'PUT':
        body = request.get_json(silent=True) or {}
        data = body.get("preferences", body)
        if not isinstance(data, dict):
            return _fail("Expected a preferences object.")

        mapping = {
            "preferredAirlines": "_preferred_airlines",
            "preferredHotelBrands": "_preferred_hotel_brands",
            "preferredSeatType": "_preferred_seat",
            "preferredClass": "_preferred_class",
            "avoidsLayovers": "_avoid_layovers",
            "dietaryRestrictions": "_dietary_restrictions",
        }
        for key, attr in mapping.items():
            if key in data:
                setattr(prefs, attr, data[key])

        if "budgetPerDay" in data:
            try:
                prefs._budget_threshold_per_day = float(data["budgetPerDay"] or 0)
            except (TypeError, ValueError):
                pass
        if "costSensitive" in data:
            prefs._cost_sensitivity = "cost_sensitive" if data["costSensitive"] else "balanced"
        if "learning" in data and isinstance(data["learning"], dict):
            prefs._learning_data = data["learning"]

        prefs._updated_at = utcnow()
        if prefs.save() is None:
            return _fail("Could not save preferences.", 500)

    return jsonify({"preferences": _preferences_client(prefs)}), 200


def _preferences_client(prefs):
    """Render preferences in the shape the app's preference hook expects."""
    return {
        "prefersEarlyFlights": bool((prefs._learning_data or {}).get("prefersEarlyFlights", False)),
        "avoidsLayovers": bool(prefs._avoid_layovers),
        "costSensitive": prefs._cost_sensitivity == "cost_sensitive",
        "flexibleTraveler": bool((prefs._learning_data or {}).get("flexibleTraveler", False)),
        "preferredAirlines": prefs._preferred_airlines or [],
        "preferredHotelBrands": prefs._preferred_hotel_brands or [],
        "preferredSeatType": prefs._preferred_seat or "any",
        "preferredClass": prefs._preferred_class or "economy",
        "budgetPerDay": prefs._budget_threshold_per_day or 0,
        "dietaryRestrictions": prefs._dietary_restrictions,
        "learning": prefs._learning_data or {},
        "lastUpdated": prefs.read()["updated_at"],
    }


# ---------------------------------------------------------------------------
# Team + health
# ---------------------------------------------------------------------------

@flyby_api.route('/team', methods=['GET'])
@token_required()
def team():
    """
    List the people in the caller's workspace and where each is traveling.

    The Team view's whole purpose is "see where colleagues are traveling," so
    each member is returned with their soonest current-or-upcoming trip
    (destination + dates only). Contact details are never exposed.
    """
    from datetime import date

    profile = g.current_user.profile
    company_id = profile.company_id if profile else None
    if not company_id:
        return jsonify({"members": []}), 200

    from model.user import Profile as ProfileModel
    from model.trip import Trip

    today = date.today()
    rows = ProfileModel.query.filter_by(_company_id=company_id).all()

    members = []
    for row in rows:
        # The member's next trip that hasn't ended yet (active or upcoming).
        trip = (
            Trip.query
            .filter_by(_user_id=row.user_id)
            .filter(Trip._end_date >= today)
            .order_by(Trip._start_date.asc())
            .first()
        )
        trip_data = None
        if trip and trip._destination:
            trip_data = {
                "destination": trip._destination,
                "startDate": trip._start_date.isoformat() if trip._start_date else None,
                "endDate": trip._end_date.isoformat() if trip._end_date else None,
            }

        # Derive a team label from the job title (e.g. "Sales Director" -> "Sales").
        title = row.job_title or ""
        team_label = _team_from_title(title)

        members.append({
            "id": row.user_id,
            "name": row.full_name or row.email.split("@")[0],
            "role": row.job_title or "Team member",
            "team": team_label,
            "avatar": row.avatar_url,
            "isSelf": row.user_id == g.user_id,
            "trip": trip_data,
        })

    return jsonify({"members": members}), 200


def _team_from_title(title):
    """Map a job title to a broad team label for the Team view's grouping."""
    lowered = (title or "").lower()
    buckets = [
        ("Sales", ("sales", "account", "revenue", "business development")),
        ("Engineering", ("engineer", "developer", "devops", "sre", "technical")),
        ("Product", ("product", "pm")),
        ("Design", ("design", "ux", "ui")),
        ("Finance", ("finance", "cfo", "accounting", "controller")),
        ("Marketing", ("marketing", "growth", "brand")),
        ("Leadership", ("ceo", "coo", "cto", "founder", "chief", "vp", "head", "director")),
    ]
    for label, needles in buckets:
        if any(n in lowered for n in needles):
            return label
    return "Team"


@flyby_api.route('/health', methods=['GET'])
def health():
    """Liveness probe used by the frontend and the Makefile."""
    return jsonify({"status": "ok", "service": "flyby-api"}), 200
