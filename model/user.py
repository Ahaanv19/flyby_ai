"""
Flyby AI — user + profile models.

This is the login system the React frontend authenticates against. It keeps the
template's original shape (a ``User`` SQLAlchemy model with private ``_``-prefixed
columns, property accessors, and ``create``/``read``/``update``/``delete``
methods, plus an ``initUsers()`` seeder) and adapts it to what the frontend
expects:

* ``_uuid`` — a stable UUID string. The frontend identifies the signed-in user
  by ``user.id``, and every owned row (trips, expenses, ...) references it via
  ``user_id``. The integer ``id`` stays as the primary key so Flask-Login and
  the admin console keep working exactly as before.
* ``_uid`` — the login handle. For Flyby this is the email address, so signing
  in with an email on the frontend and on the server console are the same act.
* ``Profile`` — the editable profile row the frontend reads and writes
  (``full_name``, ``avatar_url``, ``job_title``, ``timezone``, notification
  toggles, theme, 2FA state). One profile per user, created with the user.
"""

from datetime import datetime
import json
import os
import uuid

from flask import current_app
from flask_login import UserMixin
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from __init__ import app, db
from model.base import RowMixin


def new_uuid():
    """Return a fresh UUID4 string — the public identifier for API rows."""
    return str(uuid.uuid4())


def utcnow():
    return datetime.utcnow()


def iso(value):
    """Render a datetime as an ISO-8601 UTC string (or None)."""
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


