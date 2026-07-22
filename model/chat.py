"""
Flyby AI — chat models.

Conversations pair a traveler with teammates or the AI assistant, and they carry
expense context (an approval request, a dispute) alongside plain messages. The
thread is stored as an ordered message list on its conversation so a chat always
loads as one row, in order, exactly as it was written.
"""

from __init__ import app, db
from model.base import RowMixin, iso, new_uuid, parse_dt, utcnow


class Chat(db.Model, RowMixin):
    """
    Chat Model — a conversation in the ``chats`` table.
    """
    __tablename__ = 'chats'

    id = db.Column(db.String(64), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _company_id = db.Column(db.String(36), nullable=True)
    _name = db.Column(db.String(255), nullable=True)
    _kind = db.Column(db.String(30), nullable=False, default="direct")
    _trip_id = db.Column(db.String(36), nullable=True, index=True)
    _expense_id = db.Column(db.String(36), nullable=True, index=True)
    _participants = db.Column(db.JSON, nullable=True)
    _messages = db.Column(db.JSON, nullable=True)

    # Anything else the conversation UI tracks (unread counts, pinned state,
    # expense metadata) travels here so no chat detail is lost on a save.
    _client_data = db.Column(db.JSON, nullable=True)

    _last_message_at = db.Column(db.DateTime, nullable=True, index=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "company_id": "_company_id",
        "name": "_name",
        "kind": "_kind",
        "trip_id": "_trip_id",
        "expense_id": "_expense_id",
        "participants": "_participants",
        "messages": "_messages",
        "last_message_at": "_last_message_at",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

    WRITE_CASTS = {"last_message_at": parse_dt}

    def read_client(self):
        """Return the conversation in the shape the chat UI uses."""
        data = dict(self._client_data or {})
        data.update({
            "id": self.id,
            "messages": self._messages or [],
            "participants": self._participants or [],
        })
        if self._name is not None:
            data.setdefault("name", self._name)
        if self._trip_id:
            data.setdefault("tripId", self._trip_id)
        if self._expense_id:
            data.setdefault("expenseId", self._expense_id)
        return data

    def apply_client(self, data):
        """Write a conversation from the app's shape, keeping columns in sync."""
        if not isinstance(data, dict):
            return self

        merged = dict(self._client_data or {})
        merged.update(data)
        merged.pop("id", None)
        merged.pop("messages", None)
        merged.pop("participants", None)
        self._client_data = merged

        if "messages" in data and isinstance(data["messages"], list):
            self._messages = data["messages"]
            last = data["messages"][-1] if data["messages"] else None
            if isinstance(last, dict) and last.get("createdAt"):
                self._last_message_at = parse_dt(last["createdAt"]) or utcnow()
            else:
                self._last_message_at = utcnow()
        if "participants" in data and isinstance(data["participants"], list):
            self._participants = data["participants"]
        if "name" in data:
            self._name = data.get("name")
        if "kind" in data:
            self._kind = data.get("kind") or "direct"
        elif "isGroup" in data:
            self._kind = "group" if data.get("isGroup") else "direct"
        if "tripId" in data:
            self._trip_id = data.get("tripId")
        if "expenseId" in data:
            self._expense_id = data.get("expenseId")

        self._updated_at = utcnow()
        return self


class Message(db.Model, RowMixin):
    """
    Message Model — company-wide broadcast messages, in ``messages``.

    Distinct from chat threads: these are announcements scoped to a company
    rather than a conversation between people.
    """
    __tablename__ = 'messages'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _company_id = db.Column(db.String(36), nullable=False, index=True)
    _content = db.Column(db.Text, nullable=False, default="")
    _message_type = db.Column(db.String(30), nullable=True, default="text")
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "company_id": "_company_id",
        "content": "_content",
        "message_type": "_message_type",
        "created_at": "_created_at",
    }


def initChats():
    """Create the chat tables."""
    with app.app_context():
        db.create_all()
