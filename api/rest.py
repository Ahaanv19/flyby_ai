"""
Flyby AI — data API (``/rest/v1``).

One generic query layer serves every table the frontend reads, so adding a
column or a table needs no new endpoint. The frontend's query builder compiles
each chained query into a single request handled here.

Endpoints:
    POST /rest/v1/<table>/select    filtered read
    POST /rest/v1/<table>/insert    create rows
    POST /rest/v1/<table>/update    partial update of matching rows
    POST /rest/v1/<table>/upsert    insert or update on a conflict key
    POST /rest/v1/<table>/delete    delete matching rows
    POST /rest/v1/rpc/<name>        server-side function

**Access is decided here, never by the client.** Every request is scoped by the
``ACCESS`` table below: a caller only ever sees rows they own (or, for the
tables where it makes sense, rows in their own company), and a client-supplied
``user_id`` can never widen that. Admins are the only exception, and only for
the tables marked as admin-writable.
"""

from flask import Blueprint, g, jsonify, request

from __init__ import db
from api.jwt_authorize import token_required
from model.base import new_uuid, parse_date, parse_dt
from model.chat import Chat, Message
from model.company import Company, TravelPolicy
from model.expense import Expense
from model.mfa import MfaCredential, PendingVerification
from model.notification import Notification
from model.preferences import ClientCompany, LoyaltyProgram, TravelPreference
from model.roles import (Permission, RolePermission, UserRole, has_permission,
                         permissions_for_role)
from model.security import (ActiveSession, AuditLog, FileUploadPolicy,
                            SecurityAlert, TwoFactorAuditLog)
from model.trip import (CalendarSuggestion, Document, Itinerary, TravelAlert, Trip)
from model.user import Profile, User

rest_api = Blueprint('rest_api', __name__, url_prefix='/rest/v1')


# ---------------------------------------------------------------------------
# Table registry
# ---------------------------------------------------------------------------
# scope:
#   "owned"    rows carry user_id; a caller sees only their own
#   "profile"  own row is writable; company peers are readable
#   "company"  own company readable; admin-only writes
#   "roles"    own role readable; admin-only writes
#   "reference" readable by any signed-in user; admin-only writes
#   "admin"    admin only, in both directions

TABLES = {
    "profiles":               {"model": Profile, "scope": "profile"},
    "companies":              {"model": Company, "scope": "company"},
    "travel_policies":        {"model": TravelPolicy, "scope": "company", "owner": "company_id"},
    "trips":                  {"model": Trip, "scope": "owned"},
    "expenses":               {"model": Expense, "scope": "owned"},
    "itineraries":            {"model": Itinerary, "scope": "owned"},
    "travel_alerts":          {"model": TravelAlert, "scope": "owned"},
    "calendar_suggestions":   {"model": CalendarSuggestion, "scope": "owned"},
    "documents":              {"model": Document, "scope": "owned"},
    "travel_preferences":     {"model": TravelPreference, "scope": "owned"},
    "loyalty_programs":       {"model": LoyaltyProgram, "scope": "owned"},
    "client_companies":       {"model": ClientCompany, "scope": "owned"},
    "chats":                  {"model": Chat, "scope": "owned"},
    "notifications":          {"model": Notification, "scope": "owned"},
    "messages":               {"model": Message, "scope": "owned"},
    "active_sessions":        {"model": ActiveSession, "scope": "owned"},
    "audit_logs":             {"model": AuditLog, "scope": "owned"},
    "security_alerts":        {"model": SecurityAlert, "scope": "owned"},
    "two_factor_audit_log":   {"model": TwoFactorAuditLog, "scope": "owned"},
    "mfa_credentials":        {"model": MfaCredential, "scope": "owned"},
    "user_roles":             {"model": UserRole, "scope": "roles"},
    "permissions":            {"model": Permission, "scope": "reference"},
    "role_permissions":       {"model": RolePermission, "scope": "reference"},
    "file_upload_policies":   {"model": FileUploadPolicy, "scope": "reference"},
}

# Tables an admin may read across all users. Everything else stays owner-scoped
# even for admins, so routine admin use can't quietly widen into a data dump.
ADMIN_READ_ALL = {
    "profiles", "companies", "travel_policies", "trips", "expenses",
    "audit_logs", "security_alerts", "active_sessions", "user_roles",
    "two_factor_audit_log", "messages",
}


def _fail(message, status=400, code=None):
    return jsonify({
        "data": None,
        "error": {"message": message, "code": code or str(status)},
    }), status


