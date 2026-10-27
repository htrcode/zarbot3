"""Finance commands: sales (payments received), expenses and revenue statements.

Everything produced here — Sales, Expense and Revenue Statements, whether a
single entry or a period report — is posted to the **selected company's** own
statement channel and never to another company's channel. A company must be
picked explicitly: nothing defaults to the first company in the list.
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
from desk.database import revenue_number
from desk.utils import duplicate_submission, staff_interaction_check, to_cents

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

_COMPANY_CHOICES = [
    app_commands.Choice(name=company.name, value=company.key) for company in config.COMPANIES.values()
]
_REVENUE_CHOICES = [
    app_commands.Choice(name=name, value=name) for name in ("Commission", "Investment", "General")
]


async def _bad(interaction: discord.Interaction, title: str, body: str) -> None:
    await interaction.response.send_message(view=ui.notice(title, body, error=True), ephemeral=True)


async def _statement_channel(interaction: discord.Interaction, company: config.Company):
    """Resolve the company's statement channel, replying with setup help when it is unavailable."""
    channel = await ui.resolve_channel(interaction.client, company.channel_id("statement"))
    if channel is None:
        await interaction.response.send_message(
            view=ui.missing_company_channel_notice(company), ephemeral=True
        )
    return channel


async def _send_statement(
    interaction: discord.Interaction,
    channel,
    company: config.Company,
    card: discord.ui.LayoutView,
    *,
    saved: bool,
    done_title: str,
) -> None:
    """Send ``card`` to the company channel and confirm to the user."""
    try:
        await channel.send(view=card)
    except discord.HTTPException:
        await interaction.response.send_message(
            view=ui.notice(
                "Could not post statement",
                ("It was saved, but I" if saved else "I")
                + f" could not send it to <#{channel.id}> for **{company.name}** — "
                "check my **Send Messages** permission there.",
                error=True,
            ),
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        view=ui.notice(done_title, f"Posted to <#{channel.id}> ({company.name})."),
        ephemeral=True,
    )


class Statements(commands.Cog):
    """Sales, Expense and Revenue Statements (one company at a time)."""

    sale = app_commands.Group(name="sale", description="Sales (payments received)", guild_only=True)
    expense = app_commands.Group(name="expense", description="Professional expenses", guild_only=True)
    statement = app_commands.Group(
        name="statement",
        description="Period statements posted to the company's statements channel",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Finance data is staff-only (administrators + STAFF_ROLE_IDS).
        return await staff_interaction_check(interaction)

    # -- Sales Statement (one sale) ----------------------------------------
    @sale.command(name="add", description="Record a sale and post its Sales Statement to the company channel")
    @app_commands.describe(
        company="Company this sale belongs to",
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
    @app_commands.choices(company=_COMPANY_CHOICES, method=_METHOD_CHOICES)
    async def add_sale(
        self,
        interaction: discord.Interaction,
        company: app_commands.Choice[str],
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
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        if duplicate_submission((interaction.user.id, "sale", selected.key, round(amount, 2),
                                 client_name.strip().lower(), date.strip())):
            await _bad(interaction, "Already recorded",
                       "That sale was just submitted. Check the statement channel before adding it again.")
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
            company=selected.key,
        )
        await mirror_sale(interaction.client, record, interaction.user.id)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.sale_card(record, store.total_sales(company=selected.key)),
            saved=True,
            done_title="Sale recorded",
        )

    # -- Expense Statement (one expense) -----------------------------------
    @expense.command(name="add", description="Record an expense and post its Expense Statement to the company channel")
    @app_commands.describe(
        company="Company this expense belongs to",
        description="What the expense was for",
        amount="Amount spent (₹)",
        category="Expense category",
        method="How it was paid",
        paid_to="Who was paid",
        transaction_id="Transaction ID / UTR",
        remarks="Anything worth noting",
        date="Expense date (YYYY-MM-DD), defaults to today",
    )
    @app_commands.choices(company=_COMPANY_CHOICES, category=_CATEGORY_CHOICES, method=_METHOD_CHOICES)
    async def add_expense(
        self,
        interaction: discord.Interaction,
        company: app_commands.Choice[str],
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
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        if duplicate_submission((interaction.user.id, "expense", selected.key, round(amount, 2),
                                 description.strip().lower(), date.strip())):
            await _bad(interaction, "Already recorded",
                       "That expense was just submitted. Check the statement channel before adding it again.")
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
            company=selected.key,
        )
        await mirror_expense(interaction.client, record, interaction.user.id)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.expense_card(record, store.total_expenses(company=selected.key)),
            saved=True,
            done_title="Expense recorded",
        )

    # -- Revenue Statement (one revenue entry) -----------------------------
    @app_commands.command(
        name="revenue",
        description="Record revenue (Commission / Investment / General) and post its Revenue Statement",
    )
    @app_commands.describe(
        company="Company this revenue belongs to",
        category="Revenue category",
        amount="Amount received (₹)",
        description="What the revenue was for",
        date="Revenue date (YYYY-MM-DD), defaults to today",
    )
    @app_commands.choices(company=_COMPANY_CHOICES, category=_REVENUE_CHOICES)
    async def add_revenue(
        self,
        interaction: discord.Interaction,
        company: app_commands.Choice[str],
        category: app_commands.Choice[str],
        amount: app_commands.Range[float, 0.01, 100_000_000.0],
        description: str,
        date: str = "",
    ) -> None:
        if date and not _DATE.match(date.strip()):
            await _bad(interaction, "Invalid date", f"`{date}` should look like `2026-10-26`.")
            return
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        when = date.strip() or store._today()
        if duplicate_submission((interaction.user.id, "revenue", selected.key, category.value,
                                 round(amount, 2), description.strip().lower(), when)):
            await _bad(interaction, "Already recorded",
                       "That revenue entry was just submitted. Check the statement channel before adding it again.")
            return

        try:
            revenue_id = await self.bot.db.add_revenue(
                interaction.user.id, selected.key, category.value, to_cents(amount), description, when
            )
        except ValueError as exc:
            await _bad(interaction, "Revenue not recorded", str(exc))
            return

        record = await self.bot.db.get_revenue(revenue_id) or {}
        record["number"] = revenue_number(revenue_id)
        record["recorded_by"] = interaction.user.display_name
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.revenue_card(record),
            saved=True,
            done_title="Revenue recorded",
        )

    # -- period statements --------------------------------------------------
    @statement.command(name="sales", description="Post a Sales Statement for a period")
    @app_commands.describe(company="Company to report on", period="Period to cover")
    @app_commands.choices(company=_COMPANY_CHOICES, period=_PERIOD_CHOICES)
    async def sales_statement(self, interaction: discord.Interaction,
                              company: app_commands.Choice[str], period: str = "month") -> None:
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        start = store.period_start(period)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.sales_statement_card(store.payments(start, company=selected.key), period,
                                    store.total_sales(start, company=selected.key), company=selected.name),
            saved=False,
            done_title="Sales Statement",
        )

    @statement.command(name="expenses", description="Post an Expense Statement for a period")
    @app_commands.describe(company="Company to report on", period="Period to cover")
    @app_commands.choices(company=_COMPANY_CHOICES, period=_PERIOD_CHOICES)
    async def expenses_statement(self, interaction: discord.Interaction,
                                 company: app_commands.Choice[str], period: str = "month") -> None:
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        start = store.period_start(period)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.expenses_statement_card(store.expenses(start, company=selected.key), period,
                                       store.total_expenses(start, company=selected.key),
                                       company=selected.name),
            saved=False,
            done_title="Expense Statement",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Statements(bot))
