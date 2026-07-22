# ✈️ Flyby AI — Backend

The API behind **Flyby AI**, the corporate travel platform. It handles
authentication, stores every trip, expense and conversation, enforces travel
policy, and serves travel search — and it ships with a small operations console
for administrators.

Built with Flask, SQLAlchemy and SQLite.

| Part     | Tech                        | Folder            | Port |
| -------- | --------------------------- | ----------------- | ---- |
| Backend  | Flask + SQLAlchemy + SQLite | `flyby_ai` (here) | 8659 |
| Frontend | React + Vite + TypeScript   | `../flyby`        | 8080 |

---

## 🚀 Run it locally

```bash
# First time only
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Every time
python main.py
```

Then, in a second terminal, start the frontend:

```bash
cd ../flyby
make install    # first time only
make            # starts the Vite dev server on :8080
```

Open **http://localhost:8080** for the app, or **http://localhost:8659** for the
backend console.

> `make run` from the `flyby` folder starts both at once.

**Seeded accounts** (created on first run):

| Email            | Password     | Role  |
| ---------------- | ------------ | ----- |
| `admin@flyby.ai` | `Flyby!2024` | Admin |
| `demo@flyby.ai`  | `Flyby!2024` | User  |

Change them with `ADMIN_PASSWORD` / `DEFAULT_PASSWORD` in `.env`.

---

## 🔌 How the frontend connects

In development the Vite dev server proxies every backend path to `:8659`, so the
browser talks to a single origin and there is no CORS to configure:

```
Browser (localhost:8080)
   │  React app → src/integrations/backend/client.ts
   ▼
Vite dev server (:8080)
   │  proxies /api /auth /rest /functions /storage
   ▼
Flask backend (:8659)
   ├─ api/auth.py       sign-up, sign-in, refresh, recovery
   ├─ api/rest.py       the table query layer + rpc
   ├─ api/flyby.py      trips, expenses, chats, notifications, itineraries
   ├─ api/functions.py  2FA, travel search, transcription
   ├─ api/storage.py    avatars and receipts
   └─ SQLite            instance/volumes/flyby.db
```

For a split deploy, set `VITE_API_URL` in the frontend and `FRONTEND_ORIGINS`
here.

---

## 📁 Layout

```
__init__.py        the Flask app, database, CORS and config
main.py            entry point: blueprints, console routes, CLI commands
api/
  auth.py          /auth/v1      — the login system
  rest.py          /rest/v1      — one query layer for every table
  flyby.py         /api          — trips, expenses, chats, notifications, …
  functions.py     /functions/v1 — 2FA, travel search, transcription
  storage.py       /storage/v1   — file uploads
  travel_search.py flight / hotel / ground inventory
  jwt_authorize.py the @token_required guard
model/
  base.py          shared serialization + CRUD used by every table
  user.py          users + profiles (the login system)
  company.py       workspaces and travel policy
  trip.py          trips, itineraries, alerts, documents
  expense.py       expenses
  preferences.py   traveler preferences, loyalty, client companies
  roles.py         roles and permissions (RBAC)
  security.py      audit log, sessions, security alerts
  chat.py          conversations
  notification.py  in-app notifications
  mfa.py           two-factor credentials and challenges
templates/         the operations console (Jinja2)
instance/volumes/  the SQLite database
```

---

## 🔐 How access is decided

Every request that touches user data goes through `@token_required()` and is
then scoped **server-side** by the table registry in `api/rest.py`:

- A caller only ever sees rows they own. A client-supplied `user_id` is ignored
  on writes and cannot widen a read.
- Teammates' profiles are readable (so the app can show names and avatars) but
  never writable.
- Admin-only tables and actions re-check the role on the server; hiding a button
  in the UI is presentation, not access control.
- Passwords change only with the current password or a valid recovery token — a
  stolen session alone is not enough.

---

## 🛠️ CLI commands

```bash
# Create every table and seed the development data
python -m flask --app main custom generate_data

# Back up the database and export each table to backup/*.json
python -m flask --app main custom backup_data

# Restore accounts from backup/*.json
python -m flask --app main custom restore_data
```

---

## 🩺 Health checks

```bash
curl http://localhost:8659/health       # service + database
curl http://localhost:8659/api/health   # API only
```

---

## ⚙️ Configuration

Copy `.env.example` to `.env`. Everything has a working default, so the backend
runs with no `.env` at all.

The settings worth knowing:

- `SECRET_KEY` — signs tokens and cookies. **Change it before deploying.**
- `FLASK_PORT` — defaults to 8659.
- `ADMIN_PASSWORD` / `DEFAULT_PASSWORD` — the seeded accounts.
- `TWILIO_*` — real SMS for two-step verification. Without it, the code is
  printed to the backend console so the flow still works locally.
- `OPENAI_API_KEY` — voice transcription. Without it, dictation reports that it
  needs a key rather than failing silently.
- `DB_ENDPOINT` / `DB_USERNAME` / `DB_PASSWORD` — switch from SQLite to MySQL.
