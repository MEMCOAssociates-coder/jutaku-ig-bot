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


def _parse_page_tokens(raw: str) -> dict:
    """PAGE_TOKENS="<IG account ID>:<Page token>,<IG account ID>:<Page token>"
    — one entry per Instagram account the bot answers. Each Instagram
    account replies with the token of the Facebook Page it's linked to."""
    tokens = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if ":" in part:
            account_id, token = part.split(":", 1)
            if account_id.strip() and token.strip():
                tokens[account_id.strip()] = token.strip()
    return tokens


# Several accounts: PAGE_TOKENS. One account: PAGE_ACCESS_TOKEN still works
# on its own (used for any account not listed in PAGE_TOKENS).
PAGE_TOKENS = _parse_page_tokens(os.environ.get("PAGE_TOKENS", ""))
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
def token_for(account_id: str) -> str:
    return PAGE_TOKENS.get(account_id or "") or PAGE_ACCESS_TOKEN


def send_message(account_id: str, recipient_id: str, text: str):
    """Sends from the Instagram account the conversation is on."""
    token = token_for(account_id)
    if not token:
        log.warning("No Page token for account %s — would have sent: %s", account_id or "(default)", text)
        return
    resp = requests.post(
        SEND_URL,
        params={"access_token": token},
        json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        timeout=10,
    )
    if resp.status_code != 200:
        log.error("Failed to send message: %s %s", resp.status_code, resp.text)
    else:
        log.info("Sent [%s] -> %s: %.60s", account_id or "default", recipient_id, text)


def sender_for(account_id: str):
    """A (recipient_id, text) send function bound to one Instagram account —
    what the flows in flows.py call to reply."""
    return lambda recipient_id, text: send_message(account_id, recipient_id, text)


def default_account() -> str:
    """The only configured account, when there's exactly one."""
    return next(iter(PAGE_TOKENS)) if len(PAGE_TOKENS) == 1 else ""


# ------------------------------------------------------- start_flow (admin) --
@app.route("/start_flow", methods=["POST"])
def start_flow():
    """Call this right when you send the initial outbound 'DM Sent' message
    to a lead, e.g. from your ads/CRM tooling:

        POST /start_flow
        X-Admin-Token: <ADMIN_TOKEN>
        { "sender_id": "<IGSID>", "flow": "rent", "account": "<IG account ID>" }

    flow is one of "rent", "share", "loan". account is the Instagram account
    the opening message went out from — required when the bot answers more
    than one account (PAGE_TOKENS), optional with just one.
    """
    if ADMIN_TOKEN and request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    sender_id = body.get("sender_id")
    flow = body.get("flow")
    if not sender_id or flow not in ("rent", "share", "loan"):
        return jsonify(error="sender_id and flow ('rent'|'share'|'loan') required"), 400
    account_id = str(body.get("account") or default_account())
    if len(PAGE_TOKENS) > 1 and account_id not in PAGE_TOKENS:
        return jsonify(error="account required — one of: " + ", ".join(PAGE_TOKENS)), 400

    session = start_session(sender_id, flow, account_id)
    disclose_once(session, sender_for(account_id))
    return jsonify(status="started", sender_id=sender_id, flow=flow, account=account_id), 200


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
        # entry.id = the Instagram account that received the message
        account_id = str(entry.get("id") or "")
        for event in entry.get("messaging", []):
            message = event.get("message") or {}
            sender_id = event.get("sender", {}).get("id")
            text = message.get("text")
            # Instagram also sends a copy ("echo") of every message the bot
            # itself sends — never treat those as a customer message.
            if message.get("is_echo") or (sender_id and sender_id == account_id):
                continue
            if PAGE_TOKENS and account_id not in PAGE_TOKENS and not PAGE_ACCESS_TOKEN:
                log.warning("Message for account %s, which has no Page token — ignored", account_id)
                continue
            if sender_id and text:
                handle_inbound(account_id, sender_id, text)

    return jsonify(status="ok"), 200


# --------------------------------------------------------------- routing --
def handle_inbound(account_id: str, sender_id: str, text: str):
    numbers = extract_numbers(text)
    send = sender_for(account_id)
    session = get_session(sender_id, account_id)

    if session is None:
        # No one called /start_flow for this lead — they messaged in on
        # their own. Figure out which flow they mean and start one,
        # equivalent to the diagram's "Did they reply? -> Yes" branch.
        flow = detect_flow(text)
        if not flow:
            send(
                sender_id,
                "🤖 You're chatting with the JUTAKU automated assistant, not a "
                "person. Tell me if you want to know about *Rent*, *Share*, "
                "or *Loan* eligibility, and I'll walk you through it.",
            )
            return
        session = start_session(sender_id, flow, account_id)
        disclose_once(session, send)
        if numbers:
            # They front-loaded the number(s) already — skip straight past
            # the "ask for it" step into the amount-handling state.
            session.state = {
                "rent": "awaiting_rent_amount",
                "share": "awaiting_xy",
                "loan": "awaiting_takehome",
            }[flow]
            HANDLERS[flow](session, text, numbers, send)
        else:
            HANDLERS[flow](session, text, numbers, send)
        return

    disclose_once(session, send)
    HANDLERS[session.flow](session, text, numbers, send)


# ------------------------------------------------------------- scheduler --
# Pinned to UTC explicitly: some hosts set TZ to a locale alias (e.g.
# "Asia/Calcutta") that tzlocal can't resolve via zoneinfo, which crashes
# APScheduler's auto-detection at import time.
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    lambda: check_timeouts(sender_for, wait_minutes=WAIT_MINUTES),
    "interval",
    minutes=1,
    id="timeout_check",
)
scheduler.start()


@app.route("/", methods=["GET"])
def health():
    return jsonify(status="jutaku-ig-bot running", open_sessions=len(SESSIONS),
                   accounts=sorted(PAGE_TOKENS) or (["default"] if PAGE_ACCESS_TOKEN else [])), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
