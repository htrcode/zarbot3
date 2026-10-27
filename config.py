"""Configuration for the invoice / ticket / sales bot.

No .env file needed
-------------------
Fill in the values in the "EDIT THESE" block below (use the Pterodactyl file
manager to edit this file on the server). If you *do* set environment
variables / Startup variables on the panel, those take priority over the
values written here (same names, e.g. ``DISCORD_BOT_TOKEN``, ``GUILD_ID``).

Channel routing
---------------
``INVOICE_CHANNEL_ID``     new invoices are posted here (and nowhere else).
``STATEMENTS_CHANNEL_ID``  Sales Statements and Expense Statements are posted here.

Do not commit your real token to a public repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

BASE_DIR = Path(__file__).resolve().parent

# ==========================================================================
# EDIT THESE
# ==========================================================================
BOT_TOKEN = ""  # paste your bot token between the quotes

# Your server id(s). One id, or several separated by commas: "123,456".
# The bot must already be IN the server. Leave "" to sync commands globally.
GUILD_ID = ""

INVOICE_CHANNEL_ID = 0  # channel id where new invoices are posted
STATEMENTS_CHANNEL_ID = 0  # channel id for Sales / Expense Statements

# --- Companies (multi-company finance) ---------------------------------------
# Every sale, expense, revenue entry and invoice is stored against one of these
# three companies (key -> name), and its statement is posted only to that
# company's own channel. A company channel may live in another server; the bot
# must be a member there with View Channel + Send Messages.
#
# Fill in the channel ids here, or as panel / environment variables:
#   COMPANY_ZARBUNEX_STATEMENT_CHANNEL_ID, COMPANY_HECTORHOST_STATEMENT_CHANNEL_ID,
#   COMPANY_MATRIXMC_STATEMENT_CHANNEL_ID
#   COMPANY_ZARBUNEX_INVOICE_CHANNEL_ID, ... (optional: invoice cards; defaults
#   to the same company's statement channel)
#   COMPANY_ZARBUNEX_NAME, ... (optional display-name override)
# Leave an id at 0 until you have the real channel: the commands then refuse to
# save instead of posting one company's money into another company's channel.
COMPANY_CONFIG = {
    "zarbunex": {"name": "Zarbunex PVT LTD", "statement_channel_id": 0, "invoice_channel_id": 0},
    "hectorhost": {"name": "HectorHost", "statement_channel_id": 0, "invoice_channel_id": 0},
    "matrixmc": {"name": "MatrixMC", "statement_channel_id": 0, "invoice_channel_id": 0},
}

# Roles allowed to use the finance / invoice / ticket / sales commands
# (server administrators always can). Role ids separated by commas: "111,222"
STAFF_ROLE_IDS = ""

BRAND_TAGLINE = ""  # small line under every card title, e.g. "Hector Hosting"
UTC_OFFSET_HOURS = 5.5  # fallback offset for dates (5.5 = India / IST)
TIMEZONE = "Asia/Kolkata"  # used for due dates, follow-ups and reports
CURRENCY = "INR"  # INR shows ₹ with Indian digit grouping

# Optional extra channel ids (comma separated) that also receive every
# invoice / payment card. Usually leave empty.
TRANSACTION_CHANNEL_IDS = ""
LOG_CHANNEL_ID = 0  # overdue-invoice alerts and general logs (optional)

# --- Tickets -------------------------------------------------------------
# Category ids (Discord: User Settings > Advanced > Developer Mode, then right-click the
# category > Copy Category ID). Leave 0 for any category you do not have.
TICKET_DEFAULT_CATEGORY_ID = 0  # "Tickets" - every new ticket is created here (falls back to Billing if 0)
TICKET_CATEGORY_ID = 0  # "Billing" category (kept as before)
TICKET_SUPPORT_CATEGORY_ID = 0  # "Technical Support" category (also a Complete Purchase target)
TICKET_PARTNERSHIP_CATEGORY_ID = 0  # "Partnership" category (also a Complete Purchase target)
TICKET_RECRUITMENT_CATEGORY_ID = 0  # "Recruitment" category
TICKET_ACTIVE_CLIENTS_CATEGORY_ID = 0  # "Active Clients" category
TICKET_OUTDATED_CLIENTS_CATEGORY_ID = 0  # "Outdated Clients" category
TICKET_FOLLOWUP_CATEGORY_ID = 0  # "Follow Up" category
TICKET_CLOSED_CATEGORY_ID = 0  # closed tickets are moved here (optional)
TICKET_STAFF_ROLE_IDS = ""  # roles that see every ticket (falls back to STAFF_ROLE_IDS)
TICKET_LOG_CHANNEL_ID = 0  # transcripts, close/reopen logs, stale-ticket digest
TICKET_PREFIX = "ticket"
TICKET_CLOSED_PREFIX = "closed"
TICKET_STALE_DAYS = 3  # tickets idle this long show up as stale
TICKET_MAX_OPEN_PER_USER = 3  # 0 = unlimited (staff are never limited)

# --- Sales pipeline ------------------------------------------------------
SALES_CHANNEL_ID = 0  # follow-up reminders (falls back to the ticket log / log channel)
FOLLOWUP_CHECK_MINUTES = 30
SALES_LOOKAHEAD_DAYS = 7

# --- Business details printed on invoice PDFs -----------------------------
BUSINESS_NAME = "My Hosting Company"
BUSINESS_TAGLINE = "Reliable hosting. Powerful solutions."
BUSINESS_EMAIL = "support@example.com"
BUSINESS_PHONE = "+91 00000 00000"
BUSINESS_ADDRESS = "City, State"
BUSINESS_TAX_ID = ""  # e.g. "GSTIN: 32ABCDE1234F1Z5"
PAYMENT_INSTRUCTIONS = "Pay via UPI: yourname@upi"
REFUND_POLICY = "Refunds are only available within 24 hours after the purchase has been made."
TERMS = "Payment is due by the due date shown|Services may be suspended for overdue invoices"
FOOTER_SERVICES = ""  # e.g. "web hosting, game server hosting and virtual machines"
BRAND_COLOR = "#6D28D9"  # accent colour on PDFs
INVOICE_PREFIX = "INV"

# Needed so ticket transcripts contain message text. Turn on
# "Message Content Intent" in the Discord Developer Portal (Bot tab).
# If it is not enabled the bot still starts, just without transcript text.
ENABLE_MESSAGE_CONTENT = True
# ==========================================================================


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _ids(name: str, fallback: Union[str, int, List[int], None]) -> List[int]:
    """List of ids: environment variable first, then the value written above."""
    raw = _env(name)
    if not raw:
        raw = fallback  # type: ignore[assignment]
    if raw in (None, "", 0):
        return []
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [int(x) for x in raw if int(x)]
    return [int(x) for x in str(raw).replace(";", ",").split(",") if x.strip().isdigit()]


def _one(name: str, fallback: Union[str, int, None]) -> Optional[int]:
    found = _ids(name, fallback)
    return found[0] if found else None


def _text(name: str, fallback: str) -> str:
    return (_env(name) or str(fallback)).strip()


def _number(name: str, fallback: float, cast=int):
    raw = _env(name)
    try:
        return cast(raw) if raw else cast(fallback)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}")


def _flag(name: str, fallback: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return bool(fallback)
    return raw not in ("0", "false", "no", "off")


def _path(value: str, *candidates: str) -> str:
    """First existing file among the configured value and the bundled defaults."""
    for candidate in (value, *candidates):
        if not candidate:
            continue
        p = Path(candidate)
        p = p if p.is_absolute() else BASE_DIR / p
        if p.exists():
            return str(p)
    return str(BASE_DIR / (candidates[-1] if candidates else value))


# ---- core -----------------------------------------------------------------
TOKEN: str = _text("DISCORD_BOT_TOKEN", "") or _text("DISCORD_TOKEN", "") or BOT_TOKEN.strip()
GUILD_IDS: List[int] = _ids("GUILD_ID", GUILD_ID)
GUILD_ID: Optional[int] = GUILD_IDS[0] if GUILD_IDS else None  # first server (kept for older code)
INVOICE_CHANNEL_ID: Optional[int] = _one("INVOICE_CHANNEL_ID", INVOICE_CHANNEL_ID)
STATEMENTS_CHANNEL_ID: Optional[int] = _one("STATEMENTS_CHANNEL_ID", STATEMENTS_CHANNEL_ID)
STAFF_ROLE_IDS: List[int] = _ids("STAFF_ROLE_IDS", STAFF_ROLE_IDS)
LOG_CHANNEL_ID: Optional[int] = _one("LOG_CHANNEL_ID", LOG_CHANNEL_ID)
TRANSACTION_CHANNEL_IDS: List[int] = _ids("TRANSACTION_CHANNEL_IDS", TRANSACTION_CHANNEL_IDS)

BRAND_TAGLINE: str = _text("BRAND_TAGLINE", BRAND_TAGLINE)
UTC_OFFSET_HOURS: float = _number("UTC_OFFSET_HOURS", UTC_OFFSET_HOURS, float)
TIMEZONE: str = _text("TIMEZONE", TIMEZONE)
CURRENCY: str = _text("CURRENCY", CURRENCY).upper()
ENABLE_MESSAGE_CONTENT: bool = _flag("ENABLE_MESSAGE_CONTENT", ENABLE_MESSAGE_CONTENT)

# ---- storage --------------------------------------------------------------
#: JSON ledger behind Sales / Expense Statements and quick invoices.
DATA_FILE: Path = Path(os.environ.get("INVOICE_BOT_DATA") or BASE_DIR / "data.json")
#: SQLite database behind itemised invoices, tickets, deals and P&L.
_db = Path(_env("DB_PATH") or "data/bot.db")
DB_PATH: str = str(_db if _db.is_absolute() else BASE_DIR / _db)

# ---- tickets --------------------------------------------------------------
TICKET_CATEGORY_ID: Optional[int] = _one("TICKET_CATEGORY_ID", TICKET_CATEGORY_ID)
TICKET_CLOSED_CATEGORY_ID: Optional[int] = _one("TICKET_CLOSED_CATEGORY_ID", TICKET_CLOSED_CATEGORY_ID)
TICKET_SUPPORT_CATEGORY_ID: Optional[int] = _one("TICKET_SUPPORT_CATEGORY_ID", TICKET_SUPPORT_CATEGORY_ID)
TICKET_PARTNERSHIP_CATEGORY_ID: Optional[int] = _one("TICKET_PARTNERSHIP_CATEGORY_ID", TICKET_PARTNERSHIP_CATEGORY_ID)
TICKET_DEFAULT_CATEGORY_ID: Optional[int] = _one("TICKET_DEFAULT_CATEGORY_ID", TICKET_DEFAULT_CATEGORY_ID)
TICKET_RECRUITMENT_CATEGORY_ID: Optional[int] = _one("TICKET_RECRUITMENT_CATEGORY_ID", TICKET_RECRUITMENT_CATEGORY_ID)
TICKET_ACTIVE_CLIENTS_CATEGORY_ID: Optional[int] = _one("TICKET_ACTIVE_CLIENTS_CATEGORY_ID", TICKET_ACTIVE_CLIENTS_CATEGORY_ID)
TICKET_OUTDATED_CLIENTS_CATEGORY_ID: Optional[int] = _one("TICKET_OUTDATED_CLIENTS_CATEGORY_ID", TICKET_OUTDATED_CLIENTS_CATEGORY_ID)
TICKET_FOLLOWUP_CATEGORY_ID: Optional[int] = _one("TICKET_FOLLOWUP_CATEGORY_ID", TICKET_FOLLOWUP_CATEGORY_ID)
# Every ticket category staff can move a ticket to: key -> (label, config variable, category id).
TICKET_CATEGORIES = {
    "tickets": ("Tickets", "TICKET_DEFAULT_CATEGORY_ID", TICKET_DEFAULT_CATEGORY_ID),
    "billing": ("Billing", "TICKET_CATEGORY_ID", TICKET_CATEGORY_ID),
    "support": ("Technical Support", "TICKET_SUPPORT_CATEGORY_ID", TICKET_SUPPORT_CATEGORY_ID),
    "partnership": ("Partnership", "TICKET_PARTNERSHIP_CATEGORY_ID", TICKET_PARTNERSHIP_CATEGORY_ID),
    "recruitment": ("Recruitment", "TICKET_RECRUITMENT_CATEGORY_ID", TICKET_RECRUITMENT_CATEGORY_ID),
    "active": ("Active Clients", "TICKET_ACTIVE_CLIENTS_CATEGORY_ID", TICKET_ACTIVE_CLIENTS_CATEGORY_ID),
    "outdated": ("Outdated Clients", "TICKET_OUTDATED_CLIENTS_CATEGORY_ID", TICKET_OUTDATED_CLIENTS_CATEGORY_ID),
    "followup": ("Follow Up", "TICKET_FOLLOWUP_CATEGORY_ID", TICKET_FOLLOWUP_CATEGORY_ID),
}
TICKET_STAFF_ROLE_IDS: List[int] = _ids("TICKET_STAFF_ROLE_IDS", TICKET_STAFF_ROLE_IDS) or STAFF_ROLE_IDS
TICKET_LOG_CHANNEL_ID: Optional[int] = _one("TICKET_LOG_CHANNEL_ID", TICKET_LOG_CHANNEL_ID) or LOG_CHANNEL_ID
TICKET_PREFIX: str = _text("TICKET_PREFIX", TICKET_PREFIX)
TICKET_CLOSED_PREFIX: str = _text("TICKET_CLOSED_PREFIX", TICKET_CLOSED_PREFIX)
TICKET_STALE_DAYS: int = _number("TICKET_STALE_DAYS", TICKET_STALE_DAYS)
TICKET_MAX_OPEN_PER_USER: int = _number("TICKET_MAX_OPEN_PER_USER", TICKET_MAX_OPEN_PER_USER)
_tdir = Path(_env("TICKET_TRANSCRIPT_DIR") or "data/transcripts")
TICKET_TRANSCRIPT_DIR: str = str(_tdir if _tdir.is_absolute() else BASE_DIR / _tdir)

# ---- sales pipeline -------------------------------------------------------
SALES_CHANNEL_ID: Optional[int] = _one("SALES_CHANNEL_ID", SALES_CHANNEL_ID) or TICKET_LOG_CHANNEL_ID or LOG_CHANNEL_ID
FOLLOWUP_CHECK_MINUTES: int = _number("FOLLOWUP_CHECK_MINUTES", FOLLOWUP_CHECK_MINUTES)
SALES_LOOKAHEAD_DAYS: int = _number("SALES_LOOKAHEAD_DAYS", SALES_LOOKAHEAD_DAYS)

# ---- invoice PDFs ---------------------------------------------------------
BUSINESS_NAME: str = _text("BUSINESS_NAME", BUSINESS_NAME)
BUSINESS_TAGLINE: str = _text("BUSINESS_TAGLINE", BUSINESS_TAGLINE)
BUSINESS_ADDRESS: str = _text("BUSINESS_ADDRESS", BUSINESS_ADDRESS)
BUSINESS_EMAIL: str = _text("BUSINESS_EMAIL", BUSINESS_EMAIL)
BUSINESS_PHONE: str = _text("BUSINESS_PHONE", BUSINESS_PHONE)
BUSINESS_TAX_ID: str = _text("BUSINESS_TAX_ID", BUSINESS_TAX_ID)
TERMS: str = _text("TERMS", TERMS)
PAYMENT_INSTRUCTIONS: str = _text("PAYMENT_INSTRUCTIONS", PAYMENT_INSTRUCTIONS)
REFUND_POLICY: str = _text("REFUND_POLICY", REFUND_POLICY)
FOOTER_SERVICES: str = _text("FOOTER_SERVICES", FOOTER_SERVICES)
BRAND_COLOR: str = _text("BRAND_COLOR", BRAND_COLOR)
INVOICE_PREFIX: str = _text("INVOICE_PREFIX", INVOICE_PREFIX)
SIGNATURE_PATH: str = _path(_env("SIGNATURE_PATH"), "assets/signature.png", "signature.png")
LOGO_PATH: str = _path(_env("LOGO_PATH"), "assets/logo.png", "logo.png")

# ---- companies ------------------------------------------------------------
@dataclass(frozen=True)
class Company:
    """One of the configured companies: a stable key, a display name and its channels."""

    key: str
    name: str
    statement_channel_id: Optional[int] = None
    invoice_channel_id: Optional[int] = None

    @property
    def statement_env(self) -> str:
        return f"COMPANY_{self.key.upper()}_STATEMENT_CHANNEL_ID"

    @property
    def invoice_env(self) -> str:
        return f"COMPANY_{self.key.upper()}_INVOICE_CHANNEL_ID"

    def channel_id(self, kind: str = "statement") -> Optional[int]:
        """Statement channel, or the invoice channel (which falls back to the statement channel)."""
        if kind == "invoice":
            return self.invoice_channel_id or self.statement_channel_id
        return self.statement_channel_id


COMPANIES: Dict[str, Company] = {
    key: Company(
        key=key,
        name=_text(f"COMPANY_{key.upper()}_NAME", cfg.get("name", key)),
        statement_channel_id=_one(f"COMPANY_{key.upper()}_STATEMENT_CHANNEL_ID",
                                  cfg.get("statement_channel_id", 0)),
        invoice_channel_id=_one(f"COMPANY_{key.upper()}_INVOICE_CHANNEL_ID",
                                cfg.get("invoice_channel_id", 0)),
    )
    for key, cfg in COMPANY_CONFIG.items()
}


def get_company(value) -> Optional[Company]:
    """Company by key (``hectorhost``) or display name (``HectorHost``); None if unknown/empty."""
    if not value:
        return None
    text = str(value).strip()
    if text.lower() in COMPANIES:
        return COMPANIES[text.lower()]
    for company in COMPANIES.values():
        if company.name.casefold() == text.casefold():
            return company
    return None


def company_name(value) -> str:
    """Display name for a stored company key (``—`` when unset, so old rows stay readable)."""
    company = get_company(value)
    if company:
        return company.name
    return str(value).strip() if value else "—"
