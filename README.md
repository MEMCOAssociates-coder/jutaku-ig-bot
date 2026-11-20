# JUTAKU Instagram DM Automation

A full Instagram DM bot that replicates the **"JUTAKU — DM Rent & DM Share
Flows"** diagram node-for-node — DM Rent, DM Share, and DM Loan — and
replies to real leads automatically.

## What it matches from the diagram

| Diagram step                        | How the bot handles it                                             |
|--------------------------------------|----------------------------------------------------------------------|
| "DM Sent"                            | `POST /start_flow` — call this the instant you send the opening DM (from an ad click-to-DM, broadcast, or manually) |
| "Did they reply?"                    | Tracked per-lead; a background check runs every minute               |
| "No response → assume general rent"  | Fires automatically after `WAIT_MINUTES` of silence (Rent/Share only — **Loan has no fallback, it just waits**, exactly as the diagram shows) |
| "Ask: Type your exact rent" / "Ask X & Y" / "Ask take-home" | Sent the moment the lead replies |
| "^X = X × 1.1 × 1.1" / "^X = X×Y×1.1×1.1" / "44% of take-home" | `calculator.py` — the only file with the actual math |
| "^X vs ₹36,827" branch (3-way for Rent/Share, yes/no for Loan) | `flows.py` — exact copy from each coloured outcome box, including the year-by-year rent projection message |
| "Final — His/Her Share = ^X ÷ Y"     | Sent as a follow-up message at the end of the Share flow              |
| Loan "Book Exclusive Site Visit" / "NOT YET ELIGIBLE" alternatives | Sent verbatim as the final message of the Loan flow |

If someone messages in on their own (no `/start_flow` call was made) and says
something like "rent 28000", the bot detects the flow from the keyword,
treats their message as having already "replied," and — since they
front-loaded the number — skips straight to the computed answer instead of
asking for it again.

## Files

- `app.py` — Flask webhook server, routing, and the once-a-minute timeout
  scheduler.
- `flows.py` — the three state machines, with every message worded to match
  the diagram's boxes.
- `calculator.py` — the formulas only. Change a number here if a formula or
  the ₹36,827 benchmark ever changes.

## What you need (you said this part is already done)

- A Meta Developer App with the **Instagram Messaging** product added.
- An Instagram **Business/Creator** account linked to a Facebook Page — or
  several (the bot can answer more than one Instagram account; each replies
  from its own account).
- Each linked **Facebook Page's access token** (it starts with `EAA…`; replies
  go through `graph.facebook.com`). Get it in Graph API Explorer: Generate
  Access Token with `instagram_basic`, `instagram_manage_messages`,
  `pages_show_list`, `pages_manage_metadata`, `pages_read_engagement` →
  extend it in the Access Token Debugger → `GET me/accounts?fields=name,id,
  access_token,instagram_business_account` → copy each Page's
  `access_token`. Check it in the debugger: Type **Page**, Expires **Never**.
  (The token from *Instagram → API setup → Generate token* is an Instagram
  user token for `graph.instagram.com` and does NOT work with this bot.)
- In each Instagram app: Settings → Messages and story replies → Message
  controls → Connected tools → **Allow access to messages** on.

## Setup steps

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables** — copy `.env.example` to `.env` and
   fill in:
   - `VERIFY_TOKEN` — any string you make up (Meta echoes it back during setup)
   - `PAGE_TOKENS` — one `<Instagram account ID>:<Page token>` pair per
     account, comma-separated (the Instagram account ID is the
     `instagram_business_account.id` from `me/accounts`). With a single
     account you can use `PAGE_ACCESS_TOKEN` instead.
   - `APP_SECRET` — from Meta App Dashboard → App Settings → Basic (recommended)
   - `ADMIN_TOKEN` — any string you make up, protects `/start_flow`
   - `WAIT_MINUTES` — how long to wait for a reply before the fallback fires (default 60)

3. **Run it locally to test:**
   ```bash
   export $(cat .env | xargs)
   python app.py
   ```
   Starts on `http://localhost:5000`.

4. **Expose it to the internet.** Meta requires a public HTTPS URL.
   - Quick test: `ngrok http 5000`, use the `https://...ngrok...` URL it gives you.
   - Permanent: deploy to Render, Railway, Fly.io, a VPS, AWS/GCP, etc.
     (tell me which one and I'll walk you through it).

5. **Register the webhook in the Meta App Dashboard:**
   - App → **Webhooks** → **Instagram** → Subscribe.
   - Callback URL: `https://<your-domain>/webhook`
   - Verify token: your `VERIFY_TOKEN`.
   - Subscribe to the `messages` field.

6. **Kick off a tracked lead** (optional — only needed if you want the "Did
   they reply?" / no-response-fallback behaviour tied to an outbound DM you
   send via ads or a broadcast tool):
   ```bash
   curl -X POST https://<your-domain>/start_flow \
     -H "X-Admin-Token: <ADMIN_TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"sender_id": "<IGSID>", "flow": "rent", "account": "<Instagram account ID>"}'
   ```
   `account` = the Instagram account the opening DM went out from (required
   when the bot answers more than one account).

7. **Test it** — DM your Instagram account "rent 25000" from another
   account. You should get the bot disclosure, the year-by-year projection,
   and the matching outcome message within a couple of seconds.

## Compliance note (required by Meta, already built in)

Every new conversation gets one disclosure message up front —
*"You're chatting with the JUTAKU automated assistant, not a person."*
Meta requires this for any automated chat experience. It's handled by
`disclose_once()` in `flows.py`; you don't need to do anything extra.

## Limitations to know about

- **State is in memory** (`SESSIONS` dict in `flows.py`). Fine for testing,
  but it resets on restart and won't work across multiple server instances.
  For real production traffic, swap it for Redis or a small database table
  — say the word and I'll wire that in.
- **The "Did they reply?" timer** only exists for leads you explicitly
  started with `/start_flow`. A self-initiated DM has no "sent" timestamp to
  measure against, so it's treated as an immediate reply.
- `gunicorn` is in `requirements.txt` for production, e.g. `gunicorn app:app`.
  Note: APScheduler's background thread only runs reliably with a single
  worker process (`gunicorn -w 1 app:app`) unless you move the scheduler to
  a separate process — ask me if you want that set up for higher traffic.