def _table_or_none(name):
    return TABLES.get(name)


def _is_admin():
    return getattr(g, "current_user", None) is not None and g.current_user.is_admin()


def _company_id():
    """The caller's company, or None when they aren't in a workspace."""
    profile = getattr(g.current_user, "profile", None)
    return profile.company_id if profile else None


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

OPERATORS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "is", "like", "ilike", "contains"}


def _column(model, field):
    """Resolve a public field name to its SQLAlchemy column, or None."""
    attr = model.COLUMNS.get(field)
    if attr is None:
        return None
    return getattr(model, attr, None)


def _coerce(column, value):
    """
    Convert a filter value to match its column's type.

    The frontend sends timestamps as ISO strings, but date and datetime columns
    store real date objects — comparing the two as text gives wrong answers
    (``2026-07-21T10:00:00Z`` does not order against ``2026-07-21 10:00:00``).
    Converting here keeps range filters correct across SQLite and MySQL alike.
    """
    if value is None or isinstance(value, (list, tuple)):
        return value
    try:
        column_type = column.type
    except AttributeError:
        return value

    if isinstance(column_type, db.DateTime):
        return parse_dt(value) or value
    if isinstance(column_type, db.Date):
        return parse_date(value) or value
    return value


def _apply_filter(query, model, spec):
    """Apply one ``{column, op, value}`` filter, ignoring unknown columns."""
    if not isinstance(spec, dict):
        return query
    field = spec.get("column")
    op = (spec.get("op") or "eq").lower()
    value = spec.get("value")

    column = _column(model, field)
    if column is None or op not in OPERATORS:
        return query

    if op == "in":
        value = [_coerce(column, item) for item in (value if isinstance(value, list) else [value])]
    elif op not in ("is", "like", "ilike", "contains"):
        value = _coerce(column, value)

    if op == "eq":
        return query.filter(column == value)
    if op == "neq":
        return query.filter(column != value)
    if op == "gt":
        return query.filter(column > value)
    if op == "gte":
        return query.filter(column >= value)
    if op == "lt":
        return query.filter(column < value)
    if op == "lte":
        return query.filter(column <= value)
    if op == "in":
        values = value if isinstance(value, list) else [value]
        return query.filter(column.in_(values))
    if op == "is":
        if value is None:
            return query.filter(column.is_(None))
        return query.filter(column.is_(bool(value)))
    if op == "like":
        return query.filter(column.like(value))
    if op == "ilike":
        return query.filter(column.ilike(value))
    if op == "contains":
        # JSON array containment, checked in Python: the JSON columns here are
        # small preference lists, and this keeps behavior identical on SQLite
        # and MySQL rather than depending on dialect-specific JSON operators.
        return query
    return query


def _scope_query(query, model, table, config):
    """
    Restrict a read to what the caller is allowed to see.

    This is applied to every select, after the client's own filters, so no
    combination of client filters can reach another user's rows.
    """
    scope = config["scope"]
    user_id = g.user_id

    if scope == "owned":
        if _is_admin() and table in ADMIN_READ_ALL:
            return query
        column = _column(model, "user_id")
        return query.filter(column == user_id) if column is not None else query

    if scope == "profile":
        if _is_admin():
            return query
        company_id = _company_id()
        own = Profile._user_id == user_id
        if company_id:
            # Teammates are visible so the app can show names and avatars.
            return query.filter(db.or_(own, Profile._company_id == company_id))
        return query.filter(own)

    if scope == "company":
        if _is_admin():
            return query
        company_id = _company_id()
        if not company_id:
            return query.filter(db.false())
        owner_field = config.get("owner", "id")
        column = _column(model, owner_field)
        return query.filter(column == company_id) if column is not None else query

    if scope == "roles":
        if _is_admin():
            return query
        column = _column(model, "user_id")
        return query.filter(column == user_id) if column is not None else query

    # "reference" tables are readable by any signed-in user.
    return query


