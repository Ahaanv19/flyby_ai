"""
Flyby AI — backend entry point.

Serves two things:

* **The API the React frontend runs on** — authentication (``/auth/v1``), the
  data layer (``/rest/v1``), file storage (``/storage/v1``), service endpoints
  (``/functions/v1``) and the feature API (``/api``).
* **The operations console** — a server-rendered landing page and admin area for
  managing accounts, protected by Flask-Login.

Run locally:  python main.py   ->   http://localhost:8659
"""

import json
import logging
import os
import shutil
from urllib.parse import urljoin, urlparse

from flask import (abort, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from flask.cli import AppGroup
from flask import current_app
from flask_login import current_user, login_required, login_user, logout_user

# import "objects" from "this" project
from __init__ import app, db, login_manager

# Security helpers: RBAC, IP allowlist, audit logging, rate limiting.
# Imported defensively: if a security dependency is missing in some environment,
# fall back to no-op shims so the app still boots (degraded) instead of 502.
try:
    from utils.security import admin_required, ip_allowlist, audit, limiter
except Exception as _sec_err:  # noqa: BLE001
    logging.getLogger(__name__).error(
        "Security helpers unavailable (%s); running WITHOUT RBAC/rate-limit "
        "decorators. Install deps: pip install -r requirements.txt", _sec_err,
    )

    def admin_required(func):
        return func

    def ip_allowlist(*_args, **_kwargs):
        def decorator(func):
            return func
        return decorator

    def audit(*_args, **_kwargs):
        return None

    class _NoopLimiter:
        def limit(self, *_args, **_kwargs):
            def decorator(func):
                return func
            return decorator

    limiter = _NoopLimiter()

# API endpoints
from api.auth import auth_api
from api.rest import rest_api
from api.storage import storage_api
from api.functions import functions_api
from api.flyby import flyby_api
from api.duffel_api import duffel_api
from api.plaid_api import plaid_api
from api.stays_api import stays_api
from api.oauth_login import (available_providers, complete_login, is_login_state,
                             oauth_login_api)
from model.identity import UserIdentity
from model.plaid import PlaidItem, PlaidTransaction
from api.google_api import google_api
from api.outlook_api import outlook_api
from api.email_api import email_api
from api.company_api import company_api, review_portal

# database Initialization functions
from model.user import User, Profile, ensureProfileColumns, initUsers
from model.company import Company, TravelPolicy, ensureCompanyColumns, format_join_code, initCompanies
from model.trip import (CalendarSuggestion, Document, Itinerary, TravelAlert,
                        Trip, initTrips)
from model.expense import Expense, initExpenses
from model.preferences import (ClientCompany, LoyaltyProgram, TravelPreference,
                               ensurePreferenceColumns, initPreferences)
from model.roles import UserRole, Permission, RolePermission, initRoles
from model.security import (ActiveSession, AuditLog, FileUploadPolicy,
                            SecurityAlert, TwoFactorAuditLog, initSecurity)
from model.chat import Chat, Message, initChats
from model.notification import Notification, initNotifications
from model.mfa import MfaCredential, PendingVerification, initMFA

# register URIs for api endpoints
app.register_blueprint(auth_api)        # /auth/v1/*
app.register_blueprint(rest_api)        # /rest/v1/*
app.register_blueprint(storage_api)     # /storage/v1/*
app.register_blueprint(functions_api)   # /functions/v1/*
app.register_blueprint(flyby_api)       # /api/*
app.register_blueprint(duffel_api)      # /api/duffel/*  (real flight search + booking)
app.register_blueprint(plaid_api)       # /api/plaid/*   (card linking + expense capture)
app.register_blueprint(stays_api)       # /api/stays/*   (real hotel search + booking)
app.register_blueprint(oauth_login_api) # /auth/v1/authorize, identities (Google/Microsoft sign-in)
app.register_blueprint(google_api)      # /api/google/*  (Google Calendar sync)
app.register_blueprint(outlook_api)     # /api/outlook/* (Outlook / Microsoft 365 sync)
app.register_blueprint(email_api)       # /api/email/*   (email status + test message)
app.register_blueprint(company_api)     # /api/company/* (company portals: join, request, manage)


# Tell Flask-Login the view function name of your login route
login_manager.login_view = "login"


@login_manager.unauthorized_handler
def unauthorized_callback():
    return redirect(url_for('login', next=request.path))


# register URIs for server pages
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@app.context_processor
def inject_user():
    # app_url: where "Open the app" links go — the deployed app in production,
    # not a hardcoded localhost address.
    pending_portals = 0
    if getattr(current_user, "is_authenticated", False) and current_user.is_admin():
        try:
            pending_portals = Company.query.filter_by(_status="pending").count()
        except Exception:  # noqa: BLE001 — a count must never break a page
            pending_portals = 0
    return dict(
        current_user=current_user,
        app_url=(current_app.config.get('APP_URL') or 'http://localhost:8080').rstrip('/'),
        signin_providers=available_providers(),
        pending_portals=pending_portals,
    )


# Helper function to check if the URL is safe for redirects
def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"])  # throttle brute-force attempts
def login():
    """
    Sign in to the operations console.

    The same accounts work here and in the React app — one users table, one
    password — so an admin signs in with their normal Flyby email.
    """
    # In development, sign in on the address Google and Microsoft return to
    # (localhost, not 127.0.0.1). Browsers keep their cookies separately, so a
    # sign-in started on the other one could never finish.
    canonical = urlparse(app.config.get('PUBLIC_BACKEND_URL') or '').netloc
    if (request.method == 'GET' and canonical and request.host != canonical
            and not app.config.get('IS_PRODUCTION')):
        return redirect(app.config['PUBLIC_BACKEND_URL'] + request.full_path.rstrip('?'))
    # A failed Google/Microsoft sign-in comes back here with its reason.
    error = request.args.get('oauth_error') or None
    next_page = request.args.get('next', '') or request.form.get('next', '')
    if request.method == 'POST':
        username = request.form.get('username', '')
        user = User.by_email(username)
        if user and user.is_password(request.form.get('password', '')):
            login_user(user)
            user.touch_login()
            audit("login_success", username=username)
            AuditLog.record(user.uuid, "console_login", True)
            if not is_safe_url(next_page):
                return abort(400)
            return redirect(next_page or url_for('index'))
        else:
            audit("login_failure", username=username)
            error = 'Invalid email or password.'
    return render_template("login.html", error=error, next=next_page)


# ---------------------------------------------------------------------------
# One sign-in for the app and the console
# ---------------------------------------------------------------------------
# Someone already signed in to the app opens the console without typing their
# password again: the app asks for a one-time link with its normal access token,
# and the link signs that same account in here. Links last 60 seconds and work
# once.

_CONSOLE_HANDOFF_SALT = "console-handoff"
_used_console_handoffs = {}


def _console_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.config["SECRET_KEY"])


