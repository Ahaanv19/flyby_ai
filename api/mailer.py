"""
Flyby AI — sending email.

One job: get a message to a mail server without ever getting in the way of the
request that caused it. A sign-in must not wait on a slow mail server, and a
booking that has just charged a card must not fail because the confirmation
email couldn't be sent.

So ``send`` only queues. A single background thread delivers, retries once on a
dropped connection, and writes what happened to the audit log (``email_sent`` /
``email_failed``) so there is a record to check when someone says "I never got
it". ``send_now`` delivers on the spot and reports the mail server's answer —
for the "send me a test email" button, where the answer is the point.

Mail goes over SMTP with whatever account is configured in ``.env`` (see the
Email section of ``__init__.py``). With nothing configured, nothing is sent:
``is_configured()`` is False, ``send`` returns False, and the app carries on.

What is never logged: message bodies (a password-reset link is a credential) and
full recipient addresses.
"""

import logging
import queue
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from flask import current_app

logger = logging.getLogger(__name__)

ADDRESS = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")
TIMEOUT_SECONDS = 15
QUEUE_LIMIT = 200

# What email has done since the server started, for the admin page.
LAST_STATUS = {"checked_at": None, "ok": None, "message": None, "sent": 0, "failed": 0}

_queue = queue.Queue(maxsize=QUEUE_LIMIT)
_worker = None
_worker_lock = threading.Lock()


def _settings():
    config = current_app.config
    return {
        "host": config.get("SMTP_HOST"),
        "port": config.get("SMTP_PORT") or 587,
        "username": config.get("SMTP_USERNAME"),
        "password": config.get("SMTP_PASSWORD"),
        "security": config.get("SMTP_SECURITY") or "starttls",
        "from_address": config.get("MAIL_FROM"),
        "from_name": config.get("MAIL_FROM_NAME") or "Flyby AI",
        "reply_to": config.get("MAIL_REPLY_TO"),
    }


def is_configured():
    """Whether there is a mail server to send through."""
    settings = _settings()
    return bool(settings["host"] and settings["from_address"])


def mask(address):
    """a***@example.com — enough to recognise, not enough to harvest."""
    name, _, domain = (address or "").partition("@")
    return f"{name[:1]}***@{domain}" if domain else "***"


def _record(ok, message):
    LAST_STATUS.update(
        checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        ok=ok, message=(message or "")[:300] or None,
    )
    LAST_STATUS["sent" if ok else "failed"] += 1


def _build(settings, to, subject, text, html):
    message = EmailMessage()
    # A newline in a header would let a value smuggle in headers of its own.
    message["Subject"] = re.sub(r"[\r\n]+", " ", subject).strip()[:200]
    message["From"] = formataddr((settings["from_name"], settings["from_address"]))
    message["To"] = to
    if settings["reply_to"] and ADDRESS.match(settings["reply_to"]):
        message["Reply-To"] = settings["reply_to"]
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=settings["from_address"].split("@")[-1])
    # Tells mail clients this is a notification, so it isn't answered with an
    # out-of-office reply.
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    return message