def _post_filter_contains(rows, model, filters):
    """Apply ``contains`` filters over JSON array columns after the SQL query."""
    contains = [f for f in filters if isinstance(f, dict) and (f.get("op") or "").lower() == "contains"]
    if not contains:
        return rows
    for spec in contains:
        attr = model.COLUMNS.get(spec.get("column"))
        if not attr:
            continue
        wanted = spec.get("value")
        wanted = wanted if isinstance(wanted, list) else [wanted]
        rows = [
            row for row in rows
            if isinstance(getattr(row, attr, None), list)
            and all(item in getattr(row, attr) for item in wanted)
        ]
    return rows


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@rest_api.route('/<table>/select', methods=['POST'])
@token_required()
def select(table):
    """Read rows from ``table``, scoped to what the caller may see."""
    config = _table_or_none(table)
    if config is None:
        return _fail(f"Unknown table '{table}'.", 404, "unknown_table")

    model = config["model"]
    body = request.get_json(silent=True) or {}
    filters = body.get("filters") or []

    query = model.query
    for spec in filters:
        query = _apply_filter(query, model, spec)
    query = _scope_query(query, model, table, config)

    for spec in (body.get("order") or []):
        column = _column(model, spec.get("column"))
        if column is None:
            continue
        query = query.order_by(column.asc() if spec.get("ascending", True) else column.desc())

    total = None
    if body.get("count"):
        total = query.count()

    offset = body.get("offset")
    limit = body.get("limit")
    if offset:
        query = query.offset(int(offset))
    # ``limit`` may legitimately be 0: a count-only request asks for the total
    # without any rows, so this checks for None rather than truthiness.
    if limit is not None:
        query = query.limit(int(limit))

    try:
        rows = query.all()
    except Exception as error:  # noqa: BLE001 - surfaced to the client as a query error
        db.session.rollback()
        return _fail(f"Query failed: {error}", 400, "query_failed")

    rows = _post_filter_contains(rows, model, filters)
    data = [row.read() for row in rows]

    # "single" expects exactly one row; "maybe" tolerates none.
    mode = body.get("single")
    if mode == "one":
        if len(data) != 1:
            return jsonify({
                "data": None,
                "error": {
                    "message": "Expected exactly one row.",
                    "code": "PGRST116",
                    "details": f"Found {len(data)} rows",
                },
            }), 200
        return jsonify({"data": data[0], "error": None, "count": total}), 200
    if mode == "maybe":
        return jsonify({"data": data[0] if data else None, "error": None, "count": total}), 200

    return jsonify({"data": data, "error": None, "count": total}), 200


@rest_api.route('/<table>/insert', methods=['POST'])
@token_required()
def insert(table):
    """Insert rows into ``table``, always owned by the caller."""
    config = _table_or_none(table)
    if config is None:
        return _fail(f"Unknown table '{table}'.", 404, "unknown_table")
    if not _can_write(table, config):
        return _fail("You do not have permission to write to this table.", 403, "forbidden")

    model = config["model"]
    body = request.get_json(silent=True) or {}
    rows = body.get("rows") or []
    if isinstance(rows, dict):
        rows = [rows]

    created = []
    try:
        for payload in rows:
            payload = dict(payload or {})
            # Ownership is assigned server-side; a client-supplied user_id is
            # ignored so rows can never be planted on another account.
            if "user_id" in model.COLUMNS:
                payload["user_id"] = g.user_id
            row = model.from_payload(payload, user_id=g.user_id)
            db.session.add(row)
            created.append(row)
        db.session.commit()
    except Exception as error:  # noqa: BLE001
        db.session.rollback()
        return _fail(f"Insert failed: {error}", 400, "insert_failed")

    return jsonify({"data": [row.read() for row in created], "error": None}), 201


@rest_api.route('/<table>/update', methods=['POST'])
@token_required()
def update(table):
    """Update the rows in ``table`` that match the given filters."""
    config = _table_or_none(table)
    if config is None:
        return _fail(f"Unknown table '{table}'.", 404, "unknown_table")
    if not _can_write(table, config):
        return _fail("You do not have permission to write to this table.", 403, "forbidden")

    model = config["model"]
    body = request.get_json(silent=True) or {}
    values = body.get("values") or {}

    query = model.query
    for spec in (body.get("filters") or []):
        query = _apply_filter(query, model, spec)
    query = _scope_write(query, model, table, config)

    try:
        rows = query.all()
        for row in rows:
            row.apply(values)
        db.session.commit()
    except Exception as error:  # noqa: BLE001
        db.session.rollback()
        return _fail(f"Update failed: {error}", 400, "update_failed")

    return jsonify({"data": [row.read() for row in rows], "error": None}), 200


