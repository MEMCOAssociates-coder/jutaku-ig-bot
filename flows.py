"""
State machines for the three JUTAKU DM flows, built to match the
"JUTAKU — DM Rent & DM Share Flows" diagram node-for-node:

  DM RENT FLOW  : DM Sent -> Did they reply? -> Ask exact rent ->
                  Rent(X) received? -> compute -> branch vs 36,827 -> end
  DM SHARE FLOW : DM Sent -> Did they reply? -> Ask X & Y ->
                  X&Y received? -> compute -> branch vs 36,827 ->
                  final per-person share -> end
  DM LOAN FLOW  : DM Sent -> Did they reply? -> Ask take-home ->
                  Take-home received? -> 44% vs 36,827 -> eligible? -> end

Only Rent and Share have a "no response -> assume a default and carry on"
fallback in the diagram. Loan has none — it just keeps waiting, so this
code never force-progresses a loan conversation.
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from calculator import calculate_rent, calculate_share, calculate_loan

BENCHMARK = 36827
DEFAULT_RENT = 30000
DEFAULT_SHARERS = 2

# How long to wait for a reply before the "no response" fallback fires.
# The diagram just says "No response -> assume general rent" without a
# number attached, so this is the one knob you'll want to tune for your
# actual audience. Configurable via WAIT_MINUTES in app.py / .env.
DEFAULT_WAIT_MINUTES = 60


# ---------------------------------------------------------------- session --
@dataclass
class Session:
    sender_id: str
    flow: str  # "rent" | "share" | "loan"
    state: str  # see STATES per flow below
    account_id: str = ""  # the Instagram account (IG business ID) the conversation is on
    data: dict = field(default_factory=dict)
    last_update: float = field(default_factory=time.time)
    disclosed: bool = False

    def touch(self):
        self.last_update = time.time()


# In-memory store: (account_id, sender_id) -> Session. One conversation per
# person per Instagram account — the same customer messaging both accounts
# gets two separate conversations. Swap for Redis/a DB in production (see
# README) so state survives restarts and works across many workers.
SESSIONS: dict[tuple, Session] = {}


def get_session(sender_id: str, account_id: str = "") -> Optional[Session]:
    return SESSIONS.get((account_id, sender_id))


def start_session(sender_id: str, flow: str, account_id: str = "") -> Session:
    """Call this the moment the opening 'DM Sent' message goes out, to
    begin tracking the 'Did they reply?' timer for that lead."""
    s = Session(sender_id=sender_id, flow=flow, state="awaiting_first_reply", account_id=account_id)
    SESSIONS[(account_id, sender_id)] = s
    return s


def end_session(sender_id: str, account_id: str = ""):
    SESSIONS.pop((account_id, sender_id), None)


# ------------------------------------------------------------- messaging --
SendFn = Callable[[str, str], None]  # (sender_id, text) -> None

BOT_DISCLOSURE = (
    "🤖 You're chatting with the JUTAKU automated assistant, not a person."
)


def disclose_once(session: Session, send: SendFn):
    if not session.disclosed:
        send(session.sender_id, BOT_DISCLOSURE)
        session.disclosed = True


# ====================================================================
# DM RENT FLOW
# ====================================================================

RENT_ASK_EXACT = "Type your exact rent to know the exact value 🙂"

RENT_NO_RESPONSE_NOTE = (
    "No worries — I'll use a typical Bengaluru rent of ₹30,000 for your estimate:"
)


def _rent_year_by_year(x: float, x_hat: float) -> str:
    return (
        "📈 Do you know how your rent increases year on year?\n"
        "Bengaluru rent rises ~10% every year — here's yours, year by year, "
        "till May 2029:\n"
        f"• Today: ₹{x:,.0f}\n"
        f"• Next year: ₹{x * 1.1:,.0f}\n"
        f"• By possession (May 2029): ₹{x:,.0f} × 1.1 × 1.1 = ₹{x_hat:,.0f}\n\n"
        "This will be your rent by 2029."
    )


def _rent_outcome(x_hat: float) -> str:
    if x_hat > BENCHMARK:
        return (
            '💰 "Save the Difference. Own Your Home."\n'
            "Rent builds your advance amount — enjoy a lower EMI and save "
            "the monthly difference."
        )
    if x_hat == BENCHMARK:
        return (
            '🏠 "No Extra Burden. Just Ownership."\n'
            "Rent paid becomes your advance — move to ownership, no added "
            "monthly difference."
        )
    return (
        '🏡 "Pay a Little Extra. Own Your Home."\n'
        "Rent paid until possession contributes to ownership — pay just "
        "the difference between rent & EMI."
    )


def _finish_rent(session: Session, x: float, send: SendFn):
    result = calculate_rent(x)
    send(session.sender_id, _rent_year_by_year(x, result.x_hat))
    send(session.sender_id, _rent_outcome(result.x_hat))
    end_session(session.sender_id, session.account_id)


def handle_rent_reply(session: Session, text: str, numbers: list, send: SendFn):
    """Any inbound message while a rent session is open."""
    session.touch()

    if session.state == "awaiting_first_reply":
        # "Did they reply?" -> Yes -> ask for exact rent
        session.state = "awaiting_rent_amount"
        send(session.sender_id, RENT_ASK_EXACT)
        return

    if session.state == "awaiting_rent_amount":
        if numbers:
            _finish_rent(session, numbers[0], send)
        else:
            send(session.sender_id, "What's the number? e.g. 25000")


def rent_timeout_fallback(session: Session, send: SendFn):
    """'No response -> Assume general rent X = 30,000' from the diagram.
    Fires from either waiting state once DEFAULT_WAIT_MINUTES has passed."""
    send(session.sender_id, RENT_NO_RESPONSE_NOTE)
    _finish_rent(session, DEFAULT_RENT, send)


# ====================================================================
# DM SHARE FLOW
# ====================================================================

SHARE_ASK_XY = (
    "How many people are you sharing with (Y)? What's your monthly rent (X)? "
    "You can send both numbers, e.g. \"25000, 2\"."
)

SHARE_NO_RESPONSE_NOTE = (
    "No worries — I'll use a typical shared setup (₹30,000 rent, 2 sharers) "
    "for your estimate:"
)


def _share_year_by_year(x: float, y: float, x_hat: float) -> str:
    return (
        "📈 Do you know how your shared rent increases year on year?\n"
        "Bengaluru rent rises ~10% every year — here's your household's "
        "combined rent, year by year, till May 2029:\n"
        f"• Today: ₹{x * y:,.0f}\n"
        f"• Next year: ₹{x * y * 1.1:,.0f}\n"
        f"• By possession (May 2029): ₹{x:,.0f} × {y:g} × 1.1 × 1.1 = ₹{x_hat:,.0f}\n\n"
        "This will be your household's rent by 2029."
    )


def _share_outcome(x_hat: float) -> str:
    if x_hat > BENCHMARK:
        return (
            '💰 "Save the Difference. Own It Together."\n'
            "Combined rent builds your advance — lower EMI, savings split "
            "across the household."
        )
    if x_hat == BENCHMARK:
        return (
            '🏠 "No Extra Burden. Just Ownership."\n'
            "Combined rent becomes your advance — move to ownership, no "
            "added difference, shared by all."
        )
    return (
        '🏡 "Pay a Little Extra. Own It Together."\n'
        "Combined rent until possession contributes to ownership — pay "
        "just the difference, split between sharers."
    )


def _finish_share(session: Session, x: float, y: float, send: SendFn):
    result = calculate_share(x, y)
    send(session.sender_id, _share_year_by_year(x, y, result.x_hat))
    send(session.sender_id, _share_outcome(result.x_hat))
    send(
        session.sender_id,
        f"👤 Final — His/Her Share = ₹{result.final_share:,.0f}\n"
        "What each flatmate individually puts toward ownership.",
    )
    end_session(session.sender_id, session.account_id)


def handle_share_reply(session: Session, text: str, numbers: list, send: SendFn):
    session.touch()

    if session.state == "awaiting_first_reply":
        session.state = "awaiting_xy"
        send(session.sender_id, SHARE_ASK_XY)
        return

    if session.state == "awaiting_xy":
        pending = session.data.setdefault("numbers", [])
        pending.extend(numbers)
        if len(pending) >= 2:
            x, y = pending[0], pending[1]
            _finish_share(session, x, y, send)
        elif len(pending) == 1:
            send(session.sender_id, "Got it. And how many people are sharing (Y)?")
        else:
            send(session.sender_id, SHARE_ASK_XY)


def share_timeout_fallback(session: Session, send: SendFn):
    send(session.sender_id, SHARE_NO_RESPONSE_NOTE)
    _finish_share(session, DEFAULT_RENT, DEFAULT_SHARERS, send)


# ====================================================================
# DM LOAN FLOW
# ====================================================================

LOAN_ASK_TAKEHOME = (
    "Want to know your loan eligibility? Share your monthly take-home 💼"
)

LOAN_ELIGIBLE_MSG = (
    "🎉 YES — Eligible! Congratulations!\n"
    "You're on track for ownership. Let's book your exclusive site visit:\n"
    "📍 JUTAKU Experience Centre, Singhasandra, Bengaluru"
)

LOAN_NOT_ELIGIBLE_MSG = (
    "↩️ NOT YET ELIGIBLE — but the journey doesn't end here.\n"
    "Explore alternative ownership plans (Own 500 sq.ft / Experience "
    "50,000 sq.ft) and book a site visit to see your options."
)


def _finish_loan(session: Session, take_home: float, send: SendFn):
    result = calculate_loan(take_home)
    send(
        session.sender_id,
        f"44% of your monthly take-home is ₹{result.eligible_amount:,.0f}.",
    )
    send(
        session.sender_id,
        LOAN_ELIGIBLE_MSG if result.meets_benchmark else LOAN_NOT_ELIGIBLE_MSG,
    )
    end_session(session.sender_id, session.account_id)


def handle_loan_reply(session: Session, text: str, numbers: list, send: SendFn):
    session.touch()

    if session.state == "awaiting_first_reply":
        # "Did they reply?" -> Yes -> ask for take-home
        session.state = "awaiting_takehome"
        send(session.sender_id, LOAN_ASK_TAKEHOME)
        return

    if session.state == "awaiting_takehome":
        if numbers:
            _finish_loan(session, numbers[0], send)
        else:
            send(session.sender_id, "What's the number? e.g. 90000")
    # NOTE: no timeout fallback for loan — the diagram shows "No input ->
    # waits" / "No reply -> waits" with no auto-default, so the session is
    # simply left open until the person answers. See loan_timeout_fallback.


def loan_timeout_fallback(session: Session, send: SendFn):
    """Per the diagram, loan has no fallback default — it just waits.
    This function intentionally does nothing but exists so the scheduler
    has one consistent interface across all three flows; it's a no-op
    placeholder in case you later want a gentle nudge message here."""
    pass


# ---------------------------------------------------------- dispatch table --
HANDLERS = {
    "rent": handle_rent_reply,
    "share": handle_share_reply,
    "loan": handle_loan_reply,
}

TIMEOUT_HANDLERS = {
    "rent": rent_timeout_fallback,
    "share": share_timeout_fallback,
    "loan": loan_timeout_fallback,
}


def check_timeouts(send_for: Callable[[str], SendFn], wait_minutes: float = DEFAULT_WAIT_MINUTES):
    """Call this periodically (see app.py's scheduler). Finds every session
    that's been waiting longer than wait_minutes and fires that flow's
    'no response' behaviour, exactly matching the diagram's timeout
    branches. send_for(account_id) gives the send function for the
    Instagram account the conversation is on, so the reply comes from it."""
    cutoff = time.time() - wait_minutes * 60
    stale = [
        s
        for s in list(SESSIONS.values())
        if s.flow in ("rent", "share")  # loan has no timeout fallback — see above
        and s.state in ("awaiting_first_reply", "awaiting_rent_amount", "awaiting_xy")
        and s.last_update < cutoff
    ]
    for session in stale:
        TIMEOUT_HANDLERS[session.flow](session, send_for(session.account_id))
