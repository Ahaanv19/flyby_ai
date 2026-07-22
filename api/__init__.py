"""
Flyby AI — HTTP API package.

Blueprints registered by ``main.py``:

    auth.py        /auth/v1/*      sign-up, sign-in, refresh, password reset
    rest.py        /rest/v1/*      the generic table query layer + /rpc
    storage.py     /storage/v1/*   avatar and receipt uploads
    functions.py   /functions/v1/* 2FA, travel search, transcription
    flyby.py       /api/*          trips, expenses, chats, notifications, itineraries
    jwt_authorize.py                the @token_required guard used across the API
"""