@rest_api.route('/<table>/upsert', methods=['POST'])
@token_required()
def upsert(table):
    """Insert rows, updating instead when they collide on ``on_conflict``."""
    config = _table_or_none(table)
    if config is None:
        return _fail(f"Unknown table '{table}'.", 404, "unknown_table")
    if not _can_write(table, config):
        return _fail("You do not have permission to write to this table.", 403, "forbidden")

    model = config["model"]
    body = request.get_json(silent=True) or {}
    rows = body.get("rows") or []
    if isinstance(rows, dict):
        rows = [rows]
    conflict_keys = [k.strip() for k in (body.get("on_conflict") or "id").split(",") if k.strip()]

    saved = []
    try:
        for payload in rows:
            payload = dict(payload or {})
            if "user_id" in model.COLUMNS:
                payload["user_id"] = g.user_id

            query = model.query
            matched = True
            for key in conflict_keys:
                column = _column(model, key)
                if column is None or key not in payload:
                    matched = False
                    break
                query = query.filter(column == payload[key])
            existing = _scope_write(query, model, table, config).first() if matched else None

            if existing is not None:
                existing.apply(payload)
                saved.append(existing)
            else:
                row = model.from_payload(payload, user_id=g.user_id)
                db.session.add(row)
                saved.append(row)
        db.session.commit()
    except Exception as error:  # noqa: BLE001
        db.session.rollback()
        return _fail(f"Upsert failed: {error}", 400, "upsert_failed")

    return jsonify({"data": [row.read() for row in saved], "error": None}), 200


@rest_api.route('/<table>/delete', methods=['POST'])
@token_required()
def delete(table):
    """Delete the rows in ``table`` that match the given filters."""
    config = _table_or_none(table)
    if config is None:
        return _fail(f"Unknown table '{table}'.", 404, "unknown_table")
    if not _can_write(table, config):
        return _fail("You do not have permission to write to this table.", 403, "forbidden")

    model = config["model"]
    body = request.get_json(silent=True) or {}
    filters = body.get("filters") or []
    # A delete with no filters would clear the caller's entire table; require
    # an explicit filter so an incomplete query can't wipe data.
    if not filters:
        return _fail("A delete requires at least one filter.", 400, "unfiltered_delete")

    query = model.query
    for spec in filters:
        query = _apply_filter(query, model, spec)
    query = _scope_write(query, model, table, config)

    try:
        rows = query.all()
        data = [row.read() for row in rows]
        for row in rows:
            db.session.delete(row)
        db.session.commit()
    except Exception as error:  # noqa: BLE001
        db.session.rollback()
        return _fail(f"Delete failed: {error}", 400, "delete_failed")

    return jsonify({"data": data, "error": None}), 200


def _can_write(table, config):
    """Whether the caller may write to ``table`` at all."""
    scope = config["scope"]
    if scope in ("reference",):
        return _is_admin()
    if scope in ("company", "roles"):
        return _is_admin()
    return True


def _scope_write(query, model, table, config):
    """
    Restrict a write to rows the caller owns.

    Deliberately stricter than the read scope: teammates' profiles are readable
    but never writable, and admins are only unrestricted where that is the point.
    """
    scope = config["scope"]
    if scope == "profile":
        if _is_admin():
            return query
        return query.filter(Profile._user_id == g.user_id)
    if scope in ("company", "roles", "reference"):
        return query  # already admin-gated by _can_write
    column = _column(model, "user_id")
    if column is None:
        return query
    if _is_admin() and table in ADMIN_READ_ALL:
        return query
    return query.filter(column == g.user_id)


# ---------------------------------------------------------------------------
# Server-side functions
# ---------------------------------------------------------------------------

@rest_api.route('/rpc/<name>', methods=['POST'])
@token_required()
def rpc(name):
    """
    Call a named server-side function.

    Only the functions listed here are callable, and each one re-derives the
    subject from the session rather than trusting the arguments.
    """
    args = request.get_json(silent=True) or {}

    if name == "has_permission":
        # The permission check always answers for the caller: passing another
        # user's id cannot be used to probe someone else's access.
        permission = args.get("_permission") or args.get("permission")
        return jsonify({"data": has_permission(g.user_id, permission), "error": None}), 200

    if name == "get_user_permissions":
        from model.roles import UserRole as _UserRole
        role = _UserRole.role_for(g.user_id)
        return jsonify({"data": permissions_for_role(role), "error": None}), 200

    if name == "get_my_role":
        from model.roles import UserRole as _UserRole
        return jsonify({"data": _UserRole.role_for(g.user_id), "error": None}), 200

    return _fail(f"Unknown function '{name}'.", 404, "unknown_function")
