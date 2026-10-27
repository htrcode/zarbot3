"""Quick invoices (client, contact, amount, service/plan, due date).

``/invoice create`` opens :class:`QuickInvoiceModal`; the command itself lives in
``desk/invoices.py`` next to the itemised-invoice commands because Discord allows
only one ``/invoice`` group. New invoices are posted to ``INVOICE_CHANNEL_ID`` —
deliberately *not* the channel used for Sales / Expense Statements.
"""

from __future__ import annotations

import discord
from discord.ext import commands

import config
import data as store
import ui


class QuickInvoiceModal(discord.ui.Modal, title="Create invoice"):
    customer = discord.ui.TextInput(label="Client name", placeholder="Acme Ltd.", max_length=100)
    contact = discord.ui.TextInput(
        label="Client contact", placeholder="Discord / phone / email", required=False, max_length=100
    )
    amount = discord.ui.TextInput(label="Amount (₹)", placeholder="1250.00", max_length=16)
    service = discord.ui.TextInput(
        label="Service/Plan", placeholder="Minecraft Hosting - Plan 4GB", max_length=200
    )
    due = discord.ui.TextInput(
        label="Due date (YYYY-MM-DD)", placeholder="2026-11-30", required=False, max_length=32
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_amount = str(self.amount.value).replace(",", "").replace("₹", "").strip()
        try:
            amount = round(float(raw_amount), 2)
        except ValueError:
            await interaction.response.send_message(
                view=ui.notice("Invalid amount", f"`{self.amount.value}` is not a number.", error=True),
                ephemeral=True,
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                view=ui.notice("Invalid amount", "The amount must be greater than zero.", error=True),
                ephemeral=True,
            )
            return

        # NEW INVOICES GO TO THE INVOICE CHANNEL, NOT THE STATEMENTS CHANNEL.
        # Check the channel first so no invoice is saved without being posted.
        channel = await ui.resolve_channel(interaction.client, config.INVOICE_CHANNEL_ID)
        if channel is None:
            await interaction.response.send_message(
                view=ui.missing_channel_notice("INVOICE_CHANNEL_ID"), ephemeral=True
            )
            return

        inv = store.create_invoice(
            customer=str(self.customer.value),
            contact=str(self.contact.value or ""),
            amount=amount,
            due=str(self.due.value or ""),
            service=str(self.service.value or ""),
            created_by=interaction.user.display_name,
        )

        try:
            await channel.send(view=ui.invoice_card(inv))
        except discord.HTTPException:
            await interaction.response.send_message(
                view=ui.notice(
                    "Could not post invoice",
                    "I could not send the invoice to the invoice channel — "
                    "check my **Send Messages** permission there.",
                    error=True,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            view=ui.notice(
                "Invoice created",
                f"`{inv['number']}` for **{store.money(inv['amount'])}** ({inv['customer']}) "
                f"was posted to <#{config.INVOICE_CHANNEL_ID}>.",
            ),
            ephemeral=True,
        )


# Backwards-compatible name.
InvoiceModal = QuickInvoiceModal


async def setup(bot: commands.Bot) -> None:  # nothing to register: the commands live in desk/invoices.py
    return None
