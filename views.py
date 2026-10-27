"""Components V2 cards and button panels for invoices, tickets, deals and finance.

Every card follows the bot's flat design (see ``ui.py``): a ``##`` title, a
divider, ``**Label:** value`` fields, a divider, a small ``-#`` footer and the
button rows at the bottom. No accent colour strips, no plain-text replies.
"""
import asyncio
import io
import logging

import discord

import ui
from desk.database import invoice_number, primary_service, ticket_number
from desk.pdf_gen import invoice_pdf, pnl_pdf
import config
from desk.utils import (STAGE_META, TICKET_PRIORITIES, days_since, dmy, fmt, is_staff, is_ticket_staff,
                        parse_date, shift_month, to_cents)

log = logging.getLogger("invoice-bot.views")

STATUS_STYLE = {
    "paid": ("✅ Paid", 0),
    "partial": ("🟡 Partially paid", 0),
    "unpaid": ("🔵 Unpaid", 0),
    "overdue": ("🔴 Overdue", 0),
    "void": ("⚫ Void", 0),
}


async def make_pdf_file(inv: dict) -> discord.File:
    data = await asyncio.to_thread(invoice_pdf, inv)
    return discord.File(io.BytesIO(data), filename=f"{inv['number']}.pdf")


def _clip(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n*…truncated*"


def _esc(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def _f(label: str, value) -> str:
    text = str(value).strip() if value not in (None, "") else "—"
    return f"**{label}:** {text}"


def _plain(cents: int) -> str:
    """70000 -> '700', 125050 -> '1250.5' (for pre-filling form fields)."""
    text = f"{cents / 100:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _who(user_id) -> str:
    return f"<@{user_id}>" if user_id else "—"


def simple_card(title: str, body: str, color=None) -> ui.PersistentView:
    """Plain flat card used for lists and confirmations (``color`` is ignored: no accent strips)."""
    return ui.build_card(title, _clip(body) or "Nothing to show.", timeout=180)


async def _pdf_message(channel_or_followup, inv: dict, *, ephemeral: bool = False):
    """Send the invoice PDF as a V2 card with the file embedded."""
    file = await make_pdf_file(inv)
    card = ui.file_card(f"Invoice PDF · {inv['number']}",
                        ui._field("Client Name", _esc(inv["client_name"])) + "\n"
                        + ui._field("Total", fmt(inv["total"], inv["currency"])),
                        file.filename)
    kwargs = {"view": card, "file": file}
    if ephemeral:
        kwargs["ephemeral"] = True
    await channel_or_followup.send(**kwargs)


# ---------------------------------------------------------------------------
# Invoice card
# ---------------------------------------------------------------------------
def invoice_blocks(inv: dict) -> tuple[str, str, str]:
    cur = inv["currency"]
    label, _ = STATUS_STYLE[inv["status"]]
    if inv["status"] == "overdue" and inv["is_partial"]:
        label += " (partially paid)"
    client = _esc(inv["client_name"]) + (f" ({_who(inv['client_discord_id'])})" if inv["client_discord_id"] else "")
    fields = "\n".join([
        _f("Client Name", client),
        _f("Client Contact", _esc(inv.get("client_contact") or "") or None),
        _f("Status", label),
        _f("Issued", dmy(inv["issue_date"])),
        _f("Due Date", dmy(inv["due_date"])),
        _f("Category", inv["category"]),
    ])
    lines = []
    for it in inv["items"][:12]:
        line = f"`{it['quantity']:g}×` {_esc(it['description'][:60])} — **{fmt(it['line_total'], cur)}**"
        meta = " · ".join(x for x in (it.get("product_type"), it.get("plan"), it.get("node"),
                                       "link ready" if it.get("delivery_link") else "") if x)
        if meta:
            line += f"\n> {_esc(meta)}"
        lines.append(line)
    if len(inv["items"]) > 12:
        lines.append(f"*…and {len(inv['items']) - 12} more (see the PDF)*")
    items = "\n".join(lines)
    tot = [_f("Subtotal", fmt(inv["subtotal"], cur))]
    if inv["discount"]:
        tot.append(_f("Discount", f"-{fmt(inv['discount'], cur)}"))
    if inv["tax_percent"]:
        tot.append(_f(f"Tax ({inv['tax_percent']:g}%)", fmt(inv["tax"], cur)))
    tot.append(_f("Total", fmt(inv["total"], cur)))
    tot.append(_f("Paid", fmt(inv["paid"], cur)))
    tot.append(_f("Balance", fmt(inv["balance"], cur)))
    return fields, _clip(items, 2200), "\n".join(tot)


async def send_invoice_to_channel(bot, channel, inv: dict):
    """Post the invoice card and the PDF into a ticket channel/thread."""
    await channel.send(view=InvoiceView(bot, inv["id"], inv))
    await _pdf_message(channel, inv)


# ---------------------------------------------------------------------------
# Digital delivery (panel links, bot files, plugins, VPS access)
# ---------------------------------------------------------------------------
def delivery_items(inv: dict) -> list[dict]:
    """Items that describe a digital product (or carry a delivery link)."""
    return [it for it in inv["items"]
            if it.get("delivery_link") or it.get("product_type") or it.get("service_name")]


def delivery_text(inv: dict) -> str:
    lines = []
    for it in delivery_items(inv):
        meta = " · ".join(x for x in (it.get("product_type"), it.get("plan"),
                                       f"Node: {it['node']}" if it.get("node") else "",
                                       f"ID: {it['server_id']}" if it.get("server_id") else "") if x)
        head = f"**{_esc(it['description'][:80])}**" + (f"\n{_esc(meta)}" if meta else "")
        if it.get("delivery_link"):
            # Do not markdown-escape URLs: underscores/asterisks in a link must stay intact.
            url = discord.utils.escape_mentions(it["delivery_link"][:300])
            head += f"\n🔗 {url}"
        else:
            head += "\n*No delivery link saved for this item.*"
        lines.append(head)
    return "\n\n".join(lines)


async def deliver_invoice(bot, inv: dict, channel, actor_id: int = 0) -> int:
    """Post the saved panel/file links and mark the invoice delivered."""
    items = delivery_items(inv)
    if not any(it.get("delivery_link") for it in items):
        raise ValueError("No delivery links saved. Add `link=https://...` to an invoice item first.")
    head = "\n".join([_f("Client Name", _esc(inv["client_name"])),
                      _f("Invoice", inv["number"]),
                      _f("Total", fmt(inv["total"], inv["currency"]))])
    body = delivery_text(inv)
    if len(body) > 3000:
        body = body[:3000].rsplit("\n", 1)[0] + "\n*…see the PDF for the full item list.*"
    view = ui.build_card(f"Digital Delivery · {inv['number']}", head, body,
                         footer="Links are shown only to people with access to this channel.")
    await channel.send(view=view)
    await bot.db.mark_delivered(actor_id, inv["id"], f"Delivered {len(items)} item(s)")
    return len(items)


async def refresh_invoice(bot, interaction: discord.Interaction, invoice_id: int, note: str):
    inv = await bot.db.get_invoice(invoice_id)
    try:
        await interaction.response.edit_message(view=InvoiceView(bot, invoice_id, inv))
        await ui.say(interaction, note)
    except (discord.HTTPException, discord.InteractionResponded, AttributeError):
        await ui.say(interaction, note)


class PaymentModal(discord.ui.Modal, title="Record payment"):
    amount = discord.ui.TextInput(label="Amount received (₹)", placeholder="250", max_length=15)
    method = discord.ui.TextInput(label="Payment method", default="UPI",
                                  placeholder="UPI / Bank transfer / Card / Cash", max_length=40)
    transaction_id = discord.ui.TextInput(label="Transaction ID", required=False, max_length=100)
    note = discord.ui.TextInput(label="Remarks (optional)", required=False, max_length=200)

    def __init__(self, bot, invoice_id: int, default_amount: str = ""):
        super().__init__()
        self.bot, self.invoice_id = bot, invoice_id
        self.amount.default = default_amount or None

    async def on_submit(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await ui.say(interaction, "🚫 Staff only.")
        try:
            await self.bot.db.add_payment(
                interaction.user.id, self.invoice_id, to_cents(self.amount.value),
                self.method.value or "manual", self.note.value or "", self.transaction_id.value or "")
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await refresh_invoice(self.bot, interaction, self.invoice_id, "💰 Payment recorded.")


class VoidModal(discord.ui.Modal, title="Void invoice"):
    reason = discord.ui.TextInput(label="Reason", max_length=200)

    def __init__(self, bot, invoice_id: int):
        super().__init__()
        self.bot, self.invoice_id = bot, invoice_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await ui.say(interaction, "🚫 Staff only.")
        try:
            await self.bot.db.void_invoice(interaction.user.id, self.invoice_id, self.reason.value)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await refresh_invoice(self.bot, interaction, self.invoice_id, "⚫ Invoice voided.")


class InvoiceView(discord.ui.LayoutView):
    """Persistent: buttons use custom_ids like inv:pdf:12, re-registered on startup."""

    def __init__(self, bot, invoice_id: int, inv: dict | None = None):
        super().__init__(timeout=None)
        self.bot, self.invoice_id = bot, invoice_id
        closed = inv is None or inv["status"] in ("paid", "void")
        voided = inv is not None and inv["status"] == "void"

        def btn(label, action, style, cb, disabled=False):
            b = discord.ui.Button(label=label, style=style, custom_id=f"inv:{action}:{invoice_id}", disabled=disabled)
            b.callback = cb
            return b

        row1 = discord.ui.ActionRow(
            btn("Download PDF", "pdf", discord.ButtonStyle.primary, self.on_pdf),
            btn("Mark Paid", "paid", discord.ButtonStyle.success, self.on_paid, disabled=inv is not None and closed),
            btn("Partial Payment", "part", discord.ButtonStyle.secondary, self.on_partial, disabled=inv is not None and closed),
        )
        row2 = discord.ui.ActionRow(
            btn("Send to Ticket", "ticket", discord.ButtonStyle.secondary, self.on_ticket, disabled=voided),
            btn("Deliver Links", "deliver", discord.ButtonStyle.success, self.on_deliver, disabled=voided),
            btn("New Ticket", "newticket", discord.ButtonStyle.secondary, self.on_new_ticket, disabled=voided),
            btn("Void", "void", discord.ButtonStyle.danger, self.on_void, disabled=voided),
        )
        if inv is None:
            items = ui.card_items(f"Invoice {invoice_number(invoice_id)}", rows=[row1, row2])
        else:
            fields, lines, totals = invoice_blocks(inv)
            footer = f"Raised by {_who(inv.get('created_by'))} · {dmy((inv.get('created_at') or '')[:10])}"
            items = ui.card_items(f"Invoice {inv['number']}", fields, lines, totals,
                                  footer=footer, rows=[row1, row2])
        self.add_item(discord.ui.Container(*items))

    # ---- callbacks ----
    async def on_pdf(self, interaction: discord.Interaction):
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv:
            return await ui.say(interaction, "❌ Invoice not found.")
        if not (is_staff(interaction.user) or interaction.user.id == inv["client_discord_id"]):
            return await ui.say(interaction, "🚫 Only staff or the invoiced client can download this.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _pdf_message(interaction.followup, inv, ephemeral=True)

    async def _staff_only(self, interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await ui.say(interaction, "🚫 Staff only.")
        return False

    async def on_paid(self, interaction: discord.Interaction):
        if not await self._staff_only(interaction):
            return
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv or inv["balance"] <= 0 or inv["voided"]:
            return await ui.say(interaction, "ℹ️ Nothing left to pay on this invoice.")
        await interaction.response.send_modal(PaymentModal(self.bot, self.invoice_id, _plain(inv["balance"])))

    async def on_partial(self, interaction: discord.Interaction):
        if await self._staff_only(interaction):
            await interaction.response.send_modal(PaymentModal(self.bot, self.invoice_id))

    async def on_void(self, interaction: discord.Interaction):
        if await self._staff_only(interaction):
            await interaction.response.send_modal(VoidModal(self.bot, self.invoice_id))

    async def on_deliver(self, interaction: discord.Interaction):
        if not await self._staff_only(interaction):
            return
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv:
            return await ui.say(interaction, "❌ Invoice not found.")
        channel = None
        if inv.get("ticket_channel_id"):
            channel = self.bot.get_channel(inv["ticket_channel_id"])
        if channel is None:
            return await ui.say(
                interaction,
                f"ℹ️ No ticket linked\nUse `/invoice deliver invoice_id:{inv['id']}` and pick a channel.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await deliver_invoice(self.bot, inv, channel, interaction.user.id)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        except discord.HTTPException:
            return await ui.say(interaction, "❌ I couldn't post in the linked ticket.")
        await ui.say(interaction, f"🔗 Delivered {count} digital item(s) in {channel.mention}.")

    async def on_ticket(self, interaction: discord.Interaction):
        if not await self._staff_only(interaction):
            return
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv["ticket_channel_id"]:
            return await ui.say(
                interaction,
                f"ℹ️ No ticket linked yet\nUse `/invoice send-ticket invoice_id:{inv['id']} channel:#ticket`.")
        try:
            channel = self.bot.get_channel(inv["ticket_channel_id"]) or await self.bot.fetch_channel(inv["ticket_channel_id"])
            await send_invoice_to_channel(self.bot, channel, inv)
        except discord.HTTPException:
            return await ui.say(interaction, "❌ I couldn't post in the linked ticket (deleted or no permission).")
        await ui.say(interaction, f"📨 Sent to {channel.mention}.")

    async def on_new_ticket(self, interaction: discord.Interaction):
        """Open a fresh (or linked) client ticket straight from the invoice."""
        if not await self._staff_only(interaction):
            return
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv or not inv.get("client_discord_id"):
            return await ui.say(
                interaction, "ℹ️ This invoice has no Discord client saved\nUse `/ticket open client:@user`.")
        service = getattr(self.bot, "tickets", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        client = interaction.guild.get_member(inv["client_discord_id"])
        if client is None:
            try:
                client = await interaction.guild.fetch_member(inv["client_discord_id"])
            except discord.HTTPException:
                client = None
        if client is None:
            return await ui.say(
                interaction, "❌ That client is no longer in this server\nUse `/ticket open` with a current member.")
        ticket_row = None
        if inv.get("ticket_channel_id"):
            ticket_row = await self.bot.db.ticket_by_channel(inv["ticket_channel_id"])
        item = primary_service(inv)
        subject = f"Order {inv['number']} · {inv['client_name']}"[:120]
        details = (f"Invoice {inv['number']} for {fmt(inv['total'], inv['currency'])}.\n"
                   f"Product: {item.get('product_type') or item.get('description') or 'service'}")
        await service.open_ticket(
            interaction, client=client, kind="purchase", subject=subject, details=details,
            service=item.get("product_type") or "", parent_id=ticket_row["id"] if ticket_row else None,
        )


# ---------------------------------------------------------------------------
# Finance dashboard
# ---------------------------------------------------------------------------
class DashboardView(discord.ui.LayoutView):
    def __init__(self, bot, report: dict, outstanding: int):
        super().__init__(timeout=900)
        self.bot, self.report, self.month = bot, report, report["month"]
        net = report["net"]
        inc = "\n".join(f"• {n}: {fmt(a)}" for n, a in report["income"]) or "• none"
        exp = "\n".join(f"• {n}: {fmt(a)}" for n, a in report["expenses"]) or "• none"
        row = discord.ui.ActionRow()
        for label, cb in (("◀ Prev", self.on_prev), ("Next ▶", self.on_next)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            b.callback = cb
            row.add_item(b)
        pdf = discord.ui.Button(label="P&L PDF", style=discord.ButtonStyle.primary, emoji="📄")
        pdf.callback = self.on_pdf
        row.add_item(pdf)
        items = ui.card_items(
            f"Finance · {self.month}",
            f"{_f('Revenue', fmt(report['revenue']))}\n{inc}",
            f"{_f('Expenses', fmt(report['expense_total']))}\n{exp}",
            f"{_f('Net Profit' if net >= 0 else 'Net Loss', fmt(net))}\n"
            f"{_f('Outstanding Invoices (all time)', fmt(outstanding))}",
            rows=[row])
        self.add_item(discord.ui.Container(*items))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await ui.say(interaction, "🚫 Staff only.")
        return False

    async def _go(self, interaction, delta):
        month = shift_month(self.month, delta)
        report = await self.bot.db.month_report(month)
        await interaction.response.edit_message(view=DashboardView(self.bot, report, await self.bot.db.outstanding()))

    async def on_prev(self, interaction):
        await self._go(interaction, -1)

    async def on_next(self, interaction):
        await self._go(interaction, 1)

    async def on_pdf(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await asyncio.to_thread(pnl_pdf, self.report)
        filename = f"PnL-{self.month}.pdf"
        card = ui.file_card(f"Profit & Loss · {self.month}", _f("Net", fmt(self.report["net"])), filename)
        await interaction.followup.send(view=card, file=discord.File(io.BytesIO(data), filename=filename),
                                        ephemeral=True)


# ===========================================================================
# Tickets
# ===========================================================================
PRIORITY_ICON = {"urgent": "🔴", "high": "🟠", "normal": "🔵", "low": "⚪"}
STATUS_ICON = {"open": "🟢", "pending": "🟡", "closed": "⚫"}


def ticket_blocks(ticket: dict, deal: dict | None = None, invoice: dict | None = None,
                  notes: list | None = None, parent: dict | None = None,
                  category: str | None = None) -> tuple[str, str]:
    fields = [
        _f("Type", f"{ticket['kind'].title()}"),
        _f("Category", _esc(category) if category else "—"),
        _f("Subject", _esc(ticket["subject"][:180])),
        _f("Client Name", f"{_esc(ticket['client_name'])} ({_who(ticket.get('client_discord_id'))})"),
        _f("Status", f"{STATUS_ICON.get(ticket['status'], '')} {ticket['status'].title()}"),
        _f("Priority", f"{PRIORITY_ICON.get(ticket['priority'], '🔵')} {ticket['priority'].title()}"),
        _f("Claimed By", _who(ticket.get("claimed_by"))),
        _f("Opened By", _who(ticket.get("opened_by"))),
        _f("Opened", dmy((ticket.get("created_at") or "")[:10])),
        _f("Last Activity", dmy((ticket.get("last_activity") or "")[:10])),
    ]
    if parent:
        fields.append(_f("Follow-up Of", f"{ticket_number(parent['id'])} · {_esc(parent['subject'][:80])}"))
    more = []
    if deal:
        label = STAGE_META.get(deal["stage"], (deal["stage"], 0))[0]
        more.append(_f("Sales", f"{label} · budget {fmt(deal['budget'])} · value {fmt(deal['value'])}"
                       + (f" · follow-up {deal['next_follow_up']}" if deal.get("next_follow_up") else "")))
    if invoice:
        more.append(_f("Invoice", f"{invoice['number']} · {invoice['status']} · balance "
                       f"{fmt(invoice['balance'], invoice['currency'])}"))
    if ticket.get("close_reason"):
        more.append(_f("Close Reason", _esc(ticket["close_reason"][:200])))
    if notes:
        more.append("**Internal Notes**")
        more += [f"• <@{n['author_id']}> {_esc(n['note'][:140])}" for n in notes[:3]]
    return _clip("\n".join(fields), 2000), _clip("\n".join(more), 1500)


_FROM_CACHE = object()


async def build_ticket_view(bot, ticket_id: int, category=_FROM_CACHE):
    ticket = await bot.db.get_ticket(ticket_id)
    if not ticket:
        return None
    deal = await bot.db.deal_by_ticket(ticket_id)
    invoice = await bot.db.get_invoice(deal["invoice_id"]) if deal and deal.get("invoice_id") else None
    notes = await bot.db.ticket_notes(ticket_id, 3)
    parent = await bot.db.ticket_parent(ticket_id)
    if category is _FROM_CACHE:  # live category name from the channel cache (no API call)
        channel = bot.get_channel(ticket.get("channel_id") or 0) if hasattr(bot, "get_channel") else None
        category = getattr(getattr(channel, "category", None), "name", None)
    return TicketPanelView(bot, ticket, deal, invoice, notes, parent, category)


async def refresh_ticket_panel(bot, interaction: discord.Interaction, ticket_id: int):
    """Queue an in-place edit of the ticket dashboard (coalesced and de-duplicated by Tickets)."""
    service = getattr(bot, "tickets", None)
    if service:
        service.request_refresh(ticket_id)


class CloseTicketModal(discord.ui.Modal, title="Close ticket"):
    reason = discord.ui.TextInput(label="Reason / resolution", required=False, max_length=300,
                                  placeholder="Delivered the VPS, client confirmed everything works")

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Ticket staff only.")
        service = getattr(interaction.client, "tickets", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await service.close_ticket(interaction.guild, self.ticket_id, interaction.user, self.reason.value)
        except (ValueError, discord.HTTPException) as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, "⚫ Ticket closed and transcript saved.")
        await refresh_ticket_panel(interaction.client, interaction, self.ticket_id)


class TicketNoteModal(discord.ui.Modal, title="Internal note"):
    note = discord.ui.TextInput(label="Note (only staff can see this)", style=discord.TextStyle.paragraph,
                                max_length=1500)

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Ticket staff only.")
        await interaction.client.db.add_ticket_note(interaction.user.id, self.ticket_id, self.note.value)
        await ui.say(interaction, "📝 Note saved.")  # the dashboard refreshes from the database event


class FollowUpTicketModal(discord.ui.Modal, title="New linked ticket"):
    subject = discord.ui.TextInput(label="What is this follow-up about?", max_length=120,
                                   placeholder="Renewal / delivery / upgrade for the same client")
    details = discord.ui.TextInput(label="Handover notes", style=discord.TextStyle.paragraph,
                                   required=False, max_length=1500,
                                   placeholder="What was already discussed or purchased?")

    def __init__(self, parent_id: int):
        super().__init__()
        self.parent_id = parent_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Ticket staff only.")
        service = getattr(interaction.client, "tickets", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        await service.open_ticket(interaction, client=None, kind="purchase", subject=self.subject.value,
                                  details=self.details.value, parent_id=self.parent_id)


PURCHASE_TARGETS = {"support": "Technical Support", "partnership": "Partnership"}


class PurchaseModal(discord.ui.Modal):
    """Step 2 of Complete Purchase (a modal holds at most 5 inputs, so the category is picked first)."""
    client = discord.ui.TextInput(label="Client name", max_length=100)
    service = discord.ui.TextInput(label="Service name", max_length=100,
                                   placeholder="4GB VPS / Minecraft panel / Discord bot")
    payment = discord.ui.TextInput(label="Payment status", max_length=40,
                                   placeholder="Paid / Unpaid / Partially paid")
    server = discord.ui.TextInput(label="Server name / UUID (optional)", required=False, max_length=100)
    expiry = discord.ui.TextInput(label="Expiry date (optional)", required=False, max_length=10,
                                  placeholder="YYYY-MM-DD, e.g. 2026-11-27")

    def __init__(self, ticket: dict, key: str, panel_message=None):
        super().__init__(title=f"Complete purchase → {PURCHASE_TARGETS[key]}"[:45])
        self.ticket_id, self.channel_id, self.key, self.panel_message = ticket["id"], ticket["channel_id"], key, panel_message
        self.client.default = (ticket.get("client_name") or "")[:100] or None

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Ticket staff only.")
        service = getattr(interaction.client, "tickets", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        if interaction.guild is None or interaction.channel_id != self.channel_id:
            return await ui.say(interaction, "❌ Use this inside the ticket channel.")
        expiry = ""
        if self.expiry.value.strip():
            try:
                expiry = parse_date(self.expiry.value)
            except ValueError as e:
                return await ui.say(interaction, f"❌ {e}")
        val = lambda field, fallback="Not provided": field.value.strip() or fallback
        details = {"client": val(self.client), "service": val(self.service), "payment": val(self.payment),
                   "server": val(self.server), "expiry": expiry or "Not provided"}
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            posted = await service.complete_purchase(interaction.guild, self.ticket_id, interaction.user,
                                                     self.key, details)
        except (ValueError, discord.HTTPException) as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"✅ Purchase completed, ticket moved to **{PURCHASE_TARGETS[self.key]}**."
                     if posted else "⚠️ Ticket moved, but I could not post the summary in the channel.")
        # The dashboard (Complete Purchase greyed out, new category) is refreshed by Tickets.complete_purchase.


class PurchaseTargetView(ui.PersistentView):
    """Ephemeral step 1: pick the destination category, then the modal opens."""

    def __init__(self, bot, ticket: dict, panel_message=None):
        super().__init__(timeout=120)
        self.bot, self.ticket, self.panel_message = bot, ticket, panel_message
        select = discord.ui.Select(placeholder="Destination category", options=[
            discord.SelectOption(label="Technical Support", value="support", emoji="🛠️"),
            discord.SelectOption(label="Partnership", value="partnership", emoji="🤝")])
        select.callback = self.on_pick
        self.add_item(discord.ui.Container(*ui.card_items(
            "Complete purchase", "Where should this ticket go?", rows=[discord.ui.ActionRow(select)])))

    async def on_pick(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Ticket staff only.")
        key = interaction.data["values"][0]
        service = getattr(self.bot, "tickets", None)
        if not service or key not in PURCHASE_TARGETS:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        try:
            service.purchase_target(interaction.guild, key)  # fail now, not after the form is filled in
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await interaction.response.send_modal(PurchaseModal(self.ticket, key, self.panel_message))


class MoveTicketModal(discord.ui.Modal):
    """Step 2 of Move: optional new channel name."""
    new_name = discord.ui.TextInput(label="Channel name (optional)", required=False, max_length=90,
                                    placeholder="Leave unchanged to keep the current name")

    def __init__(self, ticket: dict, key: str, current_name: str):
        super().__init__(title=f"Move → {config.TICKET_CATEGORIES[key][0]}"[:45])
        self.ticket_id, self.channel_id, self.key = ticket["id"], ticket["channel_id"], key
        self.new_name.default = current_name[:90] or None

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Ticket staff only.")
        service = getattr(interaction.client, "tickets", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        if interaction.guild is None or interaction.channel_id != self.channel_id:
            return await ui.say(interaction, "❌ Use this inside the ticket channel.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await service.move_ticket(interaction.guild, self.ticket_id, interaction.user,
                                               self.key, self.new_name.value)
        except (ValueError, discord.HTTPException) as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"✅ {result}")


class MoveTargetView(ui.PersistentView):
    """Ephemeral step 1: pick the destination category, then the rename form opens."""

    def __init__(self, bot, ticket: dict, channel):
        super().__init__(timeout=120)
        self.bot, self.ticket = bot, ticket
        current = getattr(channel, "category_id", None)
        options = [discord.SelectOption(label=label, value=key,
                                        description="Current category" if cid == current else None)
                   for key, (label, _var, cid) in config.TICKET_CATEGORIES.items() if cid]
        select = discord.ui.Select(placeholder="Destination category", options=options)
        select.callback = self.on_pick
        self.add_item(discord.ui.Container(*ui.card_items(
            "Move ticket", "Pick a category. You can rename the channel in the next step.",
            rows=[discord.ui.ActionRow(select)])))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_ticket_staff(interaction.user):
            return True
        await ui.say(interaction, "🚫 Ticket staff only.")
        return False

    async def on_pick(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        service = getattr(self.bot, "tickets", None)
        if not service or key not in config.TICKET_CATEGORIES:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        try:
            service.category_target(interaction.guild, key)  # fail now, not after the form is filled in
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        name = getattr(interaction.channel, "name", "") or ""
        await interaction.response.send_modal(MoveTicketModal(self.ticket, key, name))


class TicketPanelView(discord.ui.LayoutView):
    """Persistent panel posted inside every ticket channel."""

    def __init__(self, bot, ticket: dict, deal=None, invoice=None, notes=None, parent=None, category=None):
        super().__init__(timeout=None)
        self.bot, self.ticket_id, self.ticket = bot, ticket["id"], ticket
        self.deal = deal
        closed = ticket["status"] == "closed"

        def btn(label, action, style, cb, disabled=False, emoji=None):
            b = discord.ui.Button(label=label, style=style, emoji=emoji,
                                  custom_id=f"ticket:{action}:{ticket['id']}", disabled=disabled)
            b.callback = cb
            return b

        claim_label = "Unclaim" if ticket.get("claimed_by") else "Claim"
        row1 = discord.ui.ActionRow(
            btn(claim_label, "claim", discord.ButtonStyle.secondary, self.on_claim, disabled=closed, emoji="🙋"),
            btn("Create Invoice", "invoice", discord.ButtonStyle.primary, self.on_invoice, emoji="🧾"),
            btn("Linked Ticket", "follow", discord.ButtonStyle.secondary, self.on_follow,
                disabled=closed, emoji="➕"),
            btn("Complete Purchase", "complete", discord.ButtonStyle.success, self.on_complete,
                disabled=closed or bool(ticket.get("purchase_completed_at")), emoji="✅"),
        )
        close_btn = (btn("Reopen", "reopen", discord.ButtonStyle.success, self.on_reopen, emoji="🔓")
                     if closed else
                     btn("Close", "close", discord.ButtonStyle.danger, self.on_close, emoji="🔒"))
        row2 = discord.ui.ActionRow(
            btn("Add Note", "note", discord.ButtonStyle.secondary, self.on_note, emoji="📝"),
            btn("Transcript", "transcript", discord.ButtonStyle.secondary, self.on_transcript, emoji="📄"),
            btn("Sales", "sales", discord.ButtonStyle.secondary, self.on_sales,
                disabled=deal is None, emoji="📈"),
            close_btn,
            btn("Move", "move", discord.ButtonStyle.secondary, self.on_move, disabled=closed, emoji="📂"),
        )
        priority = discord.ui.Select(
            custom_id=f"ticket:priority:{ticket['id']}", placeholder="Priority", disabled=closed,
            options=[discord.SelectOption(label=p.title(), value=p, emoji=PRIORITY_ICON[p],
                                          default=p == ticket["priority"]) for p in TICKET_PRIORITIES])
        priority.callback = self.on_priority
        fields, more = ticket_blocks(ticket, deal, invoice, notes, parent, category)
        items = ui.card_items(f"Ticket {ticket_number(ticket['id'])}", fields, more,
                              rows=[row1, row2, discord.ui.ActionRow(priority)])
        self.add_item(discord.ui.Container(*items))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Server-side gate for EVERY control on the dashboard (buttons and the priority select)."""
        if is_ticket_staff(interaction.user):
            return True
        await ui.say(interaction, "🚫 Ticket staff only.")
        return False

    async def _staff(self, interaction) -> bool:
        if is_ticket_staff(interaction.user):
            return True
        await ui.say(interaction, "🚫 Ticket staff only.")
        return False

    async def _ack(self, interaction: discord.Interaction):
        """Acknowledge a click. The dashboard message is edited once by Tickets.request_refresh."""
        if interaction.message is not None and interaction.message.flags.ephemeral:  # /ticket info copy
            return await interaction.response.edit_message(view=await build_ticket_view(self.bot, self.ticket_id))
        await interaction.response.defer()

    async def on_priority(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        value = interaction.data["values"][0]
        ticket = await self.bot.db.get_ticket(self.ticket_id)
        if value not in TICKET_PRIORITIES or not ticket or ticket["status"] == "closed":
            return await ui.say(interaction, "ℹ️ This ticket is closed.")
        if ticket["priority"] != value:
            await self.bot.db.set_ticket_priority(interaction.user.id, self.ticket_id, value)
        await self._ack(interaction)

    async def on_move(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        ticket = await self.bot.db.get_ticket(self.ticket_id)
        if not ticket or ticket["status"] == "closed":
            return await ui.say(interaction, "ℹ️ This ticket is closed. Reopen it before moving it.")
        if not any(cid for _, _, cid in config.TICKET_CATEGORIES.values()):
            return await ui.say(interaction, "❌ No ticket categories are configured. Set the `TICKET_*_CATEGORY_ID` values in config.py.")
        await interaction.response.send_message(view=MoveTargetView(self.bot, ticket, interaction.channel), ephemeral=True)

    async def on_claim(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        ticket = await self.bot.db.get_ticket(self.ticket_id)
        if not ticket or ticket["status"] == "closed":
            return await ui.say(interaction, "ℹ️ This ticket is closed.")
        claimed = None if ticket.get("claimed_by") == interaction.user.id else interaction.user.id
        await self.bot.db.set_ticket_claimed(interaction.user.id, self.ticket_id, claimed)
        await self._ack(interaction)

    async def on_invoice(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        service = getattr(self.bot, "invoices", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Invoice system is still starting up.")
        await service.open_draft_for_ticket(interaction, self.ticket)

    async def on_follow(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        await interaction.response.send_modal(FollowUpTicketModal(self.ticket_id))

    async def on_note(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        await interaction.response.send_modal(TicketNoteModal(self.ticket_id))

    async def on_complete(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        ticket = await self.bot.db.get_ticket(self.ticket_id)  # fresh: this view can be stale after a restart
        if not ticket or ticket["status"] == "closed":
            return await ui.say(interaction, "ℹ️ This ticket is closed.")
        if ticket.get("purchase_completed_at"):
            return await ui.say(interaction, "ℹ️ This purchase was already completed.")
        await interaction.response.send_message(
            view=PurchaseTargetView(self.bot, ticket, interaction.message), ephemeral=True)

    async def on_sales(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        deal = await self.bot.db.deal_by_ticket(self.ticket_id)
        if not deal:
            return await ui.say(interaction, "ℹ️ This ticket has no sales deal.")
        view = await build_deal_view(self.bot, deal["id"])
        await interaction.response.send_message(view=view, ephemeral=True)

    async def on_close(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        await interaction.response.send_modal(CloseTicketModal(self.ticket_id))

    async def on_reopen(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        service = getattr(self.bot, "tickets", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await service.reopen_ticket(interaction.guild, self.ticket_id, interaction.user)
        except (ValueError, discord.HTTPException) as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, "🔓 Ticket reopened.")

    async def on_transcript(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        service = getattr(self.bot, "tickets", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            path = await service.save_transcript(self.ticket_id, interaction.guild)
        except (ValueError, discord.HTTPException, OSError) as e:
            return await ui.say(interaction, f"❌ {e}")
        await service.send_transcript(interaction, self.ticket_id, path)


class TicketOpenPanelView(discord.ui.LayoutView):
    """Public panel customers use to open their own tickets."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

        def btn(label, kind, style, emoji):
            b = discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"ticketopen:{kind}")
            b.callback = getattr(self, f"on_{kind}")
            return b

        row = discord.ui.ActionRow(btn("Create Ticket", "create", discord.ButtonStyle.success, "🎫"))
        items = ui.card_items(
            "Need Hosting? Create a Ticket",
            "Create a ticket and our team will assist you. Our staff will identify your request "
            "and route your ticket to the appropriate department.",
            rows=[row])
        self.add_item(discord.ui.Container(*items))

    async def on_create(self, interaction):
        # No form: the ticket is created straight away in the "Tickets" category.
        service = getattr(interaction.client, "tickets", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Ticket system is still starting up.")
        if not service.begin_create(interaction.user.id):  # double click
            return await ui.say(interaction, "⏳ Your ticket is already being created.")
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await service.open_ticket(interaction, client=interaction.user, kind="purchase",
                                      subject=f"Ticket by {interaction.user.display_name}")
        finally:
            service.end_create(interaction.user.id)


# ===========================================================================
# Sales pipeline
# ===========================================================================
def _followup_label(value: str | None) -> str:
    if not value:
        return "not set"
    age = days_since(value)
    if age is None:
        return value
    if age > 0:
        return f"{value} · overdue {age}d"
    if age == 0:
        return f"{value} · today"
    return f"{value} · in {-age}d"


def deal_blocks(deal: dict, invoice: dict | None = None, timeline: list | None = None) -> tuple[str, str]:
    label, probability = STAGE_META.get(deal["stage"], (deal["stage"], 0))
    ticket = (f"<#{deal['ticket_channel_id']}> · {ticket_number(deal['ticket_id'])}"
              if deal.get("ticket_channel_id") else (ticket_number(deal["ticket_id"]) if deal.get("ticket_id") else "—"))
    fields = [
        _f("Stage", label),
        _f("Client Name", f"{_esc(deal['client_name'])} ({_who(deal.get('client_discord_id'))})"),
        _f("Service", _esc(deal.get("service") or "—")),
        _f("Budget", fmt(deal["budget"])),
        _f("Quoted Value", fmt(deal["value"])),
        _f("Win Chance", f"{int(probability * 100)}%"),
        _f("Ticket", ticket),
        _f("Next Follow-up", _followup_label(deal.get("next_follow_up"))),
    ]
    if deal.get("follow_up_note"):
        fields.append(_f("Follow-up Note", _esc(deal["follow_up_note"][:200])))
    if invoice:
        fields.append(_f("Invoice", f"{invoice['number']} · {invoice['status']} · "
                         f"balance {fmt(invoice['balance'], invoice['currency'])}"))
    else:
        fields.append(_f("Invoice", "not created yet"))
    more = []
    if deal.get("notes"):
        more += ["**Notes**", _esc(deal["notes"][:400]), ""]
    if timeline:
        more.append("**Timeline**")
        for event in timeline[:6]:
            more.append(f"• {event['created_at'][:16]} · <@{event['actor_id']}> · {_esc(event['detail'] or event['kind'])}")
    return _clip("\n".join(fields), 2000), _clip("\n".join(more).strip(), 1500)


async def build_deal_view(bot, deal_id: int):
    deal = await bot.db.get_deal(deal_id)
    if not deal:
        return None
    invoice = await bot.db.get_invoice(deal["invoice_id"]) if deal.get("invoice_id") else None
    timeline = await bot.db.deal_timeline(deal_id, 6)
    return DealView(bot, deal, invoice, timeline)


async def refresh_deal_card(bot, interaction, deal_id: int):
    view = await build_deal_view(bot, deal_id)
    if view is None:
        return
    try:
        await interaction.message.edit(view=view)
    except (discord.HTTPException, AttributeError):
        pass


class DealFollowupModal(discord.ui.Modal, title="Schedule follow-up"):
    when = discord.ui.TextInput(label="When", max_length=20, placeholder="tomorrow / +3d / 2026-10-24")
    note = discord.ui.TextInput(label="What should happen?", required=False, max_length=300,
                                style=discord.TextStyle.paragraph, placeholder="Send revised quote for 8GB plan")

    def __init__(self, deal_id: int):
        super().__init__()
        self.deal_id = deal_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Sales staff only.")
        from desk.utils import parse_when
        try:
            when = parse_when(self.when.value)
            await interaction.client.db.set_deal_followup(interaction.user.id, self.deal_id, when, self.note.value)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"📅 Follow-up set for {when}.")
        await refresh_deal_card(interaction.client, interaction, self.deal_id)


class DealNoteModal(discord.ui.Modal, title="Deal note"):
    note = discord.ui.TextInput(label="Note", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, deal_id: int):
        super().__init__()
        self.deal_id = deal_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Sales staff only.")
        await interaction.client.db.add_deal_note(interaction.user.id, self.deal_id, self.note.value)
        await ui.say(interaction, "📝 Deal note saved.")
        await refresh_deal_card(interaction.client, interaction, self.deal_id)


class DealLostModal(discord.ui.Modal, title="Mark deal lost"):
    reason = discord.ui.TextInput(label="Why was it lost?", max_length=300,
                                  placeholder="Budget too low / went with another host")

    def __init__(self, deal_id: int):
        super().__init__()
        self.deal_id = deal_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Sales staff only.")
        try:
            await interaction.client.db.set_deal_stage(interaction.user.id, self.deal_id, "lost", self.reason.value)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, "❌ Deal marked lost.", error=False)
        await refresh_deal_card(interaction.client, interaction, self.deal_id)


class DealView(discord.ui.LayoutView):
    """Stage controls for one buying-enquiry deal."""

    def __init__(self, bot, deal: dict, invoice=None, timeline=None):
        super().__init__(timeout=900)
        self.bot, self.deal_id, self.deal = bot, deal["id"], deal
        closed = deal["stage"] in ("won", "lost")

        def btn(label, action, style, cb, disabled=False, emoji=None):
            b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled,
                                  custom_id=f"deal:{action}:{deal['id']}")
            b.callback = cb
            return b

        row1 = discord.ui.ActionRow(
            btn("Advance Stage", "advance", discord.ButtonStyle.primary, self.on_advance,
                disabled=closed or deal["stage"] == "negotiation", emoji="⏭️"),
            btn("Follow-up", "followup", discord.ButtonStyle.secondary, self.on_followup,
                disabled=closed, emoji="📅"),
            btn("Add Note", "note", discord.ButtonStyle.secondary, self.on_note, emoji="📝"),
        )
        row2 = discord.ui.ActionRow(
            btn("Mark Won", "won", discord.ButtonStyle.success, self.on_won, disabled=closed, emoji="✅"),
            btn("Mark Lost", "lost", discord.ButtonStyle.danger, self.on_lost, disabled=closed, emoji="❌"),
            btn("Create Invoice", "invoice", discord.ButtonStyle.secondary, self.on_invoice, emoji="🧾"),
            btn("Open Ticket", "open", discord.ButtonStyle.secondary, self.on_open,
                disabled=not deal.get("ticket_channel_id"), emoji="🎫"),
        )
        fields, more = deal_blocks(deal, invoice, timeline)
        items = ui.card_items(f"Deal #{deal['id']}", fields, more, rows=[row1, row2])
        self.add_item(discord.ui.Container(*items))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_ticket_staff(interaction.user):
            return True
        await ui.say(interaction, "🚫 Sales staff only.")
        return False

    async def _staff(self, interaction) -> bool:
        if is_ticket_staff(interaction.user):
            return True
        await ui.say(interaction, "🚫 Sales staff only.")
        return False

    async def on_advance(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        try:
            await self.bot.db.advance_deal(interaction.user.id, self.deal_id)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        view = await build_deal_view(self.bot, self.deal_id)
        await interaction.response.edit_message(view=view)

    async def on_followup(self, interaction: discord.Interaction):
        if await self._staff(interaction):
            await interaction.response.send_modal(DealFollowupModal(self.deal_id))

    async def on_note(self, interaction: discord.Interaction):
        if await self._staff(interaction):
            await interaction.response.send_modal(DealNoteModal(self.deal_id))

    async def on_won(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        try:
            await self.bot.db.set_deal_stage(interaction.user.id, self.deal_id, "won", "Marked won")
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        view = await build_deal_view(self.bot, self.deal_id)
        await interaction.response.edit_message(view=view)

    async def on_lost(self, interaction: discord.Interaction):
        if await self._staff(interaction):
            await interaction.response.send_modal(DealLostModal(self.deal_id))

    async def on_invoice(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        service = getattr(self.bot, "invoices", None)
        if not service:
            return await ui.say(interaction, "ℹ️ Invoice system is still starting up.")
        await service.open_draft_for_deal(interaction, self.deal)

    async def on_open(self, interaction: discord.Interaction):
        channel_id = self.deal.get("ticket_channel_id")
        if not channel_id:
            return await ui.say(interaction, "ℹ️ This deal is not linked to a ticket.")
        await ui.say(interaction, f"🎫 Ticket: <#{channel_id}>")
