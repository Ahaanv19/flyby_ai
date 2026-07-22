"""
Flyby AI — trip, itinerary, alert and document models.

``Trip`` is the centre of the product, and it carries two views of the same row:

* **Relational columns** (``destination``, ``start_date``, ``status``,
  ``total_estimated_cost``, ...) — what the data API queries and what the admin
  console reports on.
* **``_client_data``** — the full rich trip object the React app works with
  (AI reasoning, decision log, timeline, participants, selected flight/hotel).

Both views are read from and written to the same row, so there is exactly one
source of truth: updating a trip in the app updates the columns the reports use,
and vice versa.
"""

from __init__ import app, db
from model.base import RowMixin, iso, new_uuid, parse_date, parse_dt, utcnow


class Trip(db.Model, RowMixin):
    """
    Trip Model — a business trip in the ``trips`` table.
    """
    __tablename__ = 'trips'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _company_id = db.Column(db.String(36), nullable=True, index=True)

    _title = db.Column(db.String(255), nullable=False, default="")
    _destination = db.Column(db.String(255), nullable=False, default="")
    _destination_address = db.Column(db.String(512), nullable=True)
    _start_date = db.Column(db.Date, nullable=True)
    _end_date = db.Column(db.Date, nullable=True)
    _purpose = db.Column(db.String(512), nullable=True)
    _status = db.Column(db.String(40), nullable=False, default="draft", index=True)

    _flight_details = db.Column(db.JSON, nullable=True)
    _hotel_details = db.Column(db.JSON, nullable=True)
    _ground_transport = db.Column(db.JSON, nullable=True)
    _total_estimated_cost = db.Column(db.Float, nullable=True)

    _ai_generated = db.Column(db.Boolean, default=False, nullable=False)
    _calendar_event_id = db.Column(db.String(255), nullable=True)

    # The complete client-side trip object (timeline, decisions, AI reasoning,
    # participants, approval state). Stored whole so nothing the app tracks is
    # lost on a round trip through the API.
    _client_data = db.Column(db.JSON, nullable=True)

    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "company_id": "_company_id",
        "title": "_title",
        "destination": "_destination",
        "destination_address": "_destination_address",
        "start_date": "_start_date",
        "end_date": "_end_date",
        "purpose": "_purpose",
        "status": "_status",
        "flight_details": "_flight_details",
        "hotel_details": "_hotel_details",
        "ground_transport": "_ground_transport",
        "total_estimated_cost": "_total_estimated_cost",
        "ai_generated": "_ai_generated",
        "calendar_event_id": "_calendar_event_id",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

    WRITE_CASTS = {
        "start_date": parse_date,
        "end_date": parse_date,
    }

    # -- rich client view -------------------------------------------------

    def read_client(self):
        """
        Return the trip in the shape the React app uses.

        The stored blob is the base; the relational columns are layered on top
        so a trip edited through the data API (or the admin console) still comes
        back correct in the app.
        """
        data = dict(self._client_data or {})
        data.update({
            "id": self.id,
            "destination": self._destination,
            "startDate": iso(self._start_date),
            "endDate": iso(self._end_date),
            "purpose": self._purpose or "",
            "status": self._status,
            "estimatedCost": self._total_estimated_cost or 0,
            "calendarEventId": self._calendar_event_id,
            "createdAt": iso(self._created_at),
            "updatedAt": iso(self._updated_at),
        })
        # Guarantee the collection fields the app iterates over always exist.
        data.setdefault("timeline", [])
        data.setdefault("decisions", [])
        data.setdefault("participants", [])
        data.setdefault("approvalStatus", "none")
        data.setdefault("flight", None)
        data.setdefault("hotel", None)
        data.setdefault("groundTransport", None)
        data.setdefault("chatId", None)
        return data

    def apply_client(self, data):
        """
        Write a trip from the app's rich shape, keeping columns in sync.

        Everything is stored in ``_client_data``; the fields that also exist as
        real columns are mirrored across so queries and reports stay accurate.
        """
        if not isinstance(data, dict):
            return self

        merged = dict(self._client_data or {})
        merged.update(data)
        merged.pop("id", None)
        self._client_data = merged

        if "destination" in data:
            self._destination = data.get("destination") or ""
            if not self._title:
                self._title = f"Trip to {self._destination}" if self._destination else "Trip"
        if "startDate" in data:
            self._start_date = parse_date(data.get("startDate"))
        if "endDate" in data:
            self._end_date = parse_date(data.get("endDate"))
        if "purpose" in data:
            self._purpose = data.get("purpose")
        if "status" in data:
            self._status = data.get("status") or "draft"
        if "estimatedCost" in data:
            try:
                self._total_estimated_cost = float(data.get("estimatedCost") or 0)
            except (TypeError, ValueError):
                self._total_estimated_cost = 0.0
        if "calendarEventId" in data:
            self._calendar_event_id = data.get("calendarEventId")
        if "flight" in data:
            self._flight_details = data.get("flight")
        if "hotel" in data:
            self._hotel_details = data.get("hotel")
        if "groundTransport" in data:
            transport = data.get("groundTransport")
            self._ground_transport = (
                transport if isinstance(transport, (dict, list)) or transport is None
                else {"mode": transport}
            )
        if "autoGenerated" in data:
            self._ai_generated = bool(data.get("autoGenerated"))
        if "clientCompanyId" in data:
            self._company_id = data.get("clientCompanyId") or self._company_id

        self._updated_at = utcnow()
        return self


