"""
Flyby AI — two-factor authentication models.

``MfaCredential`` records an enrolled second factor (an SMS phone number today,
with room for authenticator apps and passkeys). ``PendingVerification`` holds a
short-lived SMS code plus the half-authenticated session token issued between
"password accepted" and "code accepted", so a correct password alone never
produces a usable session when 2FA is on.
"""

import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from __init__ import app, db
from model.base import RowMixin, new_uuid, parse_dt, utcnow

# How long an SMS code stays valid, and how many attempts it tolerates.
CODE_TTL_SECONDS = 10 * 60
MAX_ATTEMPTS = 5


def hash_code(code, salt):
    """Hash a verification code so the plaintext is never stored."""
    return hashlib.pbkdf2_hmac(
        "sha256", str(code).encode(), str(salt).encode(), 60_000,
    ).hex()


class MfaCredential(db.Model, RowMixin):
    """
    MFA Credential Model — an enrolled second factor, in ``mfa_credentials``.
    """
    __tablename__ = 'mfa_credentials'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _type = db.Column(db.String(30), nullable=False, default="sms")
    _device_name = db.Column(db.String(255), nullable=True)
    _is_active = db.Column(db.Boolean, default=True, nullable=False)

    # SMS factor: the destination number. Other factor types use the fields below.
    _phone_number = db.Column(db.String(50), nullable=True)
    _encrypted_secret = db.Column(db.String(255), nullable=True)
    _credential_id = db.Column(db.String(255), nullable=True)
    _public_key = db.Column(db.Text, nullable=True)
    _sign_count = db.Column(db.Integer, nullable=True, default=0)
    _backup_codes = db.Column(db.JSON, nullable=True)

    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "type": "_type",
        "device_name": "_device_name",
        "is_active": "_is_active",
        "encrypted_secret": "_encrypted_secret",
        "credential_id": "_credential_id",
        "public_key": "_public_key",
        "sign_count": "_sign_count",
        "backup_codes": "_backup_codes",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }

    @staticmethod
    def active_for(user_id):
        """Return the user's active second factor, if any."""
        if not user_id:
            return None
        return MfaCredential.query.filter_by(_user_id=user_id, _is_active=True).first()

    @staticmethod
    def generate_backup_codes(count=8):
        """Generate single-use recovery codes (returned once, stored hashed)."""
        codes = [f"{secrets.randbelow(10**8):08d}" for _ in range(count)]
        return codes, [hash_code(code, "backup") for code in codes]


class PendingVerification(db.Model, RowMixin):
    """
    Pending Verification Model — an in-flight 2FA challenge.

    Rows are short-lived: they are consumed on success, and expire on their own
    otherwise. The code is stored only as a hash, and ``_attempts`` caps guessing.
    """
    __tablename__ = 'pending_2fa_verifications'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _phone_number = db.Column(db.String(50), nullable=False, default="")
    _session_token = db.Column(db.String(128), unique=True, nullable=False)
    _code_hash = db.Column(db.String(128), nullable=False, default="")
    _attempts = db.Column(db.Integer, default=0, nullable=False)
    _expires_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "phone_number": "_phone_number",
        "session_token": "_session_token",
        "expires_at": "_expires_at",
        "created_at": "_created_at",
    }

    WRITE_CASTS = {"expires_at": parse_dt}

    @staticmethod
    def issue(user_id, phone_number):
        """
        Create a challenge and return ``(row, plaintext_code)``.

        Any earlier challenge for the user is cleared first, so only the most
        recently sent code can ever be used.
        """
        PendingVerification.query.filter_by(_user_id=user_id).delete()
        db.session.commit()

        code = f"{secrets.randbelow(10**6):06d}"
        row = PendingVerification(
            id=new_uuid(),
            _user_id=user_id,
            _phone_number=phone_number or "",
            _session_token=secrets.token_urlsafe(32),
            _code_hash=hash_code(code, user_id),
            _expires_at=utcnow() + timedelta(seconds=CODE_TTL_SECONDS),
        )
        return row.create(), code

    def is_expired(self):
        return utcnow() > self._expires_at

    def verify(self, code):
        """
        Check a submitted code.

        Returns one of ``"ok"``, ``"expired"``, ``"too_many_attempts"`` or
        ``"invalid"``. The comparison is constant-time, and every attempt is
        counted whether or not it succeeds.
        """
        if self.is_expired():
            return "expired"
        if self._attempts >= MAX_ATTEMPTS:
            return "too_many_attempts"

        self._attempts += 1
        db.session.commit()

        if hmac.compare_digest(self._code_hash, hash_code(code, self._user_id)):
            return "ok"
        return "invalid"


def initMFA():
    """Create the 2FA tables and clear any challenges left over from a restart."""
    with app.app_context():
        db.create_all()
        try:
            PendingVerification.query.filter(
                PendingVerification._expires_at < utcnow()
            ).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
