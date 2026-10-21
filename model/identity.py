"""
Flyby AI — sign-in identities from Google and Microsoft.

One row per external account a Flyby user can sign in with. The link is made on
the provider's own stable id for the person (Google's ``sub``; Microsoft's
tenant + object id), never on the email address alone — an email can change
hands or, with some Microsoft work accounts, be set to anything by a tenant
admin.

``_created_account`` records that the Flyby account was created by this sign-in.
Such an account was given an unguessable random password nobody knows, so it
has no usable password until the person sets one; the account page uses this to
say so instead of offering a "change password" form that can't work.

New table only — ``db.create_all()`` creates it on startup. Nothing existing is
altered. No foreign-key constraint is declared, so deleting a user can never
fail on it; the console's delete removes these rows alongside the user's others.
"""

from __init__ import db
from model.base import new_uuid, utcnow

PROVIDER_NAMES = {"google": "Google", "microsoft": "Microsoft"}


class UserIdentity(db.Model):
    __tablename__ = "user_identities"

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _provider = db.Column(db.String(20), nullable=False)
    _subject = db.Column(db.String(255), nullable=False)
    _email = db.Column(db.String(255), nullable=True)
    _created_account = db.Column(db.Boolean, default=False, nullable=False)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _last_used_at = db.Column(db.DateTime, default=utcnow, nullable=True)

    __table_args__ = (db.UniqueConstraint("_provider", "_subject", name="uq_identity_provider_subject"),)

    @staticmethod
    def find(provider, subject):
        return UserIdentity.query.filter_by(_provider=provider, _subject=subject).first()

    @staticmethod
    def for_user(user_uuid):
        return UserIdentity.query.filter_by(_user_id=user_uuid).order_by(UserIdentity._created_at).all()

    @staticmethod
    def link(user_uuid, provider, subject, email=None, created_account=False):
        identity = UserIdentity(
            id=new_uuid(), _user_id=user_uuid, _provider=provider, _subject=subject,
            _email=(email or "").strip().lower() or None, _created_account=created_account,
        )
        db.session.add(identity)
        db.session.commit()
        return identity

    @staticmethod
    def password_was_never_set(user_uuid):
        """True when the account came from a provider sign-in and no password was chosen since."""
        return UserIdentity.query.filter_by(_user_id=user_uuid, _created_account=True).first() is not None

    @staticmethod
    def mark_password_set(user_uuid):
        """Call when the person chooses a password, so the account shows it has one."""
        try:
            UserIdentity.query.filter_by(_user_id=user_uuid, _created_account=True).update(
                {"_created_account": False})
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()

    def touch(self):
        self._last_used_at = utcnow()
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()

    def read(self):
        return {
            "id": self.id,
            "provider": self._provider,
            "providerName": PROVIDER_NAMES.get(self._provider, self._provider),
            "email": self._email,
            "linkedAt": self._created_at.isoformat() if self._created_at else None,
            "lastUsedAt": self._last_used_at.isoformat() if self._last_used_at else None,
        }