class User(db.Model, UserMixin):
    """
    User Model

    Represents a Flyby AI account in the ``users`` table. Authentication for
    both the React frontend (JWT) and the server-rendered console (Flask-Login
    session) resolves to a row of this table.

    Attributes:
        id (Column): Integer primary key, used by Flask-Login.
        _uuid (Column): Public UUID string — what the frontend calls ``user.id``.
        _name (Column): The user's display name.
        _uid (Column): Unique login handle. For Flyby this is the email.
        _email (Column): The user's email address.
        _password (Column): Salted password hash.
        _role (Column): "Admin" or "User" — drives the admin console and the
            frontend's ``user_roles`` lookup.
        _pfp (Column): Avatar path, relative to the storage folder.
    """
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    _uuid = db.Column(db.String(36), unique=True, nullable=False, default=new_uuid)
    _name = db.Column(db.String(255), unique=False, nullable=False)
    _uid = db.Column(db.String(255), unique=True, nullable=False)
    _email = db.Column(db.String(255), unique=False, nullable=False)
    _password = db.Column(db.String(255), unique=False, nullable=False)
    _role = db.Column(db.String(20), default="User", nullable=False)
    _pfp = db.Column(db.String(255), unique=False, nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _last_login_at = db.Column(db.DateTime, nullable=True)

    # One profile per user; deleting the user clears the profile with it.
    profile = db.relationship(
        'Profile', backref='user', uselist=False,
        cascade='all, delete-orphan', lazy=True,
    )

    def __init__(self, name, uid, password="", role="User", pfp='', email=None):
        """
        Constructor, 1st step in object creation.

        Args:
            name (str): The display name of the user.
            uid (str): The login handle — the email address for Flyby accounts.
            password (str): The plaintext password; hashed before storage.
            role (str): "Admin" or "User". Defaults to "User".
            pfp (str): Avatar path. Defaults to an empty string.
            email (str): Email address. Defaults to ``uid``.
        """
        self._uuid = new_uuid()
        self._name = name
        self._uid = (uid or "").strip().lower()
        self._email = (email or uid or "").strip().lower()
        self.set_password(password)
        self._role = role
        self._pfp = pfp

    # -- Flask-Login ------------------------------------------------------

    def get_id(self):
        """Flask-Login requires the primary key as a string."""
        return str(self.id)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    # -- Identity ---------------------------------------------------------

    @property
    def uuid(self):
        """The public UUID the frontend uses as ``user.id``."""
        return self._uuid

    @property
    def email(self):
        return self._email

    @email.setter
    def email(self, email):
        self._email = (email or "").strip().lower() or "?"

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, name):
        self._name = name

    @property
    def uid(self):
        return self._uid

    @uid.setter
    def uid(self, uid):
        self._uid = (uid or "").strip().lower()

    def set_uid(self, uid):
        """Set the login handle, keeping email in sync for Flyby accounts."""
        self.uid = uid
        if "@" in (uid or ""):
            self.email = uid
        return self

    def is_uid(self, uid):
        return self._uid == (uid or "").strip().lower()

    # -- Password ---------------------------------------------------------

    @property
    def password(self):
        """Obscured hash — never expose the full value."""
        return self._password[0:10] + "..."

    def set_password(self, password):
        """Hash and store a new password, falling back to the default."""
        if not password:
            password = app.config["DEFAULT_PASSWORD"]
        self._password = generate_password_hash(password, "pbkdf2:sha256", salt_length=10)

    def is_password(self, password):
        """Return True when ``password`` matches the stored hash."""
        if not password:
            return False
        return check_password_hash(self._password, password)

    # -- Role -------------------------------------------------------------

    @property
    def role(self):
        return self._role

    @role.setter
    def role(self, role):
        self._role = role

    def is_admin(self):
        return self._role == "Admin"

    @property
    def app_role(self):
        """The frontend's role vocabulary: "admin" | "moderator" | "user"."""
        mapping = {"Admin": "admin", "Moderator": "moderator"}
        return mapping.get(self._role, "user")

    @property
    def pfp(self):
        return self._pfp

    @pfp.setter
    def pfp(self, pfp):
        self._pfp = pfp

    def __str__(self):
        return json.dumps(self.read())

    # -- CRUD -------------------------------------------------------------

    def create(self, inputs=None):
        """
        Add this user to the database, along with its profile row.

        Returns:
            User: the created user, or None if the uid is already taken.
        """
        try:
            db.session.add(self)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return None

        # Every user gets exactly one profile row; the frontend reads it on load.
        self.ensure_profile()

        if inputs:
            self.update(inputs)
        return self

    def ensure_profile(self):
        """Create the matching profile row if it does not exist yet."""
        existing = Profile.query.filter_by(_user_id=self._uuid).first()
        if existing:
            return existing
        profile = Profile(
            user_id=self._uuid,
            email=self._email,
            full_name=self._name,
        )
        try:
            db.session.add(profile)
            db.session.commit()
            return profile
        except IntegrityError:
            db.session.rollback()
            return Profile.query.filter_by(_user_id=self._uuid).first()

    def read(self):
        """Dictionary form used by the admin console and the users API."""
        profile = self.profile
        return {
            "id": self.id,
            "uuid": self._uuid,
            "uid": self.uid,
            "name": self.name,
            "email": self.email,
            "role": self._role,
            "app_role": self.app_role,
            "pfp": self._pfp,
            "avatar_url": profile.avatar_url if profile else None,
            "job_title": profile.job_title if profile else None,
            "created_at": iso(self._created_at),
            "last_login_at": iso(self._last_login_at),
        }

    def read_auth_user(self):
        """
        The shape the frontend's auth client expects for a signed-in user.

        Mirrors the session user object the frontend was written against, so
        ``user.id``, ``user.email`` and ``user.user_metadata.full_name`` all
        resolve exactly as the components already assume.
        """
        profile = self.profile
        return {
            "id": self._uuid,
            "aud": "authenticated",
            "role": "authenticated",
            "email": self._email,
            "email_confirmed_at": iso(self._created_at),
            "phone": (profile.phone if profile else None) or "",
            "created_at": iso(self._created_at),
            "updated_at": iso(self._created_at),
            "last_sign_in_at": iso(self._last_login_at),
            "app_metadata": {"provider": "email", "providers": ["email"]},
            "user_metadata": {
                "full_name": self._name,
                "email": self._email,
                "avatar_url": profile.avatar_url if profile else None,
            },
            "identities": [],
        }

    def update(self, inputs):
        """Update mutable fields from a dict; returns self, or None on conflict."""
        if not isinstance(inputs, dict):
            return self

        name = inputs.get("name", "")
        uid = inputs.get("uid", "")
        email = inputs.get("email", "")
        password = inputs.get("password", "")
        role = inputs.get("role", "")
        pfp = inputs.get("pfp", None)

        if name:
            self.name = name
            if self.profile and not self.profile.full_name:
                self.profile.full_name = name
        if uid:
            self.set_uid(uid)
        if email:
            self.email = email
        if password:
            self.set_password(password)
        if role in ("Admin", "User", "Moderator"):
            self.role = role
        if pfp is not None:
            self.pfp = pfp

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return None
        return self

    def delete(self):
        """Remove this user (and its profile) from the database."""
        try:
            db.session.delete(self)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        return None

    def touch_login(self):
        """Record a successful sign-in timestamp."""
        self._last_login_at = utcnow()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    def save_pfp(self, image_data, filename):
        """Persist an avatar image and point the user at it."""
        filename = secure_filename(filename)
        folder = os.path.join(current_app.config["UPLOAD_FOLDER"], self._uuid)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, filename), "wb") as handle:
            handle.write(image_data)
        self.pfp = f"{self._uuid}/{filename}"
        db.session.commit()
        return self.pfp

    # -- Lookups ----------------------------------------------------------

    @staticmethod
    def by_email(email):
        """Find a user by email/login handle, case-insensitively."""
        if not email:
            return None
        handle = email.strip().lower()
        return User.query.filter(db.func.lower(User._uid) == handle).first()

    @staticmethod
    def by_uuid(user_uuid):
        """Find a user by its public UUID."""
        if not user_uuid:
            return None
        return User.query.filter_by(_uuid=user_uuid).first()

    @staticmethod
    def restore(data):
        """Restore users from a backup payload, keyed by uid."""
        users = {}
        for entry in data:
            _ = entry.pop('id', None)
            uid = entry.get("uid", None)
            user = User.query.filter_by(_uid=uid).first()
            if user:
                user.update(entry)
            else:
                user = User(
                    name=entry.get("name", uid),
                    uid=uid,
                    email=entry.get("email", uid),
                )
                if entry.get("uuid"):
                    user._uuid = entry["uuid"]
                user.create()
            users[uid] = user
        return users