@app.route('/api/console/handoff', methods=['POST'])
@limiter.limit("20 per minute")
def console_handoff():
    """Give the signed-in app user a one-time link that signs them in to the console."""
    import secrets as _secrets
    from api.jwt_authorize import current_user_from_request
    user = current_user_from_request()
    if user is None:
        return jsonify({"message": "Sign in to the app first.", "error": "unauthorized"}), 401
    target = (request.get_json(silent=True) or {}).get("next") or ""
    if not (target.startswith("/") and not target.startswith("//")):
        target = ""
    code = _console_serializer().dumps(
        {"u": user.uuid, "j": _secrets.token_urlsafe(12), "n": target}, salt=_CONSOLE_HANDOFF_SALT)
    # The configured public address, not the request's: behind a proxy the
    # request can look like plain http or like the app's own host.
    base = app.config.get('PUBLIC_BACKEND_URL') or request.host_url.rstrip('/')
    return jsonify({"url": base + url_for('console_enter', code=code)})


@app.route('/console/enter')
def console_enter():
    """Finish the app-to-console handoff: sign the account in and open the console."""
    import time as _time
    from itsdangerous import BadSignature, SignatureExpired
    try:
        payload = _console_serializer().loads(request.args.get('code', ''), salt=_CONSOLE_HANDOFF_SALT, max_age=60)
    except SignatureExpired:
        return redirect(url_for('login', oauth_error="That link expired. Open the console from the app again, or sign in below."))
    except BadSignature:
        return redirect(url_for('login', oauth_error="That sign-in link isn't valid. Sign in below."))
    now = _time.time()
    for jti, used_at in list(_used_console_handoffs.items()):
        if now - used_at > 120:
            _used_console_handoffs.pop(jti, None)
    if payload.get("j") in _used_console_handoffs:
        return redirect(url_for('login', oauth_error="That link was already used. Sign in below."))
    _used_console_handoffs[payload.get("j")] = now
    user = User.by_uuid(payload.get("u"))
    if user is None:
        return redirect(url_for('login', oauth_error="That account no longer exists."))
    login_user(user)
    user.touch_login()
    AuditLog.record(user.uuid, "console_login", True, method="app_handoff")
    return redirect(payload.get("n") or url_for('index'))


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    """
    Your account, for every signed-in user — not just administrators.

    Shows who you are, how you can sign in (password, Google, Microsoft) with
    buttons to connect another, recent sign-ins, and your trips with the
    airline and hotel confirmations they carry. Lets you set or change your
    password, and disconnect a sign-in method as long as another remains.
    """
    import secrets as _secrets
    from flask import session as flask_session
    from model.security import ActiveSession

    if 'csrf' not in flask_session:
        flask_session['csrf'] = _secrets.token_urlsafe(24)
    notice, error = None, None
    if request.args.get('linked'):
        notice = f"{request.args.get('linked').title()} sign-in connected."

    if request.method == 'POST':
        if not _secrets.compare_digest(request.form.get('csrf', ''), flask_session.get('csrf', '')):
            abort(400)
        action = request.form.get('action')
        has_password = not UserIdentity.password_was_never_set(current_user.uuid)

        if action == 'password':
            new_password = request.form.get('new_password', '')
            if len(new_password) < 8:
                error = "Your new password must be at least 8 characters."
            elif new_password != request.form.get('confirm_password', ''):
                error = "The two passwords don't match."
            elif has_password and not current_user.is_password(request.form.get('current_password', '')):
                error = "Your current password is incorrect."
            else:
                current_user.set_password(new_password)
                db.session.commit()
                UserIdentity.mark_password_set(current_user.uuid)
                AuditLog.record(current_user.uuid, "password_changed", True, source="console")
                notice = "Password updated." if has_password else "Password set — you can now also sign in with your email."

        elif action == 'unlink':
            identity = UserIdentity.query.filter_by(id=request.form.get('identity_id', ''),
                                                    _user_id=current_user.uuid).first()
            others = [i for i in UserIdentity.for_user(current_user.uuid) if identity and i.id != identity.id]
            if identity is None:
                error = "That sign-in method wasn't found."
            elif not has_password and not others:
                # Removing it would leave no way to sign in at all.
                error = "Set a password first — otherwise you'd have no way to sign in."
            else:
                name = identity.read()['providerName']
                db.session.delete(identity)
                db.session.commit()
                AuditLog.record(current_user.uuid, "oauth_unlinked", True, provider=identity._provider)
                notice = f"{name} sign-in disconnected."

    identities = [i.read() for i in UserIdentity.for_user(current_user.uuid)]
    linked = {i['provider'] for i in identities}
    sessions = (ActiveSession.query.filter_by(_user_id=current_user.uuid)
                .order_by(ActiveSession._created_at.desc()).limit(8).all())

    trips = []
    for trip in Trip.query.filter_by(_user_id=current_user.uuid).all():
        data = dict(getattr(trip, '_client_data', None) or {})
        flight, hotel = data.get('flight') or {}, data.get('hotel') or {}
        trips.append({
            'destination': data.get('destination') or '—',
            'start': (data.get('startDate') or '')[:10],
            'end': (data.get('endDate') or '')[:10],
            'status': data.get('status') or 'draft',
            'airline': flight.get('airline'),
            'flight_ref': flight.get('bookingReference'),
            'round_trip': flight.get('tripType') == 'round_trip',
            'hotel': hotel.get('name'),
            'hotel_reserved': bool(hotel.get('reserved')),
            'hotel_ref': hotel.get('bookingReference'),
        })
    trips.sort(key=lambda t: t['start'] or '', reverse=True)

    return render_template(
        "account.html",
        csrf=flask_session['csrf'], notice=notice, error=error,
        identities=identities, linked=linked,
        has_password=not UserIdentity.password_was_never_set(current_user.uuid),
        sessions=sessions, trips=trips,
    )


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.errorhandler(404)  # catch for URL not found
def page_not_found(e):
    # API clients get JSON; browsers get the styled 404 page.
    if request.path.startswith(('/api/', '/auth/', '/rest/', '/storage/', '/functions/')):
        return jsonify({"error": "not_found", "message": "No such endpoint."}), 404
    return render_template('404.html'), 404


