"""
Message counts for the CRM's Campaign Reports
---------------------------------------------
Every DM the bot receives is noted here (a small SQLite file, STATS_DB):
which Instagram account, the ad the person tapped to message (Meta's
"referral" on ad-started conversations), whether the bot answered, and
whether it looked like spam. Who sent it is stored only as a one-way hash —
no names, IDs or message text are kept.

The CRM reads totals from GET /stats?from=YYYY-MM-DD&to=YYYY-MM-DD with the
header  X-Stats-Key: <STATS_KEY>  (dates are India time).
"""

import hashlib
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("STATS_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.db"))
IST = timezone(timedelta(hours=5, minutes=30))
_lock = threading.Lock()

SPAM_PATTERNS = [
    r"https?://", r"\bwww\.", r"\b[a-z0-9-]+\.(com|net|org|io|xyz|info|biz|link|site|online|top|click)\b",
    r"\bfollowers?\b", r"\bpromot(e|ion|ing)\b", r"\bcollab", r"\bshout ?out", r"\bbrand ambassador",
    r"\bcrypto", r"\bbitcoin", r"\bforex\b", r"\btrading (signal|tips)", r"\bearn (money|\$|rs|₹|daily)",
    r"\bwork from home\b", r"\bgiveaway\b", r"\bsugar (daddy|mommy)", r"\bcheck (my|out my) (profile|page|bio)",
    r"\bverify your account", r"\bcopyright (violation|infringement)", r"\byour account (will be|has been) (disabled|suspended|deleted)",
    r"\bboost your (page|account|profile)", r"\bpaid partnership",
]
_spam_re = re.compile("|".join(SPAM_PATTERNS), re.IGNORECASE)


def is_spam(text: str) -> bool:
    return bool(text) and bool(_spam_re.search(text))


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS msg (
        at TEXT NOT NULL, account TEXT NOT NULL, who TEXT NOT NULL, ad_id TEXT,
        replied INTEGER NOT NULL DEFAULT 0, spam INTEGER NOT NULL DEFAULT 0, kind TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS msg_at ON msg(at)")
    c.execute("CREATE TABLE IF NOT EXISTS ref (account TEXT NOT NULL, who TEXT NOT NULL, ad_id TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (account, who))")
    return c


def _who(account_id: str, sender_id: str) -> str:
    return hashlib.sha256(f"{account_id}:{sender_id}".encode()).hexdigest()[:20]


def record(account_id: str, sender_id: str, ad_id=None, replied=False, spam=False, kind="text"):
    """One inbound message. A person who came from an ad keeps that ad for
    their later messages (30 days)."""
    try:
        now = datetime.now(timezone.utc)
        who = _who(account_id or "", sender_id or "")
        with _lock, _conn() as c:
            if ad_id:
                c.execute("INSERT OR REPLACE INTO ref VALUES (?,?,?,?)", (account_id or "", who, str(ad_id), now.isoformat()))
            else:
                row = c.execute("SELECT ad_id, at FROM ref WHERE account=? AND who=?", (account_id or "", who)).fetchone()
                if row and datetime.fromisoformat(row[1]) > now - timedelta(days=30):
                    ad_id = row[0]
            c.execute("INSERT INTO msg VALUES (?,?,?,?,?,?,?)",
                      (now.isoformat(), account_id or "", who, str(ad_id) if ad_id else None, 1 if replied else 0, 1 if spam else 0, kind))
    except Exception as e:  # counting must never break replying
        import logging
        logging.getLogger("jutaku-bot").error("stats record failed: %s", e)


def totals(day_from: str, day_to: str):
    """Conversations (people who messaged) between two India-time dates,
    grouped by Instagram account and ad."""
    start = datetime.strptime(day_from, "%Y-%m-%d").replace(tzinfo=IST).astimezone(timezone.utc).isoformat()
    end = (datetime.strptime(day_to, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    with _lock, _conn() as c:
        people = c.execute("""SELECT account, who, MAX(ad_id), MAX(replied), MAX(spam), COUNT(*) FROM msg
                              WHERE at >= ? AND at < ? GROUP BY account, who""", (start, end)).fetchall()
    out = {}
    for account, _who_, ad_id, replied, spam, count in people:
        key = (account, ad_id)
        r = out.setdefault(key, {"account": account, "adId": ad_id, "conversations": 0, "autoReplied": 0, "notAutoReplied": 0, "spam": 0, "messages": 0})
        r["conversations"] += 1
        r["messages"] += count
        if replied:
            r["autoReplied"] += 1
        else:
            r["notAutoReplied"] += 1
            if spam:
                r["spam"] += 1
    return list(out.values())
