"""
Flyby AI — role and permission models (RBAC).

The frontend asks two questions: "what role is this user?" (``user_roles``) and
"may this user do X?" (the ``has_permission`` function, resolved through
``role_permissions`` -> ``permissions``). Both are answered from these tables.

Roles use the frontend's vocabulary — ``admin`` | ``moderator`` | ``user`` —
while the ``users`` table keeps the console's capitalised ``Admin``/``User``.
``sync_from_user()`` keeps the two in step so promoting someone in the admin
console immediately changes what they can do in the app.
"""

from __init__ import app, db
from model.base import RowMixin, new_uuid, utcnow

APP_ROLES = ("admin", "moderator", "user")

# The permission set the frontend checks against. Kept here so the seeder and
# the authorization API agree on one list.
DEFAULT_PERMISSIONS = [
    ("expense.approve", "expenses", "approve", "Approve or reject submitted expenses"),
    ("expense.view_all", "expenses", "read", "View expenses across the whole company"),
    ("trip.manage", "trips", "write", "Create and edit trips for other travelers"),
    ("trip.view_all", "trips", "read", "View trips across the whole company"),
    ("user.manage", "users", "write", "Invite, edit and deactivate team members"),
    ("tenant.settings", "company", "write", "Change company-wide settings and policy"),
    ("report.generate", "reports", "read", "Generate and export spend reports"),
    ("file.delete", "files", "delete", "Delete uploaded files and receipts"),
    ("security.view", "security", "read", "View the security dashboard and audit log"),
]

# Which permissions each role receives when the tables are seeded.
ROLE_GRANTS = {
    "admin": [name for name, *_ in DEFAULT_PERMISSIONS],
    "moderator": [
        "expense.approve", "expense.view_all", "trip.manage",
        "trip.view_all", "report.generate",
    ],
    "user": [],
}


class UserRole(db.Model, RowMixin):
    """
    User Role Model — a user's application role, in ``user_roles``.
    """
    __tablename__ = 'user_roles'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _user_id = db.Column(db.String(36), db.ForeignKey('users._uuid'), nullable=False, index=True)
    _role = db.Column(db.String(30), nullable=False, default="user")
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    __table_args__ = (db.UniqueConstraint('_user_id', '_role', name='uq_user_role'),)

    COLUMNS = {
        "id": "id",
        "user_id": "_user_id",
        "role": "_role",
        "created_at": "_created_at",
    }

    @staticmethod
    def role_for(user_id):
        """Return the user's role name, defaulting to ``user``."""
        row = UserRole.query.filter_by(_user_id=user_id).first()
        return row._role if row else "user"

    @staticmethod
    def sync_from_user(user):
        """
        Mirror a ``User.role`` onto the RBAC table.

        Called after sign-up and whenever an admin changes someone's role, so
        the console and the app never disagree about who is an admin.
        """
        if user is None:
            return None
        desired = user.app_role
        row = UserRole.query.filter_by(_user_id=user.uuid).first()
        if row is None:
            row = UserRole(id=new_uuid(), _user_id=user.uuid, _role=desired)
            return row.create()
        if row._role != desired:
            row._role = desired
            row.save()
        return row


class Permission(db.Model, RowMixin):
    """
    Permission Model — a single named capability, in ``permissions``.
    """
    __tablename__ = 'permissions'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _name = db.Column(db.String(100), unique=True, nullable=False)
    _resource = db.Column(db.String(50), nullable=False, default="")
    _action = db.Column(db.String(50), nullable=False, default="")
    _description = db.Column(db.String(255), nullable=True)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    COLUMNS = {
        "id": "id",
        "name": "_name",
        "resource": "_resource",
        "action": "_action",
        "description": "_description",
        "created_at": "_created_at",
    }


class RolePermission(db.Model, RowMixin):
    """
    Role Permission Model — grants a permission to a role, in ``role_permissions``.
    """
    __tablename__ = 'role_permissions'

    id = db.Column(db.String(36), primary_key=True, default=new_uuid)
    _role = db.Column(db.String(30), nullable=False, index=True)
    _permission_id = db.Column(db.String(36), db.ForeignKey('permissions.id'), nullable=False)
    _created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    permission = db.relationship('Permission', lazy='joined')

    COLUMNS = {
        "id": "id",
        "role": "_role",
        "permission_id": "_permission_id",
        "created_at": "_created_at",
    }

    def read(self):
        """
        Include the joined permission under a ``permissions`` key.

        The frontend selects ``permission_id, permissions(name)`` and reads the
        nested name, so the relation is embedded here rather than requiring a
        second round trip.
        """
        data = super().read()
        data["permissions"] = {"name": self.permission._name} if self.permission else None
        return data


def has_permission(user_id, permission_name):
    """
    Return True when the user's role grants ``permission_name``.

    This is the server-side check behind the frontend's ``has_permission`` call;
    the answer never depends on anything the client sends beyond the user id.
    """
    if not user_id or not permission_name:
        return False
    role = UserRole.role_for(user_id)
    match = (
        db.session.query(RolePermission.id)
        .join(Permission, Permission.id == RolePermission._permission_id)
        .filter(RolePermission._role == role)
        .filter(Permission._name == permission_name)
        .first()
    )
    return match is not None


def permissions_for_role(role):
    """Return the list of permission names granted to ``role``."""
    rows = (
        db.session.query(Permission._name)
        .join(RolePermission, RolePermission._permission_id == Permission.id)
        .filter(RolePermission._role == role)
        .all()
    )
    return [row[0] for row in rows]


def initRoles():
    """Create the RBAC tables and seed the standard permission grants."""
    with app.app_context():
        db.create_all()

        by_name = {}
        for name, resource, action, description in DEFAULT_PERMISSIONS:
            existing = Permission.query.filter_by(_name=name).first()
            if existing is None:
                existing = Permission(
                    id=new_uuid(), _name=name, _resource=resource,
                    _action=action, _description=description,
                ).create()
            by_name[name] = existing

        for role, granted in ROLE_GRANTS.items():
            for name in granted:
                permission = by_name.get(name)
                if permission is None:
                    continue
                exists = RolePermission.query.filter_by(
                    _role=role, _permission_id=permission.id,
                ).first()
                if exists is None:
                    RolePermission(
                        id=new_uuid(), _role=role, _permission_id=permission.id,
                    ).create()

        # Bring any existing accounts in line with their console role.
        from model.user import User
        for user in User.query.all():
            UserRole.sync_from_user(user)
