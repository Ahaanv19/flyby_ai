"""
Flyby AI — database models package.

Each module defines the SQLAlchemy models for one part of the product and an
``init*()`` seeder that ``main.py`` runs via ``flask custom generate_data``:

    user.py          users + profiles (the login system)
    company.py       companies (workspaces) + travel policies
    trip.py          trips, itineraries, travel alerts, calendar suggestions, documents
    expense.py       expenses
    preferences.py   travel preferences, loyalty programs, client companies
    roles.py         roles, permissions, role grants (RBAC)
    security.py      audit log, active sessions, security alerts, upload policy
    chat.py          conversations and company messages
    notification.py  in-app notifications

Shared serialization/CRUD behavior lives in ``base.py``.
"""
