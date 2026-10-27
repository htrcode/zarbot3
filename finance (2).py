import asyncio
import logging
import io
from typing import Optional

import discord
import ui
from discord import app_commands
from discord.ext import commands

from desk.bridge import record_finance
from desk.pdf_gen import pnl_pdf
from desk.utils import fmt, parse_date, staff_interaction_check, to_cents, valid_month
from desk.views import DashboardView, simple_card

log = logging.getLogger("invoice-bot.finance")

KIND_CHOICES = [app_commands.Choice(name="Income", value="income"),
                app_commands.Choice(name="Expense", value="expense")]


@app_commands.guild_only()
class Finance(commands.GroupCog, group_name="finance", group_description="Income, expenses and reports"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    async def _record(self, interaction, kind, amount, category, description, date):
        try:
            cents = to_cents(amount)
            when = parse_date(date)
            tx_id = await self.bot.db.add_transaction(
                interaction.user.id, kind, category, cents, description, when)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        # Keep Sales / Expense Statements in step with the books.
        try:
            await record_finance(self.bot, kind, category, cents, description, when,
                                 interaction.user.display_name)
        except Exception:
            log.exception("Could not post the %s statement", kind)
        icon = "➕" if kind == "income" else "➖"
        statement = "Sales Statement" if kind == "income" else "Expense Statement"
        await ui.say(interaction, f"{icon} {kind.title()} recorded\n"
                     f"{ui._field('ID', f'#{tx_id}')}\n{ui._field('Category', category)}\n"
                     f"{ui._field('Amount', fmt(cents))}\n{ui._field('Details', description or '—')}\n"
                     f"-# {statement} posted to the statements channel.")

    async def _ac(self, kind, current):
        cats = await self.bot.db.categories(kind)
        return [app_commands.Choice(name=c["name"], value=c["name"])
                for c in cats if current.lower() in c["name"].lower()][:25]

    # ---------- record ----------
    @app_commands.command(name="income", description="Record income (hosting revenue, other sales...)")
    @app_commands.describe(date="YYYY-MM-DD, defaults to today (UTC)")
    async def income(self, interaction: discord.Interaction, amount: app_commands.Range[float, 0.01, 100_000_000.0],
                     category: str, description: str = "", date: Optional[str] = None):
        await self._record(interaction, "income", amount, category, description, date)

    @income.autocomplete("category")
    async def _income_ac(self, interaction, current: str):
        return await self._ac("income", current)

    @app_commands.command(name="expense", description="Record an expense (servers, software, marketing, commissions...)")
    @app_commands.describe(date="YYYY-MM-DD, defaults to today (UTC)")
    async def expense(self, interaction: discord.Interaction, amount: app_commands.Range[float, 0.01, 100_000_000.0],
                      category: str, description: str = "", date: Optional[str] = None):
        await self._record(interaction, "expense", amount, category, description, date)

    @expense.autocomplete("category")
    async def _expense_ac(self, interaction, current: str):
        return await self._ac("expense", current)

    @app_commands.command(name="category-add", description="Add a new income or expense category")
    @app_commands.choices(kind=KIND_CHOICES)
    async def category_add(self, interaction: discord.Interaction, kind: app_commands.Choice[str], name: str):
        try:
            await self.bot.db.add_category(interaction.user.id, name, kind.value)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"✅ Added {kind.value} category **{name}**.")

    # ---------- history ----------
    @app_commands.command(name="transactions", description="Transaction history for a month")
    @app_commands.choices(kind=KIND_CHOICES)
    async def transactions(self, interaction: discord.Interaction, month: Optional[str] = None,
                           kind: Optional[app_commands.Choice[str]] = None):
        try:
            month = valid_month(month)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        rows = await self.bot.db.transactions(month, kind.value if kind else None, limit=20)
        lines = [f"`#{t['id']}` {t['occurred_on']} · {'➕' if t['kind'] == 'income' else '➖'} {t['category']} · "
                 f"**{fmt(t['amount'])}** · {(t['description'] or '')[:40]}" for t in rows]
        await interaction.response.send_message(
            view=simple_card(f"Transactions · {month}", "\n".join(lines) or "No transactions."), ephemeral=True)

    @app_commands.command(name="void", description="Void a manual transaction (never deleted, kept in audit log)")
    async def void(self, interaction: discord.Interaction, transaction_id: int, reason: str):
        try:
            await self.bot.db.void_transaction(interaction.user.id, transaction_id, reason)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"⚫ Transaction #{transaction_id} voided.")

    @app_commands.command(name="audit", description="Recent audit log entries")
    async def audit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 15):
        rows = await self.bot.db.recent_audit(limit)
        lines = [f"`#{r['id']}` {r['ts'][:16]} · <@{r['actor_id']}> · **{r['action']}** "
                 f"{r['entity']} #{r['entity_id']} · `{(r['details'] or '')[:70]}`" for r in rows]
        await interaction.response.send_message(
            view=simple_card("Audit log", "\n".join(lines) or "Empty."), ephemeral=True)

    # ---------- reports ----------
    @app_commands.command(name="dashboard", description="Monthly revenue, expenses and net profit")
    async def dashboard(self, interaction: discord.Interaction, month: Optional[str] = None):
        try:
            month = valid_month(month)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        report = await self.bot.db.month_report(month)
        await interaction.response.send_message(
            view=DashboardView(self.bot, report, await self.bot.db.outstanding()), ephemeral=True)

    @app_commands.command(name="pnl", description="Profit and Loss statement (PDF)")
    async def pnl(self, interaction: discord.Interaction, month: Optional[str] = None):
        try:
            month = valid_month(month)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.bot.db.month_report(month)
        data = await asyncio.to_thread(pnl_pdf, report)
        filename = f"PnL-{month}.pdf"
        card = ui.file_card(f"Profit & Loss · {month}", ui._field("Net", fmt(report["net"])), filename)
        await interaction.followup.send(view=card, file=discord.File(io.BytesIO(data), filename=filename),
                                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Finance(bot))
