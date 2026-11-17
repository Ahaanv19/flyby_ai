"""
Flyby AI — company portal and travel policy models.

A company portal is the workspace a traveler belongs to. Someone asks for one
to be created; Flyby verifies it (in the admin console); once verified it gets
a join code, and the person who asked becomes its first company admin. Other
travelers join with the code. Company admins can see their members' trips and
expenses — which is why a portal has to be verified before it exists, and why
membership is only ever changed by the portal endpoints (``api.company_api``),
never by a client writing a ``company_id``.

Status:
    pending    asked for, waiting for Flyby to verify
    active     verified; has a join code
    rejected   Flyby declined it (``_review_note`` says why)
    suspended  was active; switched off by Flyby

The company also owns the travel policy that bookings are checked against.
"""

import secrets

from __init__ import app, db
from model.base import RowMixin, iso, new_uuid, utcnow

# Join codes: 8 characters, shown as XXXX-XXXX. No 0/O, 1/I/L — they get
# misread when a code is passed along by voice or a screenshot.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

PORTAL_STATUSES = ("pending", "active", "rejected", "suspended")
COMPANY_ROLES = ("admin", "member")


def normalize_join_code(value):
    """A typed code as stored: upper-case, without spaces or dashes."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def format_join_code(code):
    """ABCD2345 -> ABCD-2345, for display."""
    code = normalize_join_code(code)
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LENGTH else code


class Company(db.Model, RowMixin):
    """
    Company Model — a tenant/workspace in the ``companies`` table.
    """
    __tablename__ = 'companies'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _name = db.Column(db.String(255), nullable=False)
    _domain = db.Column(db.String(255), nullable=False, default="")
    _status = db.Column(db.String(30), nullable=False, default="active")
    _settings = db.Column(db.JSON, nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    # Portal fields. Deliberately NOT in COLUMNS below: that map is what the
    # generic data API reads and writes, and a join code must only ever reach a
    # company admin through the portal endpoints.
    _join_code = db.Column(db.String(16), nullable=True, index=True)
    _created_by = db.Column(db.String(36), nullable=True)   # who asked for the portal
    _reviewed_by = db.Column(db.String(36), nullable=True)  # the Flyby admin who decided
    _reviewed_at = db.Column(db.DateTime, nullable=True)
    _review_note = db.Column(db.String(500), nullable=True)

    COLUMNS = {
        "id": "id",
        "name": "_name",
        "domain": "_domain",
        "status": "_status",
        "settings": "_settings",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

    @property
    def is_active(self):
        return self._status == "active"

    def new_join_code(self):
        """Give the portal a fresh code that no other portal has. Not committed."""
        for _ in range(20):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if not Company.query.filter(Company._join_code == code).first():
                self._join_code = code
                return code
        raise RuntimeError("Could not generate a unique join code.")

    @staticmethod
    def by_join_code(value):
        """The active portal a typed code belongs to, or None."""
        code = normalize_join_code(value)
        if len(code) != CODE_LENGTH:
            return None
        company = Company.query.filter(Company._join_code == code).first()
        return company if company and company.is_active else None

    @staticmethod
    def for_domain(domain, name=None):
        """
        Find (or create) the company that owns an email domain.

        Sign-up uses this so everyone with a ``@acme.com`` work email lands in
        the same workspace automatically.
        """
        if not domain:
            return None
        domain = domain.strip().lower()
        company = Company.query.filter(db.func.lower(Company._domain) == domain).first()
        if company:
            return company
        company = Company(id=new_uuid(), _name=name or domain.split(".")[0].title(), _domain=domain)
        return company.create()


class TravelPolicy(db.Model, RowMixin):
    """
    Travel Policy Model — per-company booking limits in ``travel_policies``.

    The frontend's policy evaluator reads these to flag out-of-policy flights
    and hotels and to decide when a trip needs manager approval.
    """
    __tablename__ = 'travel_policies'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _company_id = db.Column(db.String(36), db.ForeignKey('companies.id'), nullable=False)
    _max_flight_price = db.Column(db.Float, nullable=True)
    _max_flight_class = db.Column(db.String(50), nullable=True)
    _max_nightly_hotel_rate = db.Column(db.Float, nullable=True)
    _approval_required_above = db.Column(db.Float, nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "company_id": "_company_id",
        "max_flight_price": "_max_flight_price",
        "max_flight_class": "_max_flight_class",
        "max_nightly_hotel_rate": "_max_nightly_hotel_rate",
        "approval_required_above": "_approval_required_above",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

    @staticmethod
    def for_company(company_id):
        """Return the company's policy, creating a sensible default if absent."""
        if not company_id:
            return None
        policy = TravelPolicy.query.filter_by(_company_id=company_id).first()
        if policy:
            return policy
        policy = TravelPolicy(
            id=new_uuid(),
            _company_id=company_id,
            _max_flight_price=1200.0,
            _max_flight_class="economy",
            _max_nightly_hotel_rate=350.0,
            _approval_required_above=2500.0,
        )
        return policy.create()


def ensureCompanyColumns():
    """
    Add company columns introduced after a database was first created.

    Same reason as ``model.preferences.ensurePreferenceColumns``: create_all()
    never alters an existing table. Adds what's missing and touches nothing
    else — safe on every startup, SQLite or MySQL.
    """
    from sqlalchemy import inspect, text

    added = {
        "_join_code": "VARCHAR(16)",
        "_created_by": "VARCHAR(36)",
        "_reviewed_by": "VARCHAR(36)",
        "_reviewed_at": "DATETIME",
        "_review_note": "VARCHAR(500)",
    }
    with app.app_context():
        inspector = inspect(db.engine)
        if "companies" not in inspector.get_table_names():
            return  # create_all will build it with every column
        existing = {column["name"] for column in inspector.get_columns("companies")}
        for name, sql_type in added.items():
            if name not in existing:
                with db.engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {sql_type}"))
                print(f"Added column companies.{name}")


def initCompanies():
    """Create the company tables and seed the demo workspace."""
    with app.app_context():
        db.create_all()
        if Company.query.first():
            return
        demo = Company(
            id=new_uuid(),
            _name="Flyby Demo Co.",
            _domain="flyby.ai",
            _status="active",
            _settings={"plan": "enterprise", "seats": 50},
        )
        if demo.create():
            TravelPolicy.for_company(demo.id)
            print("Seeded demo company")
