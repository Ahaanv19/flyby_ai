"""
Flyby AI — company portals (``/api/company``).

How a company gets onto Flyby, and who can see what once it is there:

  1. Someone asks for a portal for their company (``POST /request``). It is
     *pending* — nobody can join it and it grants nothing.
  2. Flyby verifies it in the admin console (``/companies``). Only then does it
     become *active* with a join code, and the person who asked becomes its
     first company admin.
  3. Travelers join with the code (``POST /join``) and become members.
  4. Company admins see their members' trips and expenses (``/members``),
     share or replace the code, and manage who is an admin.

Membership is changed only here. The profile's ``company_id`` and
``company_role`` are read-only to the generic data API, because they decide
whose trips and expenses a company admin can read.

"Company admin" is a role inside one company. It is not Flyby's platform admin
(``User.role == "Admin"``), which runs the console and sees every company.

    GET    /api/company                      your portal, role, and any pending request
    POST   /api/company/request              ask for a portal
    DELETE /api/company/request              withdraw a pending request
    POST   /api/company/join                 join with a code
    POST   /api/company/leave                leave your company
    GET    /api/company/members              (company admin) members, with travel and spend
    GET    /api/company/members/<id>         (company admin) one member's trips and expenses
    POST   /api/company/members/<id>/role    (company admin) make admin / member
    DELETE /api/company/members/<id>         (company admin) remove from the company
    POST   /api/company/code                 (company admin) replace the join code
"""

import re
import time
from datetime import date

from flask import Blueprint, current_app, g, jsonify, request

from __init__ import db
from api import notify
from api.jwt_authorize import token_required
from model.base import new_uuid, utcnow
from model.company import (COMPANY_ROLES, Company, TravelPolicy, format_join_code,
                           normalize_join_code)
from model.expense import Expense
from model.security import AuditLog
from model.trip import Trip
from model.user import Profile, User

company_api = Blueprint('company_api', __name__, url_prefix='/api/company')

# A join code is 8 characters from a 31-letter alphabet; ten wrong guesses an
# hour per account keeps guessing one pointless.
JOIN_ATTEMPTS_PER_HOUR = 10
_join_failures = {}

DOMAIN_PATTERN = re.compile(r"^(?=.{3,253}$)([a-z0-9-]+\.)+[a-z]{2,}$")


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "unknown")


def _fail(message, status=400, code=None):
    return jsonify({"error": message, "code": code or str(status)}), status


