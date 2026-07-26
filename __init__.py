"""
Flyby AI — backend application object.

This module builds the single Flask ``app`` used by the whole backend and wires
up the pieces every other module imports:

    from __init__ import app, db, login_manager

The structure is unchanged from the original template (one Flask app, one
SQLAlchemy ``db``, Flask-Login for the server-rendered console, JWT for the
React frontend) — only the configuration is now tailored to Flyby AI:

* SQLite database at ``instance/volumes/flyby.db``
* CORS that allows the Vite dev server, with credentials so auth cookies flow
* Port 8659 (see ``main.py``)
"""

from flask import Flask
from flask_login import LoginManager
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

# Setup of key Flask object (app)
app = Flask(__name__)

# Initialize Flask-Login object (used by the server-rendered admin console)
login_manager = LoginManager()
login_manager.init_app(app)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# The React frontend (Vite) runs on :8080 in development and proxies /api,
# /auth, /rest, /storage and /functions to this backend, so in dev the browser
# is same-origin and CORS is not strictly required. It is configured anyway so
# a split deployment (frontend hosted elsewhere) works without code changes.
#
# Extra origins: FRONTEND_ORIGINS="https://a.com,https://b.com"
_default_origins = [
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
    'http://localhost:4173',
    'http://127.0.0.1:4173',
    'http://localhost:8659',
    'http://127.0.0.1:8659',
]
_extra_origins = [
    o.strip() for o in os.environ.get('FRONTEND_ORIGINS', '').split(',') if o.strip()
]

cors = CORS(
    app,
    supports_credentials=True,
    origins=_default_origins + _extra_origins,
    allow_headers=['Content-Type', 'Authorization', 'X-Origin', 'Prefer', 'X-Client-Info'],
    expose_headers=['Content-Range', 'X-Total-Count'],
    methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
)

# ---------------------------------------------------------------------------
# System defaults — seeded accounts for local development
# ---------------------------------------------------------------------------
app.config['ADMIN_USER'] = os.environ.get('ADMIN_USER') or 'admin@flyby.ai'
app.config['ADMIN_PASSWORD'] = os.environ.get('ADMIN_PASSWORD') or os.environ.get('DEFAULT_PASSWORD') or 'Flyby!2024'
app.config['DEFAULT_USER'] = os.environ.get('DEFAULT_USER') or 'demo@flyby.ai'
app.config['DEFAULT_PASSWORD'] = os.environ.get('DEFAULT_PASSWORD') or 'Flyby!2024'

# ---------------------------------------------------------------------------
# Session / token settings
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get('SECRET_KEY') or 'flyby-dev-secret-change-me'
SESSION_COOKIE_NAME = os.environ.get('SESSION_COOKIE_NAME') or 'flyby_session'
JWT_TOKEN_NAME = os.environ.get('JWT_TOKEN_NAME') or 'flyby_jwt'
app.config['SECRET_KEY'] = SECRET_KEY
app.config['SESSION_COOKIE_NAME'] = SESSION_COOKIE_NAME
app.config['JWT_TOKEN_NAME'] = JWT_TOKEN_NAME
# Access tokens are short-lived; the frontend silently refreshes them using the
# long-lived refresh token, mirroring the auth client it replaced.
app.config['JWT_ACCESS_TTL_SECONDS'] = int(os.environ.get('JWT_ACCESS_TTL_SECONDS') or 60 * 60)
app.config['JWT_REFRESH_TTL_SECONDS'] = int(os.environ.get('JWT_REFRESH_TTL_SECONDS') or 60 * 60 * 24 * 30)

# ---------------------------------------------------------------------------
# Database settings
# ---------------------------------------------------------------------------
dbName = 'flyby'
DB_ENDPOINT = os.environ.get('DB_ENDPOINT') or None
DB_USERNAME = os.environ.get('DB_USERNAME') or None
DB_PASSWORD = os.environ.get('DB_PASSWORD') or None
if DB_ENDPOINT and DB_USERNAME and DB_PASSWORD:
    # Production - Use MySQL
    DB_PORT = '3306'
    DB_NAME = dbName
    dbString = f'mysql+pymysql://{DB_USERNAME}:{DB_PASSWORD}@{DB_ENDPOINT}:{DB_PORT}'
    dbURI = dbString + '/' + dbName
    backupURI = None  # MySQL backup would require a different approach
else:
    # Development - Use SQLite (instance/volumes/flyby.db)
    dbString = 'sqlite:///volumes/'
    dbURI = dbString + dbName + '.db'
    backupURI = dbString + dbName + '_bak.db'

app.config['DB_ENDPOINT'] = DB_ENDPOINT
app.config['DB_USERNAME'] = DB_USERNAME
app.config['DB_PASSWORD'] = DB_PASSWORD
app.config['SQLALCHEMY_DATABASE_NAME'] = dbName
app.config['SQLALCHEMY_DATABASE_STRING'] = dbString
app.config['SQLALCHEMY_DATABASE_URI'] = dbURI
app.config['SQLALCHEMY_BACKUP_URI'] = backupURI
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# The SQLite folder must exist before the first connection is opened.
os.makedirs(os.path.join(app.instance_path, 'volumes'), exist_ok=True)

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ---------------------------------------------------------------------------
# Uploads (avatars + expense receipts)
# ---------------------------------------------------------------------------
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # maximum size of uploaded content
app.config['UPLOAD_EXTENSIONS'] = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf']
app.config['UPLOAD_FOLDER'] = os.path.join(app.instance_path, 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
# Buckets used by the frontend storage client (avatars, receipts).
app.config['STORAGE_FOLDER'] = os.path.join(app.instance_path, 'storage')
os.makedirs(app.config['STORAGE_FOLDER'], exist_ok=True)

# ---------------------------------------------------------------------------
# Optional third-party integrations
# ---------------------------------------------------------------------------
# Travel search, transcription and SMS all fall back to deterministic local
# implementations when these are unset, so the app is fully usable offline.
app.config['GEMINI_API_KEY'] = os.environ.get('GEMINI_API_KEY') or None
app.config['GEMINI_SERVER'] = (
    os.environ.get('GEMINI_SERVER')
    or 'https://generativelanguage.googleapis.com/v1beta/models/'
       'gemini-flash-latest:generateContent'
)
app.config['OPENAI_API_KEY'] = os.environ.get('OPENAI_API_KEY') or None
app.config['TWILIO_ACCOUNT_SID'] = os.environ.get('TWILIO_ACCOUNT_SID') or None
app.config['TWILIO_AUTH_TOKEN'] = os.environ.get('TWILIO_AUTH_TOKEN') or None
app.config['TWILIO_FROM_NUMBER'] = os.environ.get('TWILIO_FROM_NUMBER') or None

# Security hardening: rate limiting, security/CSP headers, audit logging,
# secure cookies (prod), and weak-secret checks. Safe-by-default — see
# utils/security.py. Wired last so it can read the config set above.
# Wrapped defensively: if a security dependency is missing in an environment,
# the app still boots (degraded) rather than returning 502, and logs loudly.
try:
    from utils.security import init_security
    init_security(app)
except Exception as _sec_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger(__name__).error(
        "Security hardening failed to initialize (%s). The app is running "
        "WITHOUT the new security middleware. Check that Flask-Limiter and "
        "bleach are installed (pip install -r requirements.txt).", _sec_err,
    )
