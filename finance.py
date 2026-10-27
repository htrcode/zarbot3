"""Finance commands: sales (payments received) and professional expenses.

Everything produced here — Sales Statements and Expense Statements, whether a
single entry or a period report — is posted to ``STATEMENTS_CHANNEL_ID``, the
same channel, and never to the invoice channel.
"""

from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands

import config
import data as store
import ui
from desk.bridge import mirror_expense, mirror_sale
from desk.utils import staff_interaction_check

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_PERIOD_CHOICES = [
    app_commands.Choice(name=label, value=key) for key, label in store.PERIODS.items()
]

_METHOD_CHOICES = [
    app_commands.Choice(name="UPI", value="upi"),
    app_commands.Choice(name="Bank transfer", value="bank transfer"),
    app_commands.Choice(name="Card", value="card"),
    app_commands.Choice(name="Cash", value="cash"),
    app_commands.Choice(name="Other", value="other"),
]

_CATEGORY_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in (
        "Software & subscriptions",
        "Hosting & servers",
        "Domains",
        "Travel",
        "Office",
        "Equipment",
        "Marketing",
        "Professional services",
        "Utilities",
        "General",
    )
]


async def _bad(interaction: discord.Interaction, title: str, body: str) -> None:
    await interaction.response.send_message(view=ui.notice(title, body, error=True), ephemeral=True)


async def _post(
    interaction: discord.Interaction,
    card: discord.ui.LayoutView,
    *,
    saved: bool,
    done_title: str,
) -> None:
    """Send ``card`` to the statements channel and confirm to the user."""
    channel = await ui.resolve_channel(interaction.client, config.STATEMENTS_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            view=ui.missing_channel_notice("STATEMENTS_CHANNEL_ID"), ephemeral=True
        )
        return
    try:
        await channel.send(view=card)
    except discord.HTTPException:
        await interaction.response.send_message(
            view=ui.notice(
                "Could not post statement",
                ("It was saved, but I" if saved else "I")
                + " could not send it to the statements channel — "
                "check my **Send Messages** permission there.",
                error=True,
            ),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        view=ui.notice(done_title, f"Posted to <#{config.STATEMENTS_CHANNEL_ID}>."),
        ephemeral=True,
    )


class Statements(commands.Cog):
    """Sales Statements and Expense Statements."""

    sale = app_commands.Group(name="sale", description="Sales (payments received)", guild_only=True)
    expense = app_commands.Group(name="expense", description="Professional expenses", guild_only=True)
    statement = app_commands.Group(
        name="statement",
        description="Period statements posted to the statements channel",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Finance data is staff-only (administrators + STAFF_ROLE_IDS).
        return await staff_interaction_check(interaction)

    # -- Sales Statement (one sale) ----------------------------------------
    @sale.command(name="add", description="Record a sale and post its Sales Statement")
    @app_commands.describe(
        client_name="Client name",
        amount="Amount received (₹)",
        service="Service / plan sold",
        method="How it was paid",
        client_contact="Client contact (Discord, phone, email)",
        server_name="Server name",
        server_uuid="Server UUID",
        transaction_id="Transaction ID / UTR",
        remarks="Anything worth noting",
        date="Sale date (YYYY-MM-DD), defaults to today",
    )
    @app_commands.choices(method=_METHOD_CHOICES)
    async def add_sale(
        self,
        interaction: discord.Interaction,
        client_name: str,
        amount: float,
        service: str,
        method: str = "upi",
        client_contact: str = "",
        server_name: str = "",
        server_uuid: str = "",
        transaction_id: str = "",
        remarks: str = "",
        date: str = "",
    ) -> None:
        if amount <= 0:
            await _bad(interaction, "Invalid amount", "The amount must be greater than zero.")
            return
        if date and not _DATE.match(date.strip()):
            await _bad(interaction, "Invalid date", f"`{date}` should look like `2026-10-26`.")
            return

        record = store.add_payment(
            client_name=client_name,
            client_contact=client_contact,
            server_name=server_name,
            server_uuid=server_uuid,
            service=service,
            amount=amount,
            method=method,
            transaction_id=transaction_id,
            remarks=remarks,
            date=date,
            recorded_by=interaction.user.display_name,
        )
        await mirror_sale(interaction.client, record, interaction.user.id)
        await _post(
            interaction,
            ui.sale_card(record, store.total_sales()),
            saved=True,
            done_title="Sale recorded",
        )

    # -- Expense Statement (one expense) -----------------------------------
    @expense.command(name="add", description="Record an expense and post its Expense Statement")
    @app_commands.describe(
        description="What the expense was for",
        amount="Amount spent (₹)",
        category="Expense category",
        method="How it was paid",
        paid_to="Who was paid",
        transaction_id="Transaction ID / UTR",
        remarks="Anything worth noting",
        date="Expense date (YYYY-MM-DD), defaults to today",
    )
    @app_commands.choices(category=_CATEGORY_CHOICES, method=_METHOD_CHOICES)
    async def add_expense(
        self,
        interaction: discord.Interaction,
        description: str,
        amount: float,
        category: str = "General",
        method: str = "upi",
        paid_to: str = "",
        transaction_id: str = "",
        remarks: str = "",
        date: str = "",
    ) -> None:
        if amount <= 0:
            await _bad(interaction, "Invalid amount", "The amount must be greater than zero.")
            return
        if date and not _DATE.match(date.strip()):
            await _bad(interaction, "Invalid date", f"`{date}` should look like `2026-10-26`.")
            return

        record = store.add_expense(
            description=description,
            category=category,
            amount=amount,
            method=method,
            paid_to=paid_to,
            transaction_id=transaction_id,
            remarks=remarks,
            date=date,
            recorded_by=interaction.user.display_name,
        )
        await mirror_expense(interaction.client, record, interaction.user.id)
        await _post(
            interaction,
            ui.expense_card(record, store.total_expenses()),
            saved=True,
            done_title="Expense recorded",
        )

    # -- period statements --------------------------------------------------
    @statement.command(name="sales", description="Post a Sales Statement for a period")
    @app_commands.describe(period="Period to cover")
    @app_commands.choices(period=_PERIOD_CHOICES)
    async def sales_statement(self, interaction: discord.Interaction, period: str = "month") -> None:
        start = store.period_start(period)
        await _post(
            interaction,
            ui.sales_statement_card(store.payments(start), period, store.total_sales(start)),
            saved=False,
            done_title="Sales Statement",
        )

    @statement.command(name="expenses", description="Post an Expense Statement for a period")
    @app_commands.describe(period="Period to cover")
    @app_commands.choices(period=_PERIOD_CHOICES)
    async def expenses_statement(self, interaction: discord.Interaction, period: str = "month") -> None:
        start = store.period_start(period)
        await _post(
            interaction,
            ui.expenses_statement_card(store.expenses(start), period, store.total_expenses(start)),
            saved=False,
            done_title="Expense Statement",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Statements(bot))
