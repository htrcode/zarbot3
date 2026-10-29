"""Glue between the SQLite desk (itemised invoices, tickets, finance) and the statement ledger.

* Every payment recorded on an itemised invoice becomes a **Sales Statement**
  card, and every new itemised invoice is announced — both only in the
  destination channel of the invoice's **company**.
* Sales / expenses recorded with ``/sale add``, ``/expense add``, ``/revenue``
  or the quick invoice "Mark as paid" flow are mirrored into the SQLite books
  (under the same company) so the finance dashboard and P&L include them.
* ``/finance income`` and ``/finance expense`` entries are mirrored back into
  the ledger and posted as statements, again per company.

Company routing lives in :func:`post_company`: it resolves the configured
channel id, reports missing channels / permissions / Discord API errors to the
caller and never falls back to another company's channel.
"""
import logging

import discord

import config
import data as store
import ui
from desk.database import primary_service, ticket_number
from desk.utils import dmy, fmt, to_cents

log = logging.getLogger("invoice-bot.bridge")


# ---------------------------------------------------------------------------
# posting
# ---------------------------------------------------------------------------
async def _channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except discord.HTTPException:
        log.warning("Channel %s is missing or not visible; skipping post.", channel_id)
        return None


async def post_to(bot, primary_id, view_factory) -> int:
    """Send a freshly built card to ``primary_id`` plus any TRANSACTION_CHANNEL_IDS."""
    targets = [c for c in dict.fromkeys([primary_id, *config.TRANSACTION_CHANNEL_IDS]) if c]
    if not targets:
        log.warning("No channel configured for this card (set STATEMENTS_CHANNEL_ID / INVOICE_CHANNEL_ID).")
        return 0
    sent = 0
    for channel_id in targets:
        channel = await _channel(bot, channel_id)
        if channel is None:
            continue
        try:
            await channel.send(view=view_factory())
            sent += 1
        except discord.HTTPException:
            log.exception("Could not post to channel %s", channel_id)
    return sent


async def post_company(bot, company_key, view, *, kind: str = "statement") -> tuple[bool, str]:
    """Post ``view`` to a company's own channel only.

    Returns ``(ok, message)``: on success ``message`` is the channel mention, on
    failure it is a short, user-safe explanation (unset channel, missing channel
    or insufficient permissions, Discord API error).
    """
    company = config.get_company(company_key)
    if company is None:
        return False, "That record has no company yet, so there is no channel to post to."
    channel_id = company.channel_id(kind)
    env_name = company.statement_env if kind == "statement" else company.invoice_env
    if not channel_id:
        return False, (f"No channel is configured for **{company.name}**. "
                       f"Set `{env_name}` to that channel's id, then try again.")
    channel = await ui.resolve_channel(bot, channel_id)
    if channel is None:
        return False, (f"I could not open channel `{channel_id}` for **{company.name}**. "
                       f"Check `{env_name}` and give me **View Channel** there.")
    try:
        await channel.send(view=view)
    except discord.HTTPException as exc:
        log.warning("Could not post to %s (%s): %s", channel_id, company.name, exc)
        return False, (f"I could not send to <#{channel_id}> for **{company.name}** — "
                       "check my **Send Messages** permission there.")
    return True, f"<#{channel_id}>"


# ---------------------------------------------------------------------------
# invoice events -> cards
# ---------------------------------------------------------------------------
def _contact(inv: dict) -> str:
    if inv.get("client_contact"):
        return inv["client_contact"]
    return f"<@{inv['client_discord_id']}>" if inv.get("client_discord_id") else ""


def _service_label(item: dict) -> str:
    label = " · ".join(x for x in (item.get("product_type"), item.get("plan")) if x)
    return label or item.get("description") or ""


def invoice_card(inv: dict, ticket_label):
    item = primary_service(inv)
    f = ui._field
    lines = [
        f("Company", config.company_name(inv.get("company_id"))),
        f("Client Name", ui._val(inv["client_name"])),
        f("Client Contact", ui._val(_contact(inv))),
        f("Product", ui._val(item.get("product_type"))),
        f("Server Name", ui._val(item.get("service_name") or item.get("description"))),
        f("Node", ui._val(item.get("node"))),
        f("Server ID", ui._val(item.get("server_id"), code=True)),
        f("Plan", ui._val(item.get("plan"))),
        f("Amount", fmt(inv["total"], inv["currency"])),
        f("Due Date", dmy(inv["due_date"])),
        f("Invoice", inv["number"]),
        f("Ticket", ticket_label or "—"),
    ]
    items = ui.title_and_body(f"New Invoice · {inv['number']}", "\n".join(lines))
    ui.footer_items(items, f"Raised by <@{inv['created_by']}> · {dmy(inv['issue_date'])}")
    view = ui.PersistentView(timeout=None)
    view.add_item(discord.ui.Container(*items))
    return view


def sale_from_payment(inv: dict, payment: dict) -> dict:
    """Ledger fields for an invoice payment (the invoice's company is carried over)."""
    item = primary_service(inv)
    remarks = [inv["number"]]
    if payment["paid_at"] > inv["due_date"]:
        remarks.append("Late renewal")
    if payment.get("note"):
        remarks.append(payment["note"])
    return dict(
        client_name=inv["client_name"],
        client_contact=_contact(inv),
        server_name=item.get("service_name") or item.get("description") or "",
        server_uuid=item.get("server_id") or "",
        service=_service_label(item),
        amount=payment["amount"] / 100,
        method=payment.get("method") or "manual",
        transaction_id=payment.get("transaction_id") or "",
        remarks=" · ".join(remarks),
        date=payment["paid_at"],
        recorded_by=f"<@{payment['created_by']}>",
        company=inv.get("company_id") or "",
        extra={"source": "desk", "ref": f"desk-payment:{payment['id']}", "invoice": inv["number"]},
    )


