"""
Flyby AI — in-app notification model.

Backs the bell menu: trip updates, expense approvals, chat mentions and system
notices, each with an optional deep link back into the app.
"""

from __init__ import app, db
from model.base import RowMixin, iso, new_uuid, parse_dt, utcnow


class Notification(db.Model, RowMixin):
    """
    Notification Model — one alert for one user, in ``notifications``.
    """
    __tablename__ = 'notifications'

    id = db.Column(db.String(64), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _type = db.Column(db.String(30), nullable=False, default="system")
    _title = db.Column(db.String(255), nullable=False, default="")
    _description = db.Column(db.Text, nullable=True)
    _read = db.Column(db.Boolean, default=False, nullable=False, index=True)
    # Deep link: {label, route, entityId}
    _action = db.Column(db.JSON, nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "type": "_type",
        "title": "_title",
        "description": "_description",
        "read": "_read",
        "action": "_action",
        "created_at": "_created_at",
    }

    def read_client(self):
        """Return the notification in the shape the bell menu uses."""
        return {
            "id": self.id,
            "type": self._type,
            "title": self._title,
            "description": self._description or "",
            "timestamp": iso(self._created_at),
            "read": bool(self._read),
            "action": self._action,
        }

    def apply_client(self, data):
        """Write a notification from the app's shape."""
        if not isinstance(data, dict):
            return self
        if "type" in data:
            self._type = data.get("type") or "system"
        if "title" in data:
            self._title = data.get("title") or ""
        if "description" in data:
            self._description = data.get("description") or ""
        if "read" in data:
            self._read = bool(data.get("read"))
        if "action" in data:
            self._action = data.get("action")
        if data.get("timestamp"):
            parsed = parse_dt(data["timestamp"])
            if parsed:
                self._created_at = parsed
        return self


def initNotifications():
    """Create the notifications table."""
    with app.app_context():
        db.create_all()
