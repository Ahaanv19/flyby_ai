"""
Flyby AI — company (workspace) and travel policy models.

A company is the tenant a user belongs to. The frontend uses it for the
workspace switcher, for scoping trips/expenses, and as the owner of the travel
policy that the policy evaluator checks bookings against.
"""

from __init__ import app, db
from model.base import RowMixin, iso, new_uuid, utcnow


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

    COLUMNS = {
        "id": "id",
        "name": "_name",
        "domain": "_domain",
        "status": "_status",
        "settings": "_settings",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

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