async def post_transaction_event(bot, event: str, payload: dict):
    """Database event listener: invoice created / payment received."""
    if event not in ("invoice.created", "payment"):
        return
    inv = await bot.db.get_invoice(payload["invoice_id"])
    if not inv:
        return
    if event == "invoice.created":
        ticket = None
        if inv.get("ticket_channel_id"):
            ticket = await bot.db.ticket_by_channel(inv["ticket_channel_id"])
        label = ticket_number(ticket["id"]) if ticket else None
        if config.get_company(inv.get("company_id")) is None:
            # Invoices created before multi-company support keep the old routing.
            await post_to(bot, config.INVOICE_CHANNEL_ID, lambda: invoice_card(inv, label))
            return
        ok, message = await post_company(bot, inv["company_id"], invoice_card(inv, label), kind="invoice")
        if not ok:
            log.warning("Invoice %s was not posted: %s", inv["number"], message)
        return

    payment = next((p for p in inv["payments"] if p["id"] == payload.get("payment_id")), None)
    if not payment:
        return
    ref = f"desk-payment:{payment['id']}"
    if store.has_ref(ref):
        return
    sale = store.add_payment(**sale_from_payment(inv, payment))
    company = config.get_company(sale.get("company"))
    total = store.total_sales(company=sale.get("company") or None)
    if company is None:
        await post_to(bot, config.STATEMENTS_CHANNEL_ID, lambda: ui.sale_card(sale, total))
        return
    ok, message = await post_company(bot, company.key, ui.sale_card(sale, total))
    if not ok:
        log.warning("Sales statement %s was not posted: %s", inv["number"], message)


# ---------------------------------------------------------------------------
# ledger -> SQLite books
# ---------------------------------------------------------------------------
async def mirror_sale(bot, sale: dict, actor_id: int):
    """Make a ledger sale count as income in the SQLite books (dashboard / P&L)."""
    db = getattr(bot, "db", None)
    if db is None or sale.get("source") == "desk":
        return
    try:
        await db.ensure_category("Hosting Revenue", "income")
        text = f"Sale: {sale.get('client_name') or '—'} — {sale.get('service') or 'service'}"
        tx_id = await db.add_transaction(actor_id, "income", "Hosting Revenue", to_cents(sale["amount"]),
                                         text, store._entry_date(sale),
                                         company_id=sale.get("company") or None)
        # Link the two books so voiding either side cancels the statement entry too.
        if tx_id and sale.get("id"):
            store.set_ref("payment", sale["id"], f"tx:{tx_id}")
        return tx_id
    except Exception:
        log.exception("Could not mirror the sale into the finance books")


async def mirror_expense(bot, expense: dict, actor_id: int):
    db = getattr(bot, "db", None)
    if db is None or expense.get("source") == "finance":
        return
    try:
        category = expense.get("category") or "Other Expenses"
        await db.ensure_category(category, "expense")
        text = f"{expense.get('description') or category} — {expense.get('paid_to') or ''}".strip(" —")
        tx_id = await db.add_transaction(actor_id, "expense", category, to_cents(expense["amount"]),
                                         text, str(expense.get("date", ""))[:10],
                                         company_id=expense.get("company") or None)
        if tx_id and expense.get("id"):
            store.set_ref("expense", expense["id"], f"tx:{tx_id}")
        return tx_id
    except Exception:
        log.exception("Could not mirror the expense into the finance books")


# ---------------------------------------------------------------------------
# SQLite finance commands -> ledger + statements
# ---------------------------------------------------------------------------
async def record_finance(bot, kind: str, category: str, amount_cents: int, description: str,
                         date: str, actor_name: str, company_id: str | None = None,
                         txn_id: int | None = None):
    """Mirror ``/finance income|expense`` into the ledger and post its statement.

    Returns ``(ok, message)`` from :func:`post_company`.
    """
    amount = amount_cents / 100
    if kind == "income":
        sale = store.add_payment(
            client_name="", client_contact="", server_name="", server_uuid="", service=category,
            amount=amount, method="other", transaction_id="", remarks=description, date=date,
            recorded_by=actor_name, company=company_id or "", extra={"source": "finance"})
        if txn_id and sale.get("id"):
            store.set_ref("payment", sale["id"], f"tx:{txn_id}")
        if config.get_company(sale.get("company")) is None:
            if not config.STATEMENTS_CHANNEL_ID:
                return False, "no company was given and `STATEMENTS_CHANNEL_ID` is not set."
            await post_to(bot, config.STATEMENTS_CHANNEL_ID,
                          lambda: ui.sale_card(sale, store.total_sales()))
            return True, f"<#{config.STATEMENTS_CHANNEL_ID}>"
        return await post_company(
            bot, sale["company"], ui.sale_card(sale, store.total_sales(company=sale["company"])))
    expense = store.add_expense(
        description=description or category, category=category, amount=amount, method="other",
        paid_to="", transaction_id="", remarks="", date=date, recorded_by=actor_name,
        company=company_id or "", extra={"source": "finance"})
    if txn_id and expense.get("id"):
        store.set_ref("expense", expense["id"], f"tx:{txn_id}")
    if config.get_company(expense.get("company")) is None:
        if not config.STATEMENTS_CHANNEL_ID:
            return False, "no company was given and `STATEMENTS_CHANNEL_ID` is not set."
        await post_to(bot, config.STATEMENTS_CHANNEL_ID,
                      lambda: ui.expense_card(expense, store.total_expenses()))
        return True, f"<#{config.STATEMENTS_CHANNEL_ID}>"
    return await post_company(
        bot, expense["company"], ui.expense_card(expense, store.total_expenses(company=expense["company"])))