@app.route('/')  # connects default URL to index() function
def index():
    """The console landing page: live service status and system totals."""
    from api.duffel_stays import LAST_STATUS
    from api import mailer
    email_status = dict(mailer.LAST_STATUS, configured=mailer.is_configured(),
                        sender=app.config.get("MAIL_FROM"), host=app.config.get("SMTP_HOST"))
    return render_template("index.html", stats=_system_stats(), stays_status=dict(LAST_STATUS),
                           email_status=email_status)


def _system_stats():
    """
    Collect the counts shown on the landing page and admin console.

    Best-effort: a reporting query must never take the page down, so any
    failure degrades to zeros.
    """
    try:
        return {
            "users": User.query.count(),
            "trips": Trip.query.count(),
            "expenses": Expense.query.count(),
            "companies": Company.query.count(),
            "chats": Chat.query.count(),
            "notifications": Notification.query.count(),
            "sessions": ActiveSession.query.filter_by(_revoked=False).count(),
        }
    except Exception as error:  # noqa: BLE001
        app.logger.warning("Could not collect stats: %s", error)
        return {"users": 0, "trips": 0, "expenses": 0, "companies": 0,
                "chats": 0, "notifications": 0, "sessions": 0}


@app.route('/users/table')
@login_required
@admin_required
@ip_allowlist()
def utable():
    """
    The account management console.

    Shows every account with its role and activity, so an admin can see who is
    using the platform and adjust access without touching the database.
    """
    users = User.query.order_by(User._created_at.desc()).all()

    activity = {}
    for user in users:
        try:
            activity[user.uuid] = {
                "trips": Trip.query.filter_by(_user_id=user.uuid).count(),
                "expenses": Expense.query.filter_by(_user_id=user.uuid).count(),
                "role": UserRole.role_for(user.uuid),
                "two_factor": bool(user.profile and user.profile._two_factor_enabled),
                "company": _company_name(user),
            }
        except Exception:  # noqa: BLE001
            activity[user.uuid] = {"trips": 0, "expenses": 0, "role": "user",
                                   "two_factor": False, "company": None}

    return render_template("utable.html", user_data=users, activity=activity,
                           stats=_system_stats())


