"""
JUTAKU Instagram DM Automation
-------------------------------
Implements the exact three flows from the "JUTAKU — DM Rent & DM Share
Flows" diagram (DM Rent / DM Share / DM Loan), end to end:

  * POST /start_flow   — call this the moment you send the opening "DM Sent"
                          message to a lead (from your ad click-to-DM, a
                          broadcast tool, or manually). It begins tracking
                          that lead's "Did they reply?" timer.
  * GET  /webhook       — Meta's webhook verification handshake.
  * POST /webhook       — incoming Instagram DM events. Routes each message
                          through the right flow's state machine (flows.py),
                          asks follow-up questions exactly as the diagram
                          specifies, and sends back the computed result.

A background scheduler checks every minute for leads who've gone quiet
past WAIT_MINUTES and fires each flow's "no response" behaviour — the
fallback-default calculation for Rent/Share, and a no-op (keep waiting)
for Loan, matching the diagram precisely.
"""

import hashlib
import hmac
import logging
import os
import re

import requests
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

import flows
from flows import (
    SESSIONS,
    get_session,
    start_session,
    end_session,
    disclose_once,
    HANDLERS,
    check_timeouts,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jutaku-bot")

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v19.0")
SEND_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
WAIT_MINUTES = float(os.environ.get("WAIT_MINUTES", flows.DEFAULT_WAIT_MINUTES))

# A simple shared secret so random people on the internet can't trigger
# /start_flow. Set this in your env and pass it as the X-Admin-Token header.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def extract_numbers(text: str):
    return [float(n.replace(",", "")) for n in NUMBER_RE.findall(text)]


def detect_flow(text: str):
    t = text.lower()
    if "rent" in t:
        return "rent"
    if "share" in t:
        return "share"
    if "loan" in t:
        return "loan"
    return None


# --------------------------------------------------------------- sending --
def send_message(recipient_id: str, text: str):
    if not PAGE_ACCESS_TOKEN:
        log.warning("PAGE_ACCESS_TOKEN not set — would have sent: %s", text)
        return
    resp = requests.post(
        SEND_URL,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        timeout=10,
    )
    if resp.status_code != 200:
        log.error("Failed to send message: %s %s", resp.status_code, resp.text)
    else:
        log.info("Sent -> %s: %.60s", recipient_id, text)


# ------------------------------------------------------- start_flow (admin) --
@app.route("/start_flow", methods=["POST"])
def start_flow():
    """Call this right when you send the initial outbound 'DM Sent' message
    to a lead, e.g. from your ads/CRM tooling:

        POST /start_flow
        X-Admin-Token: <ADMIN_TOKEN>
        { "sender_id": "<IGSID>", "flow": "rent" }

    flow is one of "rent", "share", "loan".
    """
    if ADMIN_TOKEN and request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    sender_id = body.get("sender_id")
    flow = body.get("flow")
    if not sender_id or flow not in ("rent", "share", "loan"):
        return jsonify(error="sender_id and flow ('rent'|'share'|'loan') required"), 400

    session = start_session(sender_id, flow)
    disclose_once(session, send_message)
    return jsonify(status="started", sender_id=sender_id, flow=flow), 200


# ------------------------------------------------------------ Meta webhook --
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verified.")
        return challenge, 200
    return "Forbidden", 403


def verify_signature(req) -> bool:
    if not APP_SECRET:
        return True
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode("utf-8"), req.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@app.route("/webhook", methods=["POST"])
def webhook():
    if not verify_signature(request):
        return "Invalid signature", 403

    data = request.get_json(silent=True) or {}
    if data.get("object") not in ("instagram", "page"):
        return jsonify(status="ignored"), 200

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            text = event.get("message", {}).get("text")
            if sender_id and text:
                handle_inbound(sender_id, text)

    return jsonify(status="ok"), 200


# --------------------------------------------------------------- routing --
def handle_inbound(sender_id: str, text: str):
    numbers = extract_numbers(text)
    session = get_session(sender_id)

    if session is None:
        # No one called /start_flow for this lead — they messaged in on
        # their own. Figure out which flow they mean and start one,
        # equivalent to the diagram's "Did they reply? -> Yes" branch.
        flow = detect_flow(text)
        if not flow:
            send_message(
                sender_id,
                "🤖 You're chatting with the JUTAKU automated assistant, not a "
                "person. Tell me if you want to know about *Rent*, *Share*, "
                "or *Loan* eligibility, and I'll walk you through it.",
            )
            return
        session = start_session(sender_id, flow)
        disclose_once(session, send_message)
        if numbers:
            # They front-loaded the number(s) already — skip straight past
            # the "ask for it" step into the amount-handling state.
            session.state = {
                "rent": "awaiting_rent_amount",
                "share": "awaiting_xy",
                "loan": "awaiting_takehome",
            }[flow]
            HANDLERS[flow](session, text, numbers, send_message)
        else:
            HANDLERS[flow](session, text, numbers, send_message)
        return

    disclose_once(session, send_message)
    HANDLERS[session.flow](session, text, numbers, send_message)


# ------------------------------------------------------------- scheduler --
# Pinned to UTC explicitly: some hosts set TZ to a locale alias (e.g.
# "Asia/Calcutta") that tzlocal can't resolve via zoneinfo, which crashes
# APScheduler's auto-detection at import time.
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    lambda: check_timeouts(send_message, wait_minutes=WAIT_MINUTES),
    "interval",
    minutes=1,
    id="timeout_check",
)
scheduler.start()


@app.route("/", methods=["GET"])
def health():
    return jsonify(status="jutaku-ig-bot running", open_sessions=len(SESSIONS)), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
