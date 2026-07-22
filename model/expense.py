"""
Flyby AI — expense model.

Like ``Trip``, an expense keeps a relational view (used by the data API,
approvals and reporting) alongside the richer object the React app works with
(merchant, payment method, dispute details, linked chat). Both are the same
row, so approving an expense in the app and reporting on it agree by construction.
"""

from __init__ import app, db
from model.base import RowMixin, iso, new_uuid, parse_date, parse_dt, utcnow


class Expense(db.Model, RowMixin):
    """
    Expense Model — a submitted spend item in the ``expenses`` table.
    """
    __tablename__ = 'expenses'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _company_id = db.Column(db.String(36), nullable=True, index=True)
    _trip_id = db.Column(db.String(36), nullable=True, index=True)

    _amount = db.Column(db.Float, nullable=False, default=0.0)
    _currency = db.Column(db.String(10), nullable=True, default="USD")
    _category = db.Column(db.String(50), nullable=False, default="other")
    _description = db.Column(db.String(512), nullable=False, default="")
    _status = db.Column(db.String(30), nullable=False, default="pending", index=True)
    _receipt_url = db.Column(db.String(512), nullable=True)

    _submitted_at = db.Column(db.DateTime, nullable=True)
    _approved_at = db.Column(db.DateTime, nullable=True)
    _approved_by = db.Column(db.String(36), nullable=True)

    # Merchant, location, payment method, dispute details, linked chat — the
    # fields the expenses UI shows that have no column of their own.
    _client_data = db.Column(db.JSON, nullable=True)

    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "company_id": "_company_id",
        "trip_id": "_trip_id",
        "amount": "_amount",
        "currency": "_currency",
        "category": "_category",
        "description": "_description",
        "status": "_status",
        "receipt_url": "_receipt_url",
        "submitted_at": "_submitted_at",
        "approved_at": "_approved_at",
        "approved_by": "_approved_by",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

    WRITE_CASTS = {
        "submitted_at": parse_dt,
        "approved_at": parse_dt,
    }

    # -- rich client view -------------------------------------------------

    def read_client(self):
        """Return the expense in the shape the React app uses."""
        data = dict(self._client_data or {})
        data.update({
            "id": self.id,
            "amount": self._amount or 0,
            "currency": self._currency or "USD",
            "category": self._category,
            "description": self._description or "",
            "status": self._status,
            "tripId": self._trip_id,
        })
        data.setdefault("merchant", self._description or "")
        data.setdefault("date", iso(self._created_at))
        data.setdefault("location", "")
        data.setdefault("paymentMethod", "")
        data.setdefault("reimbursable", True)
        if self._submitted_at and not data.get("supervisorSentAt"):
            data["supervisorSentAt"] = iso(self._submitted_at)
        return data

    def apply_client(self, data):
        """Write an expense from the app's rich shape, keeping columns in sync."""
        if not isinstance(data, dict):
            return self

        merged = dict(self._client_data or {})
        merged.update(data)
        merged.pop("id", None)
        self._client_data = merged

        if "amount" in data:
            try:
                self._amount = float(data.get("amount") or 0)
            except (TypeError, ValueError):
                self._amount = 0.0
        if "currency" in data:
            self._currency = data.get("currency") or "USD"
        if "category" in data:
            self._category = data.get("category") or "other"
        if "description" in data:
            self._description = data.get("description") or ""
        elif "merchant" in data and not self._description:
            self._description = data.get("merchant") or ""
        if "status" in data:
            self._status = data.get("status") or "pending"
        if "tripId" in data:
            self._trip_id = data.get("tripId")
        if "supervisorSentAt" in data:
            self._submitted_at = parse_dt(data.get("supervisorSentAt"))
        if data.get("status") == "approved" and not self._approved_at:
            self._approved_at = utcnow()

        self._updated_at = utcnow()
        return self


def initExpenses():
    """Create the expenses table."""
    with app.app_context():
        db.create_all()