class Profile(db.Model, RowMixin):
    """
    Profile Model

    The editable half of an account: everything the frontend's Settings page
    and profile menu read and write. Kept in its own table (rather than on
    ``users``) so the login record stays small and auth-focused, and so the
    frontend's ``profiles`` queries map to a real table one-for-one.
    """
    __tablename__ = 'profiles'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), unique=True, nullable=False)
    _email = db.Column(db.String(255), nullable=False)
    _full_name = db.Column(db.String(255), nullable=True)
    _avatar_url = db.Column(db.String(512), nullable=True)
    _job_title = db.Column(db.String(255), nullable=True)
    _phone = db.Column(db.String(50), nullable=True)
    _timezone = db.Column(db.String(64), nullable=True)
    _company_id = db.Column(db.String(36), nullable=True)
    _theme_preference = db.Column(db.String(20), nullable=True)

    # Notification preferences (Settings → Notifications)
    _notify_trip_updates = db.Column(db.Boolean, default=True, nullable=False)
    _notify_expense_approvals = db.Column(db.Boolean, default=True, nullable=False)
    _notify_flight_disruptions = db.Column(db.Boolean, default=True, nullable=False)
    _notify_weekly_summary = db.Column(db.Boolean, default=False, nullable=False)
    _auto_match_expenses = db.Column(db.Boolean, default=True, nullable=False)

    # Two-factor state (Settings → Security)
    _two_factor_enabled = db.Column(db.Boolean, default=False, nullable=False)
    _two_factor_phone = db.Column(db.String(50), nullable=True)
    _two_factor_verified_at = db.Column(db.DateTime, nullable=True)

    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # Public field -> stored attribute, used by the data API for reads,
    # writes and filters. ``user_id`` and ``email`` are read-only here: identity
    # changes go through the auth API so the users table stays authoritative.
    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "email": "_email",
        "full_name": "_full_name",
        "avatar_url": "_avatar_url",
        "job_title": "_job_title",
        "phone": "_phone",
        "timezone": "_timezone",
        "company_id": "_company_id",
        "theme_preference": "_theme_preference",
        "notify_trip_updates": "_notify_trip_updates",
        "notify_expense_approvals": "_notify_expense_approvals",
        "notify_flight_disruptions": "_notify_flight_disruptions",
        "notify_weekly_summary": "_notify_weekly_summary",
        "auto_match_expenses": "_auto_match_expenses",
        "two_factor_enabled": "_two_factor_enabled",
        "two_factor_phone": "_two_factor_phone",
        "two_factor_verified_at": "_two_factor_verified_at",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

    READ_ONLY = ("id", "user_id", "email", "created_at",
                 "two_factor_enabled", "two_factor_phone", "two_factor_verified_at")

    def __init__(self, user_id=None, email=None, full_name=None):
        self.id = new_uuid()
        self._user_id = user_id
        self._email = email
        self._full_name = full_name
        self._timezone = "America/Los_Angeles"

    # Plain-attribute accessors so the REST layer can read/write by column name.
    @property
    def user_id(self):
        return self._user_id

    @property
    def email(self):
        return self._email

    @email.setter
    def email(self, value):
        self._email = value

    @property
    def full_name(self):
        return self._full_name

    @full_name.setter
    def full_name(self, value):
        self._full_name = value

    @property
    def avatar_url(self):
        return self._avatar_url

    @avatar_url.setter
    def avatar_url(self, value):
        self._avatar_url = value

    @property
    def job_title(self):
        return self._job_title

    @job_title.setter
    def job_title(self, value):
        self._job_title = value

    @property
    def phone(self):
        return self._phone

    @phone.setter
    def phone(self, value):
        self._phone = value

    @property
    def timezone(self):
        return self._timezone

    @timezone.setter
    def timezone(self, value):
        self._timezone = value

    @property
    def company_id(self):
        return self._company_id

    @company_id.setter
    def company_id(self, value):
        self._company_id = value

    @property
    def theme_preference(self):
        return self._theme_preference

    @theme_preference.setter
    def theme_preference(self, value):
        self._theme_preference = value

    def read(self):
        """Row shape the frontend's ``profiles`` queries expect."""
        return {
            "id": self.id,
            "user_id": self._user_id,
            "email": self._email,
            "full_name": self._full_name,
            "avatar_url": self._avatar_url,
            "job_title": self._job_title,
            "phone": self._phone,
            "timezone": self._timezone,
            "company_id": self._company_id,
            "theme_preference": self._theme_preference,
            "notify_trip_updates": self._notify_trip_updates,
            "notify_expense_approvals": self._notify_expense_approvals,
            "notify_flight_disruptions": self._notify_flight_disruptions,
            "notify_weekly_summary": self._notify_weekly_summary,
            "auto_match_expenses": self._auto_match_expenses,
            "two_factor_enabled": self._two_factor_enabled,
            "two_factor_phone": self._two_factor_phone,
            "two_factor_verified_at": iso(self._two_factor_verified_at),
            "created_at": iso(self._created_at),
            "updated_at": iso(self._updated_at),
        }

    def create(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def delete(self):
        try:
            db.session.delete(self)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        return None


def initUsers():
    """
    Create the users table and seed the local development accounts.

    Seeds an admin and a demo traveler so the frontend can be signed into
    immediately after a fresh checkout. Credentials come from the environment
    (ADMIN_USER / ADMIN_PASSWORD / DEFAULT_USER / DEFAULT_PASSWORD).
    """
    with app.app_context():
        db.create_all()

        seeds = [
            {
                "name": "Flyby Admin",
                "uid": app.config["ADMIN_USER"],
                "password": app.config["ADMIN_PASSWORD"],
                "role": "Admin",
                "job_title": "Platform Administrator",
            },
            {
                "name": "Demo Traveler",
                "uid": app.config["DEFAULT_USER"],
                "password": app.config["DEFAULT_PASSWORD"],
                "role": "User",
                "job_title": "Sales Director",
            },
        ]

        for seed in seeds:
            existing = User.by_email(seed["uid"])
            if existing:
                existing.ensure_profile()
                continue
            user = User(
                name=seed["name"],
                uid=seed["uid"],
                password=seed["password"],
                role=seed["role"],
                email=seed["uid"],
            )
            created = user.create()
            if created:
                profile = created.ensure_profile()
                if profile:
                    profile.job_title = seed["job_title"]
                    db.session.commit()
                print(f"Seeded user {seed['uid']}")
