"""
Flyby AI — file storage API (``/storage/v1``).

Backs avatar uploads and expense receipts. Files live under
``instance/storage/<bucket>/<user-uuid>/<name>`` — the uploader's UUID is taken
from the session and forced into the path, so one user can never write into or
delete another user's folder no matter what key they send.

Endpoints:
    POST   /storage/v1/object/<bucket>/<key>          upload (raw body or multipart)
    DELETE /storage/v1/object/<bucket>                remove keys
    GET    /storage/v1/object/public/<bucket>/<key>   serve a stored file
"""

import os
import re

from flask import (Blueprint, current_app, g, jsonify, request,
                   send_from_directory)
from werkzeug.utils import secure_filename

from api.jwt_authorize import token_required

storage_api = Blueprint('storage_api', __name__, url_prefix='/storage/v1')

# Buckets the frontend uses, with the content types each accepts.
BUCKETS = {
    "avatars": {"image/png", "image/jpeg", "image/webp", "image/gif"},
    "receipts": {"image/png", "image/jpeg", "image/webp", "application/pdf"},
    "documents": {"image/png", "image/jpeg", "image/webp", "application/pdf"},
}

MAX_BYTES = 10 * 1024 * 1024
UUID_SEGMENT = re.compile(r"^[0-9a-fA-F-]{36}$")


def _bucket_root(bucket):
    root = os.path.join(current_app.config["STORAGE_FOLDER"], bucket)
    os.makedirs(root, exist_ok=True)
    return root


def _safe_key(key, user_id):
    """
    Turn a client-supplied key into a path inside the caller's own folder.

    The frontend sends ``<user-id>/avatar.png``. Any leading directory is
    replaced with the caller's UUID from the session, and the filename is
    sanitised, so ``../`` or another user's id cannot escape the folder.
    """
    parts = [p for p in str(key or "").split("/") if p not in ("", ".", "..")]
    filename = secure_filename(parts[-1]) if parts else ""
    if not filename:
        filename = "file"
    return os.path.join(user_id, filename)


@storage_api.route('/object/<bucket>/<path:key>', methods=['POST', 'PUT'])
@token_required()
def upload(bucket, key):
    """Store a file in ``bucket`` under the caller's folder."""
    if bucket not in BUCKETS:
        return jsonify({"error": {"message": f"Unknown bucket '{bucket}'."}}), 404

    upload_file = request.files.get("file")
    if upload_file is not None:
        payload = upload_file.read()
        content_type = upload_file.mimetype or request.content_type or ""
    else:
        payload = request.get_data()
        content_type = (request.content_type or "").split(";")[0].strip()

    if not payload:
        return jsonify({"error": {"message": "No file content was received."}}), 400
    if len(payload) > MAX_BYTES:
        return jsonify({"error": {"message": "File is larger than the 10 MB limit."}}), 413

    allowed = BUCKETS[bucket]
    if content_type and content_type not in allowed:
        return jsonify({
            "error": {"message": f"'{content_type}' files are not allowed in this bucket."}
        }), 415

    relative = _safe_key(key, g.user_id)
    destination = os.path.join(_bucket_root(bucket), relative)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(payload)

    return jsonify({
        "Key": f"{bucket}/{relative}",
        "path": relative,
        "publicUrl": f"/storage/v1/object/public/{bucket}/{relative}",
        "error": None,
    }), 200


@storage_api.route('/object/<bucket>', methods=['DELETE'])
@token_required()
def remove(bucket):
    """Delete one or more keys from ``bucket`` (only within the caller's folder)."""
    if bucket not in BUCKETS:
        return jsonify({"error": {"message": f"Unknown bucket '{bucket}'."}}), 404

    body = request.get_json(silent=True) or {}
    prefixes = body.get("prefixes") or []
    if isinstance(prefixes, str):
        prefixes = [prefixes]

    removed = []
    root = _bucket_root(bucket)
    for key in prefixes:
        relative = _safe_key(key, g.user_id)
        target = os.path.join(root, relative)
        if os.path.isfile(target):
            try:
                os.remove(target)
                removed.append(relative)
            except OSError:
                pass

    return jsonify({"data": removed, "error": None}), 200


@storage_api.route('/object/public/<bucket>/<path:key>', methods=['GET'])
def serve(bucket, key):
    """
    Serve a stored file.

    Public by design: these URLs are written into ``avatar_url`` and rendered by
    the app in contexts (img tags) that cannot send an Authorization header.
    Only the buckets listed above are reachable, and the path is re-sanitised.
    """
    if bucket not in BUCKETS:
        return jsonify({"error": {"message": f"Unknown bucket '{bucket}'."}}), 404

    parts = [p for p in str(key).split("/") if p not in ("", ".", "..")]
    if len(parts) < 2 or not UUID_SEGMENT.match(parts[0]):
        return jsonify({"error": {"message": "Not found."}}), 404

    relative = os.path.join(parts[0], secure_filename(parts[-1]))
    root = _bucket_root(bucket)
    if not os.path.isfile(os.path.join(root, relative)):
        return jsonify({"error": {"message": "Not found."}}), 404

    return send_from_directory(root, relative, max_age=0)