def _deliver(settings, message):
    """Hand one message to the mail server. Raises on failure."""
    if settings["security"] == "ssl":
        server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=TIMEOUT_SECONDS,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(settings["host"], settings["port"], timeout=TIMEOUT_SECONDS)
    try:
        server.ehlo()
        if settings["security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if settings["username"] and settings["password"]:
            server.login(settings["username"], settings["password"])
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 — the message is already sent or already failed
            pass


def _explain(error):
    """A mail server's refusal as something a person can act on."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return ("The mail server rejected the username or password. With Gmail, SMTP_PASSWORD "
                "must be an app password, not the account password.")
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "The mail server refused that recipient address."
    if isinstance(error, smtplib.SMTPSenderRefused):
        return "The mail server refused the From address. MAIL_FROM must be one this account may send as."
    if isinstance(error, ssl.SSLError):
        return "The secure connection to the mail server failed. Check SMTP_PORT and SMTP_SECURITY."
    if isinstance(error, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)) \
            or (isinstance(error, OSError) and not isinstance(error, smtplib.SMTPException)):
        return f"Couldn't connect to the mail server ({type(error).__name__}). Check SMTP_HOST and SMTP_PORT."
    return f"The mail server reported an error ({type(error).__name__})."


def _attempt(settings, message, attempts=2):
    """Deliver, retrying once if the connection dropped. Returns (ok, error_text)."""
    for attempt in range(attempts):
        try:
            _deliver(settings, message)
            return True, None
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, ConnectionError) as error:
            if attempt + 1 < attempts:
                time.sleep(1.5)
                continue
            return False, _explain(error)
        except Exception as error:  # noqa: BLE001 — every failure is reported, none raised
            return False, _explain(error)
    return False, "The mail server couldn't be reached."


def _log_outcome(app, job, ok, error):
    _record(ok, None if ok else error)
    if ok:
        logger.info("Email sent: %s to %s", job["kind"], mask(job["to"]))
    else:
        logger.warning("Email failed: %s to %s — %s", job["kind"], mask(job["to"]), error)
    try:
        with app.app_context():
            from model.security import AuditLog
            AuditLog.record(job.get("user_id"), "email_sent" if ok else "email_failed", ok,
                            kind=job["kind"], to=mask(job["to"]),
                            **({} if ok else {"reason": (error or "")[:200]}))
    except Exception:  # noqa: BLE001 — the audit entry is a record, not a requirement
        pass


def _run(app):
    while True:
        job = _queue.get()
        try:
            ok, error = _attempt(job["settings"], job["message"])
            _log_outcome(app, job, ok, error)
        except Exception as error:  # noqa: BLE001 — the worker must survive anything
            logger.error("Email worker error: %s", error)
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            app = current_app._get_current_object()
            _worker = threading.Thread(target=_run, args=(app,), name="flyby-mailer", daemon=True)
            _worker.start()


# Domains set aside for examples and testing (RFC 2606 / 6761). Nothing can be
# delivered to them, so a real mail server only bounces the message back to
# Flyby's own inbox, and bounces count against the sender's reputation. Test
# accounts use them, so skip them unless mail goes to a local test server.
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost")


def _undeliverable(address):
    domain = address.rsplit("@", 1)[-1].lower().rstrip(".")
    return (domain in _RESERVED_DOMAINS or domain.endswith(_RESERVED_SUFFIXES)
            or any(domain.endswith("." + d) for d in _RESERVED_DOMAINS))


def _prepare(to, subject, text, html, kind, user_id):
    to = (to or "").strip()
    if not ADDRESS.match(to):
        return None, "That isn't a valid email address."
    if not is_configured():
        return None, "Email isn't set up on this server."
    settings = _settings()
    if _undeliverable(to) and (settings.get("host") or "").lower() not in ("localhost", "127.0.0.1"):
        return None, "That address is on a domain reserved for testing; nothing can be delivered there."
    return {
        "settings": settings, "to": to, "kind": kind, "user_id": user_id,
        "message": _build(settings, to, subject, text, html),
    }, None


def send(to, subject, text, html=None, *, kind="notification", user_id=None):
    """
    Queue an email. Returns True if it was queued, False if it can't be sent
    (no mail server configured, a bad address, or the queue is full). Never
    raises and never blocks.
    """
    try:
        job, problem = _prepare(to, subject, text, html, kind, user_id)
        if job is None:
            logger.debug("Email not sent (%s): %s", kind, problem)
            return False
        _ensure_worker()
        _queue.put_nowait(job)
        return True
    except queue.Full:
        logger.warning("Email queue is full; dropped a %s email", kind)
        return False
    except Exception as error:  # noqa: BLE001 — sending mail must never break a request
        logger.warning("Email could not be queued (%s): %s", kind, error)
        return False


def send_now(to, subject, text, html=None, *, kind="test", user_id=None):
    """Deliver immediately and say what the mail server answered: ``(ok, problem)``."""
    try:
        job, problem = _prepare(to, subject, text, html, kind, user_id)
        if job is None:
            return False, problem
        ok, error = _attempt(job["settings"], job["message"], attempts=1)
        _log_outcome(current_app._get_current_object(), job, ok, error)
        return ok, error
    except Exception as error:  # noqa: BLE001
        return False, _explain(error)


def wait_until_sent(timeout=10.0):
    """Block until the queue is empty. For tests and scripts, not requests."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _queue.unfinished_tasks == 0:
            return True
        time.sleep(0.05)
    return False
