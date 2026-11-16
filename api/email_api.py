"""
Flyby AI — email endpoints (``/api/email``).

    GET  /api/email/status   whether this server can send email
    POST /api/email/test     send the caller a test message, and say if it arrived at the mail server

The test goes only to the caller's own account address — there is no way to
name another recipient — and is limited to one every 30 seconds per account, so
it can't be turned into a way to send mail to anyone else or to flood an inbox.
"""

import time

from flask import Blueprint, current_app, g, jsonify

from api import email_templates as templates
from api import mailer
from api.jwt_authorize import token_required

email_api = Blueprint('email_api', __name__, url_prefix='/api/email')

TEST_INTERVAL_SECONDS = 30
_last_test = {}


@email_api.route('/status', methods=['GET'])
@token_required()
def status():
    """Whether email can be sent, and the address it comes from."""
    configured = mailer.is_configured()
    return jsonify({
        "configured": configured,
        "from": current_app.config.get("MAIL_FROM") if configured else None,
    }), 200


@email_api.route('/test', methods=['POST'])
@token_required()
def test():
    """Send the signed-in user a test email and report the mail server's answer."""
    user = g.current_user
    if not mailer.is_configured():
        return jsonify({"sent": False, "reason": "not_configured",
                        "message": "Email isn't set up on this server yet."}), 200

    now = time.time()
    if now - _last_test.get(user.uuid, 0) < TEST_INTERVAL_SECONDS:
        return jsonify({"sent": False, "reason": "too_soon",
                        "message": "A test was just sent. Give it a moment before trying again."}), 429
    _last_test[user.uuid] = now

    subject, text, html = templates.test_message(user.name, current_app.config.get("MAIL_FROM"))
    ok, problem = mailer.send_now(user.email, subject, text, html, kind="test", user_id=user.uuid)
    if ok:
        return jsonify({"sent": True, "to": user.email}), 200
    return jsonify({"sent": False, "reason": "delivery_failed", "message": problem}), 200
