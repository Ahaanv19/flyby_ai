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

# database Initialization functions
from model.user import User, Profile, initUsers
from model.company import Company, TravelPolicy, initCompanies
from model.trip import (CalendarSuggestion, Document, Itinerary, TravelAlert,
                        Trip, initTrips)
from model.expense import Expense, initExpenses
from model.preferences import (ClientCompany, LoyaltyProgram, TravelPreference,
                               initPreferences)
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
    return dict(current_user=current_user)


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
    error = None
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
    return render_template("index.html", stats=_system_stats())


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
                  TravelPreference, ActiveSession, MfaCredential, UserRole):
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
        initUsers()
        initCompanies()
        initRoles()
        initSecurity()
        initMFA()
    except Exception as e:
        print(f"Seed data init: {e}")


# this runs the flask application on the development server
if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT") or 8659)
    print(f"\n  Flyby AI backend  ->  http://localhost:{port}\n")
    app.run(debug=True, host="0.0.0.0", port=port)
