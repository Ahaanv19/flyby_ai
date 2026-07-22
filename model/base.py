"""
Flyby AI — shared model helpers.

Every Flyby table follows the same contract so the generic data API in
``api/rest.py`` can serve any of them without table-specific code:

* ``COLUMNS`` maps the public field name the frontend uses to the SQLAlchemy
  attribute that stores it (private ``_``-prefixed, matching the template's
  house style).
* ``read()`` renders a row as the JSON object the frontend expects.
* ``apply(data)`` writes a partial update, ignoring unknown/read-only fields.

Keeping this in one place is what makes the frontend's queries map onto the
database one-for-one, with no hand-written serializer per table.
"""

from datetime import date, datetime, timezone
import uuid

from sqlalchemy.exc import SQLAlchemyError

from __init__ import db


def new_uuid():
    """Return a fresh UUID4 string — the public identifier for API rows."""
    return str(uuid.uuid4())


def utcnow():
    return datetime.utcnow()


def iso(value):
    """Render a datetime/date as an ISO-8601 string (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat() + "Z"
    if isinstance(value, date):
        return value.isoformat()
    return value


def parse_dt(value):
    """Parse an ISO-8601 string into a datetime, tolerating a trailing 'Z'."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Every stored timestamp is naive UTC, so an offset-aware value is converted
    # to UTC — not to local time — before the tzinfo is dropped. Converting to
    # local time here would shift each comparison by the machine's UTC offset
    # and silently skew every date range filter.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_date(value):
    """Parse an ISO date (YYYY-MM-DD), also accepting a full timestamp."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


class RowMixin:
    """
    Shared CRUD + serialization behavior for Flyby tables.

    Subclasses declare ``COLUMNS`` (public field -> attribute name) and, when a
    field needs conversion on write, ``WRITE_CASTS`` (public field -> callable).
    Fields listed in ``READ_ONLY`` are never written by ``apply()``.
    """

    COLUMNS = {}
    WRITE_CASTS = {}
    READ_ONLY = ("id", "created_at")

    # -- serialization ----------------------------------------------------

    def read(self):
        """Render this row as the JSON object the frontend expects."""
        result = {}
        for field, attr in self.COLUMNS.items():
            result[field] = iso(getattr(self, attr, None))
        return result

    # -- writes -----------------------------------------------------------

    def apply(self, data):
        """
        Write a partial update from ``data``.

        Unknown keys and read-only fields are ignored rather than raising, so a
        frontend that sends an extra field can never break a save.
        """
        if not isinstance(data, dict):
            return self
        for field, value in data.items():
            if field in self.READ_ONLY or field not in self.COLUMNS:
                continue
            cast = self.WRITE_CASTS.get(field)
            if cast is not None:
                value = cast(value)
            setattr(self, self.COLUMNS[field], value)
        if "updated_at" in self.COLUMNS:
            setattr(self, self.COLUMNS["updated_at"], utcnow())
        return self

    # -- persistence ------------------------------------------------------

    def create(self):
        """Insert this row; returns self, or None if the insert failed."""
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except SQLAlchemyError:
            db.session.rollback()
            return None

    def save(self):
        """Commit pending changes; returns self, or None if the commit failed."""
        try:
            db.session.commit()
            return self
        except SQLAlchemyError:
            db.session.rollback()
            return None

    def delete(self):
        """Remove this row from the database."""
        try:
            db.session.delete(self)
            db.session.commit()
            return True
        except SQLAlchemyError:
            db.session.rollback()
            return False

    # -- construction -----------------------------------------------------

    @classmethod
    def from_payload(cls, data, user_id=None):
        """
        Build a new row from an API payload.

        The row is created empty and filled through ``apply()`` so the same
        validation/casting rules apply to inserts and updates alike.
        """
        row = cls()
        if getattr(row, "id", None) is None:
            row.id = data.get("id") or new_uuid()
        elif data.get("id"):
            row.id = data["id"]
        if user_id and "user_id" in cls.COLUMNS:
            setattr(row, cls.COLUMNS["user_id"], user_id)
        # ``id`` is read-only for updates but must be settable at insert time,
        # which is why it is assigned above rather than through apply().
        row.apply(data)
        return row
