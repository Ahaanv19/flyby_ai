"""
Flyby AI — security models: audit trail, sessions and alerts.

These back the Security tab in Settings and the admin security console: who did
what (``audit_logs``), where they are signed in (``active_sessions``), and what
needs attention (``security_alerts``).
"""

from __init__ import app, db
from model.base import RowMixin, new_uuid, parse_dt, utcnow


class AuditLog(db.Model, RowMixin):
    """
    Audit Log Model — an immutable record of a security-relevant action.
    """
    __tablename__ = 'audit_logs'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _tenant_id = db.Column(db.String(36), nullable=True, index=True)
    _action = db.Column(db.String(100), nullable=False, index=True)
    _target_type = db.Column(db.String(50), nullable=True)
    _target_id = db.Column(db.String(64), nullable=True)
    _success = db.Column(db.Boolean, default=True, nullable=False)
    _ip_address = db.Column(db.String(64), nullable=True)
    _user_agent = db.Column(db.String(512), nullable=True)
    _metadata = db.Column(db.JSON, nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "tenant_id": "_tenant_id",
        "action": "_action",
        "target_type": "_target_type",
        "target_id": "_target_id",
        "success": "_success",
        "ip_address": "_ip_address",
        "user_agent": "_user_agent",
        "metadata": "_metadata",
        "created_at": "_created_at",
    }

    @staticmethod
    def record(user_id, action, success=True, **details):
        """Write an audit entry. Never raises — logging must not break a request."""
        try:
            entry = AuditLog(
                id=new_uuid(),
                _user_id=user_id or "system",
                _action=action,
                _success=bool(success),
                _tenant_id=details.pop("tenant_id", None),
                _target_type=details.pop("target_type", None),
                _target_id=details.pop("target_id", None),
                _ip_address=details.pop("ip_address", None),
                _user_agent=details.pop("user_agent", None),
                _metadata=details or None,
            )
            db.session.add(entry)
            db.session.commit()
            return entry
        except Exception:
            db.session.rollback()
            return None


class ActiveSession(db.Model, RowMixin):
    """
    Active Session Model — a signed-in device, in ``active_sessions``.

    Only a hash of the session token is stored, so the table can list and revoke
    sessions without ever holding a credential that could be replayed.
    """
    __tablename__ = 'active_sessions'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _session_token_hash = db.Column(db.String(128), nullable=False)
    _device_info = db.Column(db.String(512), nullable=True)
    _browser = db.Column(db.String(64), nullable=True)
    _ip_address = db.Column(db.String(64), nullable=True)
    _revoked = db.Column(db.Boolean, default=False, nullable=False, index=True)
    _revoked_at = db.Column(db.DateTime, nullable=True)
    _last_active_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "session_token_hash": "_session_token_hash",
        "device_info": "_device_info",
        "browser": "_browser",
        "ip_address": "_ip_address",
        "revoked": "_revoked",
        "revoked_at": "_revoked_at",
        "last_active_at": "_last_active_at",
        "created_at": "_created_at",
    }

    WRITE_CASTS = {
        "revoked_at": parse_dt,
        "last_active_at": parse_dt,
    }


class SecurityAlert(db.Model, RowMixin):
    """
    Security Alert Model — something that needs a human look, in ``security_alerts``.
    """
    __tablename__ = 'security_alerts'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=True, index=True)
    _tenant_id = db.Column(db.String(36), nullable=True, index=True)
    _alert_type = db.Column(db.String(50), nullable=False, default="anomaly")
    _severity = db.Column(db.String(30), nullable=False, default="low", index=True)
    _status = db.Column(db.String(30), nullable=False, default="open", index=True)
    _title = db.Column(db.String(255), nullable=False, default="")
    _description = db.Column(db.Text, nullable=True)
    _metadata = db.Column(db.JSON, nullable=True)
    _resolved_at = db.Column(db.DateTime, nullable=True)
    _resolved_by = db.Column(db.String(36), nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "tenant_id": "_tenant_id",
        "alert_type": "_alert_type",
        "severity": "_severity",
        "status": "_status",
        "title": "_title",
        "description": "_description",
        "metadata": "_metadata",
        "resolved_at": "_resolved_at",
        "resolved_by": "_resolved_by",
        "created_at": "_created_at",
    }

    WRITE_CASTS = {"resolved_at": parse_dt}


class TwoFactorAuditLog(db.Model, RowMixin):
    """
    Two-Factor Audit Log Model — every 2FA send/verify attempt.

    Phone numbers are stored masked so the trail is useful for support without
    holding the full number a second time.
    """
    __tablename__ = 'two_factor_audit_log'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), nullable=False, index=True)
    _action = db.Column(db.String(50), nullable=False)
    _success = db.Column(db.Boolean, default=True, nullable=False)
    _phone_number_masked = db.Column(db.String(50), nullable=True)
    _error_message = db.Column(db.String(255), nullable=True)
    _ip_address = db.Column(db.String(64), nullable=True)
    _user_agent = db.Column(db.String(512), nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "action": "_action",
        "success": "_success",
        "phone_number_masked": "_phone_number_masked",
        "error_message": "_error_message",
        "ip_address": "_ip_address",
        "user_agent": "_user_agent",
        "created_at": "_created_at",
    }

    @staticmethod
    def record(user_id, action, success=True, phone=None, error=None, ip=None, agent=None):
        """Write a 2FA audit entry with the phone number masked."""
        try:
            entry = TwoFactorAuditLog(
                id=new_uuid(),
                _user_id=user_id or "unknown",
                _action=action,
                _success=bool(success),
                _phone_number_masked=mask_phone(phone),
                _error_message=error,
                _ip_address=ip,
                _user_agent=agent,
            )
            db.session.add(entry)
            db.session.commit()
            return entry
        except Exception:
            db.session.rollback()
            return None


class FileUploadPolicy(db.Model, RowMixin):
    """
    File Upload Policy Model — per-tenant upload rules the frontend enforces.
    """
    __tablename__ = 'file_upload_policies'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _tenant_id = db.Column(db.String(36), nullable=True, index=True)
    _allowed_types = db.Column(db.JSON, nullable=True)
    _max_file_size_mb = db.Column(db.Integer, nullable=False, default=10)
    _require_scan = db.Column(db.Boolean, default=False, nullable=False)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    _updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "tenant_id": "_tenant_id",
        "allowed_types": "_allowed_types",
        "max_file_size_mb": "_max_file_size_mb",
        "require_scan": "_require_scan",
        "created_at": "_created_at",
        "updated_at": "_updated_at",
    }


def mask_phone(phone):
    """Return a phone number with all but the last two digits masked."""
    if not phone:
        return None
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) <= 2:
        return "*" * len(digits)
    return "*" * (len(digits) - 2) + digits[-2:]


def initSecurity():
    """Create the security tables and seed the default upload policy."""
    with app.app_context():
        db.create_all()
        if FileUploadPolicy.query.first() is None:
            FileUploadPolicy(
                id=new_uuid(),
                _allowed_types=["image/png", "image/jpeg", "image/webp", "application/pdf"],
                _max_file_size_mb=10,
                _require_scan=False,
            ).create()
