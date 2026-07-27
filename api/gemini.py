"""
Flyby AI — shared Gemini client.

One place that knows how to call Gemini and parse a JSON answer back, so every
AI feature (trip reasoning, natural-language search, …) uses the same request
shape, auth, retry and fallback behavior.

``call_gemini`` returns the parsed JSON dict, or None on any failure — callers
always have a deterministic local fallback, so a missing key, a rate limit or a
malformed response degrades gracefully instead of surfacing an error.
"""

import json
import logging
import re
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)


def gemini_key():
    """Return a usable Gemini key, or None when unset/placeholder."""
    key = (current_app.config.get("GEMINI_API_KEY") or "").strip()
    if not key or key.lower() in ("xxxxx", "your_key_here"):
        return None
    return key


def gemini_available():
    """Whether a real Gemini key is configured."""
    return gemini_key() is not None


def call_gemini(system_prompt, user_text, *, max_output_tokens=2048, temperature=0.5, timeout=15):
    """
    Call Gemini with a system prompt and a user turn; return parsed JSON or None.

    The model is asked for ``application/json`` and the response is stripped of
    any code fences before parsing. One quick retry covers a transient overload,
    then it falls back so the caller never waits long.
    """
    key = gemini_key()
    if not key:
        return None

    url = current_app.config["GEMINI_SERVER"]
    # Current Gemini API keys authenticate via the x-goog-api-key header — not a
    # ?key= query param, and not Bearer (which expects an OAuth token).
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": temperature,
            # This is a reasoning model that spends part of the budget on hidden
            # thinking, so the ceiling is set high enough that the JSON answer
            # still fits afterward. (Disabling thinking outright is rejected.)
            "maxOutputTokens": max_output_tokens,
        },
    }

    resp = None
    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as error:
            logger.warning("Gemini request failed (%s); falling back", error)
            return None
        if resp.status_code == 200:
            break
        if resp.status_code in (429, 500, 502, 503):
            logger.warning("Gemini transient %s (attempt %s)", resp.status_code, attempt + 1)
            if attempt == 0:
                time.sleep(0.4)
            continue
        logger.warning("Gemini returned %s: %s", resp.status_code, resp.text[:300])
        return None

    if resp is None or resp.status_code != 200:
        return None

    try:
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        # A reasoning model can return multiple parts; keep only the answer text.
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        text = re.sub(r"^```json\s*", "", text.strip(), flags=re.I)
        text = re.sub(r"```\s*$", "", text).strip()
        return json.loads(text)
    except (KeyError, IndexError, ValueError) as error:
        logger.warning("Gemini response parse failed: %s", error)
        return None
