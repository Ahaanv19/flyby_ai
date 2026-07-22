"""
Flyby AI — traveler preference models.

``TravelPreference`` is the explicit "how I like to travel" record the Settings
page edits and the trip planner reads. ``LoyaltyProgram`` holds airline/hotel
membership numbers so bookings can be attributed automatically.
"""

from __init__ import app, db
from model.base import RowMixin, new_uuid, utcnow


class TravelPreference(db.Model, RowMixin):
    """
    Travel Preference Model — one row per user, in ``travel_preferences``.
    """
    __tablename__ = 'travel_preferences'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), unique=True, nullable=False, index=True)

    _preferred_airlines = db.Column(db.JSON, nullable=True)
    _preferred_hotel_brands = db.Column(db.JSON, nullable=True)
    _preferred_seat = db.Column(db.String(30), nullable=True)
    _preferred_class = db.Column(db.String(30), nullable=True)
    _avoid_layovers = db.Column(db.Boolean, default=False, nullable=False)
    _cost_sensitivity = db.Column(db.String(30), nullable=False, default="balanced")
    _budget_threshold_per_day = db.Column(db.Float, nullable=True)
    _dietary_restrictions = db.Column(db.String(512), nullable=True)

    # Behavioural signals the app learns from (early-flight bookings, premium
    # vs budget choices) plus any preference the UI adds later.
    _learning_data = db.Column(db.JSON, nullable=True)

    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "preferred_airlines": "_preferred_airlines",
        "preferred_hotel_brands": "_preferred_hotel_brands",
        "preferred_seat": "_preferred_seat",
        "preferred_class": "_preferred_class",
        "avoid_layovers": "_avoid_layovers",
        "cost_sensitivity": "_cost_sensitivity",
        "budget_threshold_per_day": "_budget_threshold_per_day",
        "dietary_restrictions": "_dietary_restrictions",
        "learning_data": "_learning_data",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

    @staticmethod
    def for_user(user_id):
        """Return the user's preferences, creating defaults on first access."""
        if not user_id:
            return None
        prefs = TravelPreference.query.filter_by(_user_id=user_id).first()
        if prefs:
            return prefs
        prefs = TravelPreference(
            id=new_uuid(),
            _user_id=user_id,
            _preferred_airlines=[],
            _preferred_hotel_brands=[],
            _preferred_seat="any",
            _preferred_class="economy",
            _cost_sensitivity="balanced",
            _budget_threshold_per_day=300.0,
        )
        return prefs.create()


class LoyaltyProgram(db.Model, RowMixin):
    """
    Loyalty Program Model — airline/hotel/rail memberships, in ``loyalty_programs``.
    """
    __tablename__ = 'loyalty_programs'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _kind = db.Column(db.String(30), nullable=False, default="airline")
    _program_name = db.Column(db.String(255), nullable=False, default="")
    _member_id = db.Column(db.String(255), nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "kind": "_kind",
        "program_name": "_program_name",
        "member_id": "_member_id",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }


class ClientCompany(db.Model, RowMixin):
    """
    Client Company Model — the customer accounts a traveler visits.

    Distinct from ``Company`` (the user's own workspace): these are the clients
    a trip can be attributed to, managed per user in ``client_companies``.
    """
    __tablename__ = 'client_companies'

    id = db.Column(db.String(64), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _name = db.Column(db.String(255), nullable=False, default="")
    _industry = db.Column(db.String(255), nullable=True)
    _notes = db.Column(db.Text, nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "name": "_name",
        "industry": "_industry",
        "notes": "_notes",
        "created_at": "_created_at",
    }

    def read_client(self):
        """Shape used by the client-company picker in the app."""
        return {
            "id": self.id,
            "name": self._name,
            "industry": self._industry,
            "notes": self._notes,
            "createdAt": self.read()["created_at"],
        }


def initPreferences():
    """Create the preference tables."""
    with app.app_context():
        db.create_all()