def _company_name(user):
    profile = user.profile
    if not profile or not profile.company_id:
        return None
    company = Company.query.get(profile.company_id)
    return company._name if company else None


@app.route('/users/table2')
@login_required
@admin_required
@ip_allowlist()
def u2table():
    # Consolidated into a single management console; keep the route working by
    # redirecting so old links/bookmarks don't break.
    return redirect(url_for('utable'))


# Helper function to extract uploads for a user
@app.route('/companies')
@login_required
@admin_required
@ip_allowlist()
def companies_console():
    """
    Verify company portals.

    A portal lets its admins see their members' trips and expenses, so nobody
    can create one on their own: it is asked for in the app and decided here.
    Pending requests come first.
    """
    order = {"pending": 0, "active": 1, "suspended": 2, "rejected": 3}
    rows = []
    for company in Company.query.order_by(Company._created_at.desc()).all():
        # Workspaces made automatically from email domains before portals
        # existed have no requester and no code; they aren't portals.
        if not company._created_by and not company._join_code:
            continue
        members = Profile.query.filter_by(_company_id=company.id).filter(
            Profile._company_role.in_(("admin", "member"))).all()
        requester = User.by_uuid(company._created_by) if company._created_by else None
        reviewer = User.by_uuid(company._reviewed_by) if company._reviewed_by else None
        same_name = Company.query.filter(
            Company.id != company.id, Company._status == "active",
            db.func.lower(Company._name) == (company._name or "").lower()).first()
        settings = company._settings or {}
        email_domain = ((requester.email if requester else "") or "").split("@")[-1].lower()
        rows.append({
            "company": company,
            "code": format_join_code(company._join_code) if company._join_code else None,
            "requester": requester,
            "requester_title": settings.get("requesterTitle"),
            "website": settings.get("website") or company._domain,
            "team_size": settings.get("teamSize"),
            "note": settings.get("note"),
            # The quickest real check: does the requester's email match the
            # company's website? A Gmail address asking for "acme.com" is
            # worth a second look.
            "email_matches": bool(email_domain and company._domain and
                                  (email_domain == company._domain or email_domain.endswith("." + company._domain))),
            "email_domain": email_domain,
            "admins": [m for m in members if m._company_role == "admin"],
            "member_count": len(members),
            "reviewer": reviewer,
            "duplicate_of": same_name,
        })
    rows.sort(key=lambda r: (order.get(r["company"]._status, 9), -(r["company"]._created_at.timestamp() if r["company"]._created_at else 0)))
    return render_template("companies.html", rows=rows, stats=_system_stats())