class Itinerary(db.Model, RowMixin):
    """
    Itinerary Model — the day-by-day plan for a trip, in ``itineraries``.

    The editor works with a nested day/block structure, so the plan is stored
    whole against its trip rather than shredded into rows the app would only
    ever reassemble.
    """
    __tablename__ = 'itineraries'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _trip_id = db.Column(db.String(36), nullable=False, index=True)
    _data = db.Column(db.JSON, nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "trip_id": "_trip_id",
        "data": "_data",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }


class TravelAlert(db.Model, RowMixin):
    """
    Travel Alert Model — disruption/risk notices for a trip, in ``travel_alerts``.
    """
    __tablename__ = 'travel_alerts'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _trip_id = db.Column(db.String(36), nullable=False, index=True)
    _alert_type = db.Column(db.String(50), nullable=False, default="info")
    _severity = db.Column(db.String(30), nullable=False, default="low")
    _title = db.Column(db.String(255), nullable=False, default="")
    _message = db.Column(db.Text, nullable=False, default="")
    _is_read = db.Column(db.Boolean, default=False, nullable=False)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "trip_id": "_trip_id",
        "alert_type": "_alert_type",
        "severity": "_severity",
        "title": "_title",
        "message": "_message",
        "is_read": "_is_read",
        "created_at": "_created_at",
    }


class CalendarSuggestion(db.Model, RowMixin):
    """
    Calendar Suggestion Model — calendar events the AI thinks are trips.

    Backs the "we spotted a trip on your calendar" panel in ``calendar_suggestions``.
    """
    __tablename__ = 'calendar_suggestions'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _calendar_event_id = db.Column(db.String(255), nullable=False)
    _event_title = db.Column(db.String(255), nullable=False, default="")
    _event_date = db.Column(db.Date, nullable=True)
    _detected_location = db.Column(db.String(255), nullable=True)
    _status = db.Column(db.String(30), nullable=True, default="pending")
    _suggested_trip_id = db.Column(db.String(36), nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "calendar_event_id": "_calendar_event_id",
        "event_title": "_event_title",
        "event_date": "_event_date",
        "detected_location": "_detected_location",
        "status": "_status",
        "suggested_trip_id": "_suggested_trip_id",
        "created_at": "_created_at",
    }

    WRITE_CASTS = {"event_date": parse_date}


class Document(db.Model, RowMixin):
    """
    Document Model — files attached to a trip (tickets, receipts, visas).
    """
    __tablename__ = 'documents'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _trip_id = db.Column(db.String(36), nullable=True, index=True)
    _name = db.Column(db.String(255), nullable=False, default="")
    _document_type = db.Column(db.String(50), nullable=False, default="other")
    _file_url = db.Column(db.String(512), nullable=True)
    _file_data = db.Column(db.JSON, nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "trip_id": "_trip_id",
        "name": "_name",
        "document_type": "_document_type",
        "file_url": "_file_url",
        "file_data": "_file_data",
        "created_at": "_created_at",
    }


def initTrips():
    """Create the trip-related tables."""
    with app.app_context():
        db.create_all()
