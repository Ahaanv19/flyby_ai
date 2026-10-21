"""
Flyby AI — connected cards (Plaid) and the charges imported from them.

``PlaidItem``        one linked bank/card login per row, owned by a user. Holds
                     Plaid's access token and the sync cursor — server-side only.
``PlaidTransaction`` one imported card charge per row, keyed by Plaid's
                     transaction id, already shaped as a Flyby expense.

Why a charge is stored here before it reaches the expense list:

The expense list is saved by the app. If the app pulled charges and then failed
to save them (closed tab, lost connection), Plaid's cursor would already have
moved on and those charges would never be offered again. So charges land here
first, with ``_delivered`` false, and are only marked delivered once the app
confirms it filed them. Anything not yet confirmed is offered again next time —
nothing is lost, and because each row is keyed by Plaid's transaction id,
nothing is imported twice, whichever device asks.

New tables only — ``db.create_all()`` creates them at startup; nothing existing
is altered. No foreign keys are declared, so deleting a user can't fail on them;
the console's delete removes these rows with the user's others.
"""

from __init__ import db
from model.base import new_uuid, utcnow


class PlaidItem(db.Model):
    __tablename__ = "plaid_items"

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _item_id = db.Column(db.String(128), nullable=False, unique=True)
    # Plaid access token: a standing key to read this account's transactions.
    # Never sent to the browser. (Encrypting it at rest needs the
    # `cryptography` package, which isn't installed yet.)
    _access_token = db.Column(db.String(255), nullable=False)
    _cursor = db.Column(db.Text, nullable=True)
    _institution = db.Column(db.String(255), nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _last_synced_at = db.Column(db.DateTime, nullable=True)
    _last_error = db.Column(db.String(255), nullable=True)

    @staticmethod
    def for_user(user_uuid):
        return PlaidItem.query.filter_by(_user_id=user_uuid).order_by(PlaidItem._created_at).all()

    def read(self):
        return {
            "id": self.id,
            "institution": self._institution or "Connected card",
            "linkedAt": self._created_at.isoformat() if self._created_at else None,
            "lastSyncedAt": self._last_synced_at.isoformat() if self._last_synced_at else None,
            "lastError": self._last_error,
        }


class PlaidTransaction(db.Model):
    __tablename__ = "plaid_transactions"

    id = db.Column(db.String(128), primary_key=True)          # Plaid transaction id
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _item_id = db.Column(db.String(128), nullable=False, index=True)
    _expense = db.Column(db.JSON, nullable=False)              # the expense it becomes
    _delivered = db.Column(db.Boolean, default=False, nullable=False, index=True)
    # Set when Plaid withdraws a charge after it was already filed (e.g. it was
    # reversed), so the app can remove the expense if it hasn't been submitted.
    _removed = db.Column(db.Boolean, default=False, nullable=False)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