def _clean(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# ---------------------------------------------------------------------------
# Who is the caller in their company
# ---------------------------------------------------------------------------

def _membership(user):
    """``(profile, company, role)`` for an active portal, or ``(profile, None, None)``."""
    profile = user.ensure_profile()
    if not profile or not profile.company_id or profile._company_role not in COMPANY_ROLES:
        # No company, or a workspace from before portals existed (assigned by
        # email domain, never joined) — neither is a portal membership.
        return profile, None, None
    company = Company.query.get(profile.company_id)
    if not company or not company.is_active:
        return profile, None, None
    return profile, company, profile._company_role


def _pending_request(user_id):
    return (Company.query.filter_by(_created_by=user_id)
            .filter(Company._status.in_(("pending", "rejected")))
            .order_by(Company._created_at.desc()).first())


def _company_admin(func):
    """Only a company admin of an active portal; puts the portal on ``g``."""
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        profile, company, role = _membership(g.current_user)
        if role != "admin":
            return _fail("Only your company's admins can do that.", 403, "not_company_admin")
        g.company, g.profile = company, profile
        return func(*args, **kwargs)
    return wrapper


def _portal_json(company, role):
    members = Profile.query.filter_by(_company_id=company.id).filter(Profile._company_role.in_(COMPANY_ROLES))
    data = {
        "id": company.id,
        "name": company._name,
        "domain": company._domain or None,
        "status": company._status,
        "memberCount": members.count(),
        "verifiedAt": company._reviewed_at.isoformat() + "Z" if company._reviewed_at else None,
    }
    if role == "admin":
        # The code is how people get in, so only admins are given it.
        data["joinCode"] = format_join_code(company._join_code)
    return data


def _request_json(company):
    settings = company._settings or {}
    return {
        "id": company.id,
        "name": company._name,
        "domain": company._domain or None,
        "status": company._status,
        "requestedAt": company._created_at.isoformat() + "Z" if company._created_at else None,
        "reviewNote": company._review_note if company._status == "rejected" else None,
        "website": settings.get("website"),
        "teamSize": settings.get("teamSize"),
    }


def _set_membership(profile, company, role):
    profile.company_id = company.id if company else None
    profile._company_role = role if company else None
    profile._company_joined_at = utcnow() if company else None


# ---------------------------------------------------------------------------
# Everyone
# ---------------------------------------------------------------------------

@company_api.route('', methods=['GET'])
@token_required()
def mine():
    """The caller's portal and role, and any request they've made."""
    profile, company, role = _membership(g.current_user)
    pending = _pending_request(g.user_id)
    return jsonify({
        "portal": _portal_json(company, role) if company else None,
        "role": role,
        "request": _request_json(pending) if pending and not company else None,
    }), 200


@company_api.route('/request', methods=['POST'])
@token_required()
def request_portal():
    """
    Ask for a company portal. It does nothing until Flyby verifies it.

    One open request per person, and not while already in a portal — a
    manager of one company asking for another would have to leave first.
    """
    user = g.current_user
    _, company, _ = _membership(user)
    if company:
        return _fail(f"You're already part of {company._name}. Leave it first to set up a different company.", 409, "already_member")
    existing = _pending_request(user.uuid)
    if existing and existing._status == "pending":
        return _fail(f"You've already asked for {existing._name}. Flyby is reviewing it.", 409, "already_requested")

    data = request.get_json(silent=True) or {}
    name = _clean(data.get("name"), 120)
    website = _clean(data.get("website"), 200).lower()
    domain = re.sub(r"^https?://", "", website).split("/")[0].removeprefix("www.")
    title = _clean(data.get("jobTitle"), 120)
    note = _clean(data.get("note"), 500)
    team_size = _clean(data.get("teamSize"), 20)
    if len(name) < 2:
        return _fail("Enter your company's name.", 400, "missing_name")
    if not domain or not DOMAIN_PATTERN.match(domain):
        return _fail("Enter your company's website, like acme.com — Flyby uses it to verify the company.", 400, "missing_website")

    # A rejected request is replaced by the new one rather than piling up.
    if existing and existing._status == "rejected":
        db.session.delete(existing)

    company = Company(
        id=new_uuid(), _name=name, _domain=domain, _status="pending", _created_by=user.uuid,
        _settings={"website": website, "teamSize": team_size or None, "note": note or None,
                   "requesterTitle": title or None, "requesterEmail": user.email},
    )
    try:
        db.session.add(company)
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return _fail("Couldn't save the request. Please try again.", 500, "save_failed")

    AuditLog.record(user.uuid, "company_portal_requested", True, ip_address=_client_ip(),
                    target_type="company", target_id=company.id, name=name, domain=domain)
    notify.portal_requested(user, company)
    return jsonify({"request": _request_json(company)}), 201


@company_api.route('/request', methods=['DELETE'])
@token_required()
def withdraw_request():
    """Withdraw a request that hasn't been decided yet."""
    pending = _pending_request(g.user_id)
    if not pending:
        return _fail("There's no request to withdraw.", 404, "no_request")
    try:
        db.session.delete(pending)
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return _fail("Couldn't withdraw it. Please try again.", 500, "save_failed")
    AuditLog.record(g.user_id, "company_portal_withdrawn", True, target_type="company", target_id=pending.id)
    return jsonify({"request": None}), 200


@company_api.route('/join', methods=['POST'])
@token_required()
def join():
    """Join an active portal with its code."""
    user = g.current_user
    now = time.time()
    recent = [t for t in _join_failures.get(user.uuid, []) if now - t < 3600]
    _join_failures[user.uuid] = recent
    if len(recent) >= JOIN_ATTEMPTS_PER_HOUR:
        return _fail("Too many wrong codes. Try again in an hour, or ask your company admin to check the code.", 429, "too_many_attempts")

    profile, current, _ = _membership(user)
    if current:
        return _fail(f"You're already part of {current._name}. Leave it first to join another company.", 409, "already_member")
    pending = _pending_request(user.uuid)
    if pending and pending._status == "pending":
        return _fail(f"You've asked to set up {pending._name}. Withdraw that request to join a different company.", 409, "has_request")

    code = (request.get_json(silent=True) or {}).get("code")
    company = Company.by_join_code(code)
    if not company:
        recent.append(now)
        AuditLog.record(user.uuid, "company_join_failed", False, ip_address=_client_ip(),
                        code_tried=normalize_join_code(code)[:4] + "…")
        return _fail("That code doesn't match an active company. Check it with your company admin.", 404, "bad_code")

    _set_membership(profile, company, "member")
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return _fail("Couldn't join. Please try again.", 500, "save_failed")
    TravelPolicy.for_company(company.id)
    AuditLog.record(user.uuid, "company_joined", True, ip_address=_client_ip(),
                    target_type="company", target_id=company.id)
    notify.member_joined(company, user)
    return jsonify({"portal": _portal_json(company, "member"), "role": "member"}), 200


@company_api.route('/leave', methods=['POST'])
@token_required()
def leave():
    """Leave your company. Its last admin can't leave while others are still in it."""
    profile, company, role = _membership(g.current_user)
    if not company:
        return _fail("You're not part of a company.", 404, "not_member")
    if role == "admin":
        admins = Profile.query.filter_by(_company_id=company.id, _company_role="admin").count()
        others = Profile.query.filter_by(_company_id=company.id).filter(
            Profile._company_role.in_(COMPANY_ROLES), Profile._user_id != g.user_id).count()
        if admins <= 1 and others > 0:
            return _fail("You're the only admin. Make someone else an admin before you leave.", 409, "last_admin")
    _set_membership(profile, None, None)
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return _fail("Couldn't leave. Please try again.", 500, "save_failed")
    AuditLog.record(g.user_id, "company_left", True, target_type="company", target_id=company.id)
    return jsonify({"portal": None, "role": None}), 200


# ---------------------------------------------------------------------------
# Company admins
# ---------------------------------------------------------------------------

def _trip_summary(trip):
    data = trip.read_client()
    flight = data.get("flight") or {}
    hotel = data.get("hotel") or {}
    return {
        "id": data.get("id"),
        "destination": data.get("destination"),
        "startDate": data.get("startDate"),
        "endDate": data.get("endDate"),
        "status": data.get("status"),
        "purpose": data.get("purpose"),
        "estimatedCost": data.get("estimatedCost"),
        "flight": {
            "airline": flight.get("airline"),
            "flightNumber": flight.get("flightNumber"),
            "bookingReference": flight.get("bookingReference"),
            "bookedVia": flight.get("bookedVia"),
            "price": flight.get("price"),
        } if flight else None,
        "hotel": {"name": hotel.get("name"), "reserved": hotel.get("reserved")} if hotel and hotel.get("name") else None,
    }


def _expense_summary(expense):
    data = expense.read_client()
    return {
        "id": data.get("id"),
        "merchant": data.get("merchant"),
        "description": data.get("description"),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
        "category": data.get("category"),
        "status": data.get("status"),
        "date": data.get("date"),
        "tripId": data.get("tripId"),
        "tripName": data.get("tripName"),
        "reimbursable": data.get("reimbursable"),
        "paymentMethod": data.get("paymentMethod"),
    }


def _real_trips(user_id):
    """
    A member's own trips, without the sample trips the app adds to a new
    account (ids starting "demo_"). A manager must never see an example trip
    presented as somewhere their employee is going.
    """
    return [t for t in Trip.query.filter_by(_user_id=user_id).all() if not (t.client_id or "").startswith("demo_")]


def _members_of(company):
    return (Profile.query.filter_by(_company_id=company.id)
            .filter(Profile._company_role.in_(COMPANY_ROLES)).all())


@company_api.route('/members', methods=['GET'])
@token_required()
@_company_admin
def members():
    """Everyone in the company, with their next trip and what they've spent."""
    today = date.today()
    rows = []
    for profile in _members_of(g.company):
        trips = _real_trips(profile.user_id)
        expenses = Expense.query.filter_by(_user_id=profile.user_id).all()
        upcoming = sorted(
            (t for t in trips if t._end_date and t._end_date >= today and t._status not in ("cancelled", "archived")),
            key=lambda t: t._start_date or today)
        rows.append({
            "id": profile.user_id,
            "name": profile.full_name or (profile.email or "").split("@")[0],
            "email": profile.email,
            "jobTitle": profile.job_title,
            "role": profile._company_role,
            "joinedAt": profile._company_joined_at.isoformat() + "Z" if profile._company_joined_at else None,
            "isSelf": profile.user_id == g.user_id,
            "tripCount": len(trips),
            "upcomingTrip": _trip_summary(upcoming[0]) if upcoming else None,
            "travelingNow": bool(upcoming and upcoming[0]._start_date and upcoming[0]._start_date <= today),
            "spend": round(sum((e._amount or 0) for e in expenses), 2),
            "pendingExpenses": sum(1 for e in expenses if (e._status or "pending") == "pending"),
            "expenseCount": len(expenses),
        })
    rows.sort(key=lambda r: (r["role"] != "admin", (r["name"] or "").lower()))
    AuditLog.record(g.user_id, "company_members_viewed", True, target_type="company", target_id=g.company.id)
    return jsonify({"portal": _portal_json(g.company, "admin"), "members": rows}), 200


def _member_or_404(user_id):
    profile = Profile.query.filter_by(_user_id=user_id, _company_id=g.company.id).first()
    if not profile or profile._company_role not in COMPANY_ROLES:
        return None
    return profile


@company_api.route('/members/<user_id>', methods=['GET'])
@token_required()
@_company_admin
def member_detail(user_id):
    """One member's trips and expenses."""
    profile = _member_or_404(user_id)
    if not profile:
        return _fail("That person isn't in your company.", 404, "not_member")
    trips = sorted(_real_trips(profile.user_id), key=lambda t: t._start_date or date.min, reverse=True)
    expenses = Expense.query.filter_by(_user_id=profile.user_id).order_by(Expense._created_at.desc()).all()
    AuditLog.record(g.user_id, "company_member_viewed", True, target_type="user", target_id=profile.user_id,
                    company_id=g.company.id)
    return jsonify({
        "member": {"id": profile.user_id, "name": profile.full_name or profile.email, "email": profile.email,
                   "jobTitle": profile.job_title, "role": profile._company_role},
        "trips": [_trip_summary(t) for t in trips],
        "expenses": [_expense_summary(e) for e in expenses],
    }), 200


@company_api.route('/members/<user_id>/role', methods=['POST'])
@token_required()
@_company_admin
def member_role(user_id):
    """Make a member an admin, or an admin a member. There is always at least one admin."""
    role = ((request.get_json(silent=True) or {}).get("role") or "").strip().lower()
    if role not in COMPANY_ROLES:
        return _fail('Role must be "admin" or "member".', 400, "bad_role")
    profile = _member_or_404(user_id)
    if not profile:
        return _fail("That person isn't in your company.", 404, "not_member")
    if role == "member" and profile._company_role == "admin":
        admins = Profile.query.filter_by(_company_id=g.company.id, _company_role="admin").count()
        if admins <= 1:
            return _fail("A company needs at least one admin. Make someone else an admin first.", 409, "last_admin")
    profile._company_role = role
    db.session.commit()
    AuditLog.record(g.user_id, "company_role_changed", True, target_type="user", target_id=profile.user_id,
                    company_id=g.company.id, role=role)
    return jsonify({"id": profile.user_id, "role": role}), 200


@company_api.route('/members/<user_id>', methods=['DELETE'])
@token_required()
@_company_admin
def remove_member(user_id):
    """Take someone out of the company. To leave yourself, use /leave."""
    if user_id == g.user_id:
        return _fail("Use Leave company to remove yourself.", 400, "self")
    profile = _member_or_404(user_id)
    if not profile:
        return _fail("That person isn't in your company.", 404, "not_member")
    _set_membership(profile, None, None)
    db.session.commit()
    AuditLog.record(g.user_id, "company_member_removed", True, target_type="user", target_id=user_id,
                    company_id=g.company.id)
    return jsonify({"removed": user_id}), 200


@company_api.route('/code', methods=['POST'])
@token_required()
@_company_admin
def rotate_code():
    """Replace the join code. The old one stops working at once; members already in stay in."""
    g.company.new_join_code()
    db.session.commit()
    AuditLog.record(g.user_id, "company_code_rotated", True, target_type="company", target_id=g.company.id)
    return jsonify({"portal": _portal_json(g.company, "admin")}), 200


# ---------------------------------------------------------------------------
# Flyby's review (called from the admin console, never from the app)
# ---------------------------------------------------------------------------

def review_portal(company, decision, reviewer_uuid, note=None):
    """
    Verify, reject, suspend or reactivate a portal. Returns an error message or None.

    Verifying gives the portal a join code and makes the person who asked for it
    its first company admin.
    """
    if decision == "verify":
        if company._status not in ("pending", "rejected"):
            return "Only a pending request can be verified."
        company._status = "active"
        if not company._join_code:
            company.new_join_code()
        requester = User.by_uuid(company._created_by) if company._created_by else None
        if requester:
            profile = requester.ensure_profile()
            _, current, _ = _membership(requester)
            if current and current.id != company.id:
                return (f"{requester.email} has since joined {current._name}. "
                        "They need to leave it before this portal can make them its admin.")
            _set_membership(profile, company, "admin")
    elif decision == "reject":
        if company._status != "pending":
            return "Only a pending request can be rejected."
        if not (note or "").strip():
            return "Say why it was rejected — the person who asked is told."
        company._status = "rejected"
    elif decision == "suspend":
        if company._status != "active":
            return "Only an active portal can be suspended."
        company._status = "suspended"
    elif decision == "reactivate":
        if company._status != "suspended":
            return "Only a suspended portal can be reactivated."
        company._status = "active"
    else:
        return "Unknown decision."

    company._reviewed_by = reviewer_uuid
    company._reviewed_at = utcnow()
    company._review_note = (note or "").strip()[:500] or None
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return "Couldn't save the decision."

    if decision == "verify":
        TravelPolicy.for_company(company.id)
    AuditLog.record(reviewer_uuid, f"company_portal_{decision}", True, target_type="company",
                    target_id=company.id, name=company._name)
    requester = User.by_uuid(company._created_by) if company._created_by else None
    if requester and decision in ("verify", "reject"):
        notify.portal_decided(requester, company, decision)
    return None