@app.route('/companies/<company_id>/<decision>', methods=['POST'])
@login_required
@admin_required
@ip_allowlist()
def companies_decide(company_id, decision):
    """Verify, reject, suspend or reactivate a portal."""
    if decision not in ("verify", "reject", "suspend", "reactivate"):
        return jsonify({'error': 'Unknown action.'}), 404
    # Only from the console page itself: a JSON body can't be sent cross-site
    # without a CORS preflight, which this route doesn't grant.
    if not request.is_json:
        return jsonify({'error': 'Expected JSON.'}), 400
    company = Company.query.get(company_id)
    if not company or (not company._created_by and not company._join_code):
        return jsonify({'error': 'No such company portal.'}), 404
    note = ((request.get_json(silent=True) or {}).get('note') or '').strip()
    problem = review_portal(company, decision, current_user.uuid, note)
    if problem:
        return jsonify({'error': problem}), 409
    audit(f"company_portal_{decision}", company_id=company.id)
    messages = {
        "verify": f"{company._name} verified — join code {format_join_code(company._join_code)}",
        "reject": f"{company._name} rejected",
        "suspend": f"{company._name} suspended",
        "reactivate": f"{company._name} reactivated",
    }
    return jsonify({'message': messages[decision]}), 200


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], filename)


@app.route('/users/delete/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
@ip_allowlist()
def delete_user(user_id):
    """Delete an account and everything it owns."""
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    if user.id == current_user.id:
        return jsonify({'error': "You can't delete your own account."}), 400

    user_uuid = user.uuid
    # Remove owned rows first so nothing is orphaned behind the account.
    for model in (Trip, Expense, Chat, Notification, Itinerary, TravelAlert,
                  CalendarSuggestion, Document, LoyaltyProgram, ClientCompany,
                  TravelPreference, ActiveSession, MfaCredential, UserRole,
                  UserIdentity, PlaidItem, PlaidTransaction):
        try:
            model.query.filter_by(_user_id=user_uuid).delete()
        except Exception:  # noqa: BLE001
            db.session.rollback()
    db.session.commit()

    user.delete()
    audit("user_deleted", target_user_id=user_id)
    AuditLog.record(current_user.uuid, "user_deleted", True, target_id=user_uuid)
    return jsonify({'message': 'User deleted successfully'}), 200


@app.route('/users/reset_password/<int:user_id>', methods=['POST'])
@login_required
@admin_required
@ip_allowlist()
def reset_password(user_id):
    """Reset an account's password back to the configured default."""
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    user.set_password(app.config['DEFAULT_PASSWORD'])
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        audit("password_reset_failed", target_user_id=user_id)
        return jsonify({'error': 'Password reset failed'}), 500

    audit("password_reset", target_user_id=user_id)
    AuditLog.record(current_user.uuid, "password_reset", True, target_id=user.uuid)
    return jsonify({'message': 'Password reset successfully'}), 200


@app.route('/users/set_role/<int:user_id>', methods=['POST'])
@login_required
@admin_required
@ip_allowlist()
def set_user_role(user_id):
    """
    Promote or demote an account between 'Admin' and 'User'.

    The change is mirrored onto the RBAC table immediately, so what the person
    can do in the app matches what the console says without a re-sync step.
    """
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json(silent=True) or {}
    role = (data.get('role') or '').strip()
    if role not in ('Admin', 'User'):
        return jsonify({'error': 'Invalid role. Must be "Admin" or "User".'}), 400
    # Guard: don't let an admin remove their own admin (avoid self lock-out).
    if user.id == current_user.id and role != 'Admin':
        return jsonify({'error': "You can't remove your own admin role."}), 400

    try:
        user.role = role
        db.session.commit()
        UserRole.sync_from_user(user)
        audit("role_changed", target_user_id=user_id, new_role=role)
        AuditLog.record(current_user.uuid, "role_changed", True,
                        target_id=user.uuid, new_role=role)
        return jsonify({'message': f'Role updated to {role}', 'role': role}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Calendar OAuth callbacks
# ---------------------------------------------------------------------------
# Google and Microsoft redirect here after the traveler consents, because the
# redirect URI has to be a fixed, registered address — it can't point at a
# static frontend route. The one-time code is handed straight back to the app,
# which exchanges it for tokens using its own authenticated session. Nothing
# sensitive is exposed: the code is single-use, short-lived, and useless without
# the client secret, which never leaves this server.

def _oauth_redirect(provider):
    from urllib.parse import urlencode
    from api.app_address import calendar_return_base
    # Back to the app address the traveler started from (see api.app_address).
    app_url = calendar_return_base(request.args.get('state'))
    error = request.args.get('error')
    code = request.args.get('code')
    params = {f'{provider}_error': error} if error else {f'{provider}_code': code or ''}
    return redirect(f"{app_url}/settings?{urlencode(params)}")


# The same two addresses also finish "Sign in with Google / Microsoft". Reusing
# them means no new redirect URLs to register; the state value says which flow
# it is (sign-in states start with "login.").

@app.route('/auth/outlook/callback')
def outlook_callback():
    """Return from Microsoft's consent screen — calendar connect or sign-in."""
    if is_login_state(request.args.get('state')):
        return complete_login()
    return _oauth_redirect('outlook')


@app.route('/auth/google/callback')
def google_callback():
    """Return from Google's consent screen — calendar connect or sign-in."""
    if is_login_state(request.args.get('state')):
        return complete_login()
    return _oauth_redirect('google')


@app.route('/health')
def health():
    """Service health, including database reachability."""
    try:
        db.session.execute(db.text("SELECT 1"))
        database = "ok"
    except Exception as error:  # noqa: BLE001
        database = f"error: {error}"
    return jsonify({
        "status": "ok" if database == "ok" else "degraded",
        "service": "flyby-backend",
        "database": database,
    }), 200 if database == "ok" else 503


# Create an AppGroup for custom commands
custom_cli = AppGroup('custom', help='Custom commands')


@custom_cli.command('generate_data')
def generate_data():
    """Create every table and seed the development data."""
    initializers = [
        ("users", initUsers),
        ("companies", initCompanies),
        ("roles", initRoles),
        ("trips", initTrips),
        ("expenses", initExpenses),
        ("preferences", initPreferences),
        ("chats", initChats),
        ("notifications", initNotifications),
        ("security", initSecurity),
        ("mfa", initMFA),
    ]
    for name, initializer in initializers:
        try:
            initializer()
        except Exception as error:  # noqa: BLE001
            print(f"Error initializing {name}: {error}")


def backup_database(db_uri, backup_uri):
    """Backup the current database."""
    if backup_uri:
        db_path = db_uri.replace('sqlite:///', 'instance/')
        backup_path = backup_uri.replace('sqlite:///', 'instance/')
        shutil.copyfile(db_path, backup_path)
        print(f"Database backed up to {backup_path}")
    else:
        print("Backup not supported for production database.")


def extract_data():
    """Read every table into a plain dict, ready to serialize."""
    data = {}
    with app.app_context():
        data['users'] = [user.read() for user in User.query.all()]
        data['profiles'] = [row.read() for row in Profile.query.all()]
        data['companies'] = [row.read() for row in Company.query.all()]
        data['trips'] = [row.read() for row in Trip.query.all()]
        data['expenses'] = [row.read() for row in Expense.query.all()]
        data['preferences'] = [row.read() for row in TravelPreference.query.all()]
        data['loyalty_programs'] = [row.read() for row in LoyaltyProgram.query.all()]
        data['chats'] = [row.read() for row in Chat.query.all()]
        data['notifications'] = [row.read() for row in Notification.query.all()]
        data['user_roles'] = [row.read() for row in UserRole.query.all()]
    return data


def save_data_to_json(data, directory='backup'):
    """Write each table to its own JSON file."""
    if not os.path.exists(directory):
        os.makedirs(directory)
    for table, records in data.items():
        with open(os.path.join(directory, f'{table}.json'), 'w') as f:
            json.dump(records, f, indent=2)
    print(f"Data backed up to {directory} directory.")


def load_data_from_json(directory='backup'):
    """Read the backup files back into a dict."""
    data = {}
    tables = ['users', 'profiles', 'companies', 'trips', 'expenses',
              'preferences', 'loyalty_programs', 'chats', 'notifications',
              'user_roles']
    for table in tables:
        try:
            with open(os.path.join(directory, f'{table}.json'), 'r') as f:
                data[table] = json.load(f)
        except FileNotFoundError:
            print(f"Warning: {table}.json not found, skipping...")
    return data


def restore_data(data):
    """Restore users (and their profiles) from a backup payload."""
    with app.app_context():
        users = User.restore(data.get('users', []))
        print(f"Restored {len(users)} users.")


@custom_cli.command('backup_data')
def backup_data():
    data = extract_data()
    save_data_to_json(data)
    backup_database(app.config['SQLALCHEMY_DATABASE_URI'], app.config['SQLALCHEMY_BACKUP_URI'])


@custom_cli.command('restore_data')
def restore_data_command():
    data = load_data_from_json()
    restore_data(data)


@custom_cli.command('make_admins')
def make_admins():
    """Make every existing account a Flyby Admin (console and app). New sign-ups stay regular users."""
    from model.roles import UserRole
    with app.app_context():
        for user in User.query.all():
            before = user.role
            if before != "Admin":
                user.role = "Admin"
                db.session.commit()
            UserRole.sync_from_user(user)
            print(f"{user.email}: {before} -> Admin")


# Register the custom command group with the Flask application
app.cli.add_command(custom_cli)


# Startup: make sure every table exists and the seed accounts are present, so a
# fresh checkout can be signed into immediately. Best-effort and idempotent —
# it never blocks startup and never overwrites existing data.
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Database table init: {e}")
    try:
        ensurePreferenceColumns()
    except Exception as e:
        print(f"Preference column init: {e}")
    try:
        ensureProfileColumns()
    except Exception as e:
        print(f"Profile column init: {e}")
    try:
        ensureCompanyColumns()
    except Exception as e:
        print(f"Company column init: {e}")
    try:
        initUsers()
        initCompanies()
        initRoles()
        initSecurity()
        initMFA()
    except Exception as e:
        print(f"Seed data init: {e}")


# this runs the flask application on the development server
if __name__ == "__main__":
    import socket

    # On bind, Werkzeug's dev server calls socket.getfqdn(host), which does a
    # reverse-DNS lookup. On guest/captive/offline networks that lookup can hang
    # for many seconds (or effectively forever), making startup look frozen —
    # the server has already printed its banner but never finishes binding.
    # A dev server has no need for the fully-qualified name, so we short-circuit
    # it to skip the network round-trip entirely and start instantly anywhere.
    socket.getfqdn = lambda name="": name or "localhost"

    host = os.environ.get("FLASK_HOST") or "0.0.0.0"
    port = int(os.environ.get("FLASK_PORT") or 8659)
    print(f"\n  Flyby AI backend  ->  http://localhost:{port}\n")
    app.run(debug=True, host=host, port=port)
