"""Department-scoped ticket access, the ticket move flow and the /revenue fields.

Run with: python -m unittest discover -s tests -t .
"""
import json
import os
import shutil
import tempfile
import types
import unittest

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_TMP = tempfile.mkdtemp(prefix="zarbot-departments-")
os.environ.setdefault("INVOICE_BOT_DATA", os.path.join(_TMP, "data.json"))

import discord  # noqa: E402

import config  # noqa: E402
import ui  # noqa: E402
from desk import views  # noqa: E402
from desk.database import Database  # noqa: E402
from desk.pdf_gen import pnl_pdf  # noqa: E402
from desk.tickets import Tickets  # noqa: E402


# --------------------------------------------------------------------------- fakes
class _Perms:
    def __init__(self, manage_channels=True, administrator=False):
        self.manage_channels = manage_channels
        self.administrator = administrator


class _Role:
    def __init__(self, role_id, name="Role"):
        self.id, self.name = role_id, name


class _Member:
    def __init__(self, member_id, roles=(), administrator=False, manage_channels=False):
        self.id = member_id
        self.roles = list(roles)
        self.guild_permissions = _Perms(manage_channels, administrator)
        self.display_name = f"user{member_id}"
        self.mention = f"<@{member_id}>"


class _Category(discord.CategoryChannel):
    """A CategoryChannel without a gateway, so isinstance() checks pass."""

    def __init__(self, category_id, name):
        self.id, self.name = category_id, name

    def permissions_for(self, member):
        return _Perms(True)


class _Channel:
    def __init__(self, channel_id=500, category=None, name="ticket-0001"):
        self.id, self.name, self.category = channel_id, name, category
        self.category_id = getattr(category, "id", None)
        self.edits = []

    def permissions_for(self, member):
        return _Perms(True)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "category" in kwargs:
            self.category, self.category_id = kwargs["category"], kwargs["category"].id
        if "name" in kwargs:
            self.name = kwargs["name"]


class _Guild:
    def __init__(self, roles=(), channels=()):
        self.id = 1
        self.default_role = _Role(0, "@everyone")
        self._roles = {role.id: role for role in roles}
        self._channels = {channel.id: channel for channel in channels}
        self.members = {}
        self.me = _Member(999)

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, member_id):
        return self.members.get(member_id)


def _custom_ids(components):
    for component in components or ():
        if isinstance(component, dict):
            if component.get("custom_id"):
                yield component["custom_id"]
            yield from _custom_ids(component.get("components"))


class _Departments:
    """Mixin: give two categories their own staff roles for one test, then restore them."""

    billing_id, support_id, manager_id = 11, 22, 33

    def setUp(self):
        super().setUp()
        self._saved = (dict(config.TICKET_DEPARTMENT_ROLE_IDS),
                       list(config.ALL_DEPARTMENT_ROLE_IDS),
                       list(config.TICKET_CROSS_DEPARTMENT_ROLE_IDS),
                       dict(config.TICKET_CATEGORIES))
        config.TICKET_DEPARTMENT_ROLE_IDS.clear()
        config.TICKET_DEPARTMENT_ROLE_IDS.update({"billing": [self.billing_id],
                                                 "support": [self.support_id]})
        config.ALL_DEPARTMENT_ROLE_IDS[:] = [self.billing_id, self.support_id]
        config.TICKET_CROSS_DEPARTMENT_ROLE_IDS[:] = [self.manager_id]
        config.TICKET_CATEGORIES.update({
            "billing": ("Billing", "TICKET_CATEGORY_ID", 111),
            "support": ("Technical Support", "TICKET_SUPPORT_CATEGORY_ID", 222),
        })
        self.billing_role = _Role(self.billing_id, "Billing")
        self.support_role = _Role(self.support_id, "Technical Support")
        self.manager_role = _Role(self.manager_id, "Manager")

    def tearDown(self):
        roles, all_roles, cross, categories = self._saved
        config.TICKET_DEPARTMENT_ROLE_IDS.clear()
        config.TICKET_DEPARTMENT_ROLE_IDS.update(roles)
        config.ALL_DEPARTMENT_ROLE_IDS[:] = all_roles
        config.TICKET_CROSS_DEPARTMENT_ROLE_IDS[:] = cross
        config.TICKET_CATEGORIES.clear()
        config.TICKET_CATEGORIES.update(categories)
        super().tearDown()


# --------------------------------------------------------------------------- access
class OverwriteTests(_Departments, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.guild = _Guild([self.billing_role, self.support_role, self.manager_role])
        self.owner = _Member(7)
        self.guild.members[7] = self.owner

    def test_move_grants_the_destination_and_locks_out_the_previous_department(self):
        overwrites = Tickets._overwrites(self.guild, 7, department="support",
                                        drop_roles=[self.billing_id])
        self.assertTrue(overwrites[self.support_role].view_channel)
        self.assertTrue(overwrites[self.support_role].send_messages)
        self.assertFalse(overwrites[self.billing_role].view_channel)          # previous department
        self.assertFalse(overwrites[self.billing_role].read_message_history)  # cannot read either
        self.assertTrue(overwrites[self.owner].view_channel)                  # ticket owner
        self.assertTrue(overwrites[self.guild.me].view_channel)               # the bot
        self.assertFalse(overwrites[self.guild.default_role].view_channel)

    def test_cross_department_staff_are_never_removed(self):
        overwrites = Tickets._overwrites(self.guild, 7, department="support",
                                        drop_roles=[self.billing_id, self.manager_id])
        self.assertTrue(overwrites[self.manager_role].view_channel)
        self.assertTrue(overwrites[self.manager_role].send_messages)
        self.assertFalse(overwrites[self.billing_role].view_channel)

    def test_a_closed_ticket_stays_readable_for_its_department(self):
        overwrites = Tickets._overwrites(self.guild, 7, closed=True, department="billing")
        self.assertFalse(overwrites[self.owner].send_messages)
        self.assertTrue(overwrites[self.billing_role].read_message_history)

    def test_an_unconfigured_category_grants_no_department(self):
        overwrites = Tickets._overwrites(self.guild, 7)  # no department known for the ticket
        self.assertTrue(overwrites[self.guild.me].view_channel)
        self.assertFalse(overwrites[self.guild.default_role].view_channel)
        self.assertNotIn(self.billing_role, overwrites)
        self.assertNotIn(self.support_role, overwrites)


class CanManageTests(_Departments, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = Tickets.__new__(Tickets)

    def test_department_staff_are_scoped_to_their_own_category(self):
        billing = _Member(1, [self.billing_role])
        self.assertTrue(self.service.can_manage(billing, None, "billing"))
        self.assertFalse(self.service.can_manage(billing, None, "support"))
        self.assertTrue(self.service.can_manage(_Member(2, [self.support_role]), None, "support"))

    def test_admins_and_cross_department_roles_always_pass(self):
        self.assertTrue(self.service.can_manage(_Member(3, administrator=True), None, "billing"))
        self.assertTrue(self.service.can_manage(_Member(4, [self.manager_role]), None, "support"))

    def test_unrelated_member_is_refused(self):
        self.assertFalse(self.service.can_manage(_Member(5, [_Role(404)]), None, "support"))
        self.assertFalse(self.service.can_manage(object(), None, "support"))

    def test_ticket_outside_every_department_is_open_to_any_department_staff(self):
        self.assertTrue(self.service.can_manage(_Member(6, [self.billing_role]), None, None))


# --------------------------------------------------------------------------- moving
class MoveTests(_Departments, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-move-")
        self.db = Database(os.path.join(self.dir, "move.db"))
        await self.db.connect()
        self.billing_cat = _Category(111, "Billing")
        self.support_cat = _Category(222, "Technical Support")
        self.channel = _Channel(category=self.billing_cat)
        self.guild = _Guild([self.billing_role, self.support_role, self.manager_role],
                            [self.billing_cat, self.support_cat])
        self.service = Tickets.__new__(Tickets)
        self.service.bot = types.SimpleNamespace(db=self.db, get_channel=lambda cid: self.channel)
        self.service._moving, self.service._moved = set(), {}
        self.refreshes = []
        self.service.request_refresh = self.refreshes.append

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _ticket(self):
        return await self.db.create_ticket(
            1, subject="Need a VPS", kind="support", priority="normal",
            client_discord_id=7, client_name="Bibi", channel_id=self.channel.id)

    async def test_a_move_without_a_reason_is_refused(self):
        ticket_id = await self._ticket()
        with self.assertRaises(ValueError) as ctx:
            await self.service.move_ticket(self.guild, ticket_id, _Member(9), "support", "   ")
        self.assertIn("reason", str(ctx.exception).lower())
        self.assertEqual(self.channel.edits, [])   # nothing was changed
        self.assertEqual(self.refreshes, [])

    async def test_move_hands_the_ticket_to_the_destination_department(self):
        ticket_id = await self._ticket()
        actor = _Member(9, [self.support_role], manage_channels=True)
        result = await self.service.move_ticket(self.guild, ticket_id, actor, "support",
                                               "Client paid in full", "support-0001-bibi")

        edit = self.channel.edits[-1]
        self.assertEqual(edit["category"], self.support_cat)
        self.assertEqual(edit["name"], "support-0001-bibi")
        self.assertIn("Client paid in full", edit["reason"])
        self.assertTrue(edit["overwrites"][self.support_role].view_channel)
        self.assertFalse(edit["overwrites"][self.billing_role].view_channel)

        note = (await self.db.ticket_notes(ticket_id))[0]["note"]
        self.assertIn("reason: Client paid in full", note)   # timestamped dashboard note
        self.assertIn("user9", note)                         # ... naming the staff member
        self.assertIn("Billing", note)
        self.assertIn("Technical Support", note)

        moves = [row for row in await self.db.recent_audit(10) if row["action"] == "ticket.move"]
        details = json.loads(moves[0]["details"])
        self.assertEqual(details["reason"], "Client paid in full")
        self.assertEqual(details["by"], "user9")
        self.assertEqual(details["permissions"]["department"], "support")

        self.assertEqual(self.refreshes, [ticket_id])        # dashboard refreshed
        self.assertIn("Technical Support", result)           # and the staff member is told


# --------------------------------------------------------------------------- /revenue
class RevenueStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-revenue-")
        self.db = Database(os.path.join(self.dir, "revenue.db"))
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_revenue_records_every_field_the_command_collects(self):
        revenue_id = await self.db.add_revenue(
            1, "zarbunex", "Commission", 500000, "Hosting referral commission", "2026-10-05",
            received_from="Acme Ltd", method="upi", transaction_id="UTR-9911",
            remarks="part of the Q3 referral deal", recorded_by="Wixz")
        stored = await self.db.get_revenue(revenue_id)
        self.assertEqual(stored["company_id"], "zarbunex")
        self.assertEqual(stored["category"], "Commission")
        self.assertEqual(stored["amount"], 500000)
        self.assertEqual(stored["occurred_on"], "2026-10-05")
        self.assertEqual(stored["received_from"], "Acme Ltd")
        self.assertEqual(stored["method"], "upi")
        self.assertEqual(stored["transaction_id"], "UTR-9911")
        self.assertEqual(stored["remarks"], "part of the Q3 referral deal")
        self.assertEqual(stored["recorded_by"], "Wixz")

    async def test_revenue_reaches_its_company_pnl_for_its_own_month(self):
        await self.db.add_revenue(1, "zarbunex", "Commission", 500000, "Referral", "2026-10-05")
        await self.db.add_revenue(1, "zarbunex", "General", 100000, "Panel resale", "2026-10-06")
        await self.db.add_revenue(1, "hectorhost", "General", 20000, "Misc", "2026-10-07")

        report = await self.db.month_report("2026-10", "zarbunex")
        self.assertEqual(report["income_total"], 0)
        self.assertEqual(report["statement_total"], 600000)
        self.assertEqual(report["revenue"], 600000)          # in the P&L revenue total
        self.assertEqual(report["net"], 600000)
        self.assertEqual(dict(report["statement_rows"])["Commission"], 500000)
        self.assertEqual(dict(report["statement_rows"])["General"], 100000)
        self.assertTrue(pnl_pdf(report).startswith(b"%PDF"))

        # Another company, and another month, stay untouched.
        self.assertEqual((await self.db.month_report("2026-10", "matrixmc"))["revenue"], 0)
        self.assertEqual((await self.db.month_report("2026-09", "zarbunex"))["revenue"], 0)
        self.assertEqual((await self.db.month_report("2026-10", "hectorhost"))["revenue"], 20000)
        self.assertEqual((await self.db.month_report("2026-10"))["revenue"], 620000)

    async def test_investment_is_neither_revenue_nor_profit(self):
        await self.db.add_revenue(1, "hectorhost", "Investment", 1000000, "Seed capital", "2026-10-06")
        report = await self.db.month_report("2026-10", "hectorhost")
        self.assertEqual(report["investment"], 1000000)
        self.assertEqual(report["revenue"], 0)
        self.assertEqual(report["net"], 0)
        self.assertEqual(dict(report["statement_rows"])["Commission"], 0)
        summary = await self.db.revenue_summary("hectorhost")
        self.assertEqual((summary["operating"], summary["investment"]), (0, 1000000))


class RevenueCardTests(unittest.TestCase):
    def test_the_statement_card_shows_every_field(self):
        record = {"id": 3, "company_id": "zarbunex", "category": "General", "amount": 250000,
                  "description": "Panel resale", "received_from": "Acme Ltd", "method": "upi",
                  "transaction_id": "UTR-9", "remarks": "part payment", "occurred_on": "2026-10-05",
                  "recorded_by": "Wixz", "created_at": "2026-10-05T10:00:00"}
        view = ui.revenue_card(record, {"operating": 250000, "investment": 500000})
        text = "\n".join(c["content"] for c in view.to_components()[0]["components"] if c["type"] == 10)
        for label in ("Transaction ID", "Company", "Category", "Amount", "Description", "Date",
                      "Received From", "Payment Method", "Remarks", "Recorded By"):
            self.assertIn(f"**{label}:**", text)
        self.assertIn("REV-0003", text)
        self.assertIn("₹2,500.00", text)
        self.assertIn("**Total Revenue:**", text)
        self.assertIn("Investment (tracked separately)", text)


class RevenueCommandTests(unittest.TestCase):
    def test_revenue_takes_the_same_fields_as_expense_add(self):
        from cogs.finance import Statements

        cog = Statements(object())
        command = next(c for c in cog.get_app_commands() if c.name == "revenue")
        names = {p.name for p in command.parameters}
        for name in ("company", "category", "amount", "description", "received_from", "method",
                     "transaction_id", "remarks", "date"):
            self.assertIn(name, names)
        required = {p.name for p in command.parameters if p.required}
        self.assertLessEqual({"company", "category", "amount", "description", "received_from"},
                             required)


# --------------------------------------------------------------------------- UI
class FinanceDashboardTests(unittest.TestCase):
    def test_revenue_statements_and_investment_are_their_own_lines(self):
        report = {"month": "2026-10", "company": "zarbunex",
                  "income": [("Hosting Revenue", 100000)], "income_total": 100000,
                  "expenses": [], "statement_rows": [("Commission", 500000), ("General", 0)],
                  "statement_total": 500000, "revenue": 600000, "expense_total": 0, "net": 600000,
                  "revenue_rows": [("Commission", 500000), ("Investment", 1000000), ("General", 0)],
                  "revenue_total": 1500000, "investment": 1000000}
        view = views.DashboardView(object(), report, 0)
        text = "\n".join(c["content"] for c in view.to_components()[0]["components"] if c["type"] == 10)
        self.assertIn("**Operating Revenue:** ₹6,000.00", text)
        self.assertIn("Revenue statements (Commission / General)", text)
        self.assertIn("**Investment (tracked separately):** ₹10,000.00", text)


class TicketDashboardTests(_Departments, unittest.TestCase):
    ticket = {"id": 1, "subject": "Need a VPS", "kind": "purchase", "priority": "normal",
              "status": "open", "client_name": "Acme", "client_discord_id": 5,
              "company_id": "zarbunex", "claimed_by": None, "opened_by": 9, "deal_id": None,
              "created_at": "2026-10-27", "last_activity": "2026-10-28"}

    def test_the_dashboard_hides_the_ticket_type_and_company(self):
        fields, _more = views.ticket_blocks(dict(self.ticket), category="Billing")
        self.assertIn("**Category:**", fields)
        self.assertNotIn("**Type:**", fields)
        self.assertNotIn("**Company:**", fields)

    def test_the_panel_has_move_but_no_complete_purchase(self):
        view = views.TicketPanelView(object(), dict(self.ticket))
        ids = set(_custom_ids(view.to_components()))
        self.assertIn("ticket:move:1", ids)
        self.assertIn("ticket:close:1", ids)
        self.assertNotIn("ticket:complete:1", ids)


class MoveModalTests(_Departments, unittest.TestCase):
    def test_the_form_requires_a_destination_and_a_reason(self):
        channel = _Channel(category=_Category(111, "Billing"))
        modal = views.MoveTicketModal({"id": 4, "channel_id": channel.id}, channel)
        labels = {c["label"]: c["component"] for c in modal.to_components() if c["type"] == 18}
        self.assertEqual(labels["Destination category"]["custom_id"], "move:destination")
        self.assertTrue(labels["Destination category"]["required"])
        self.assertEqual(labels["Reason for moving"]["custom_id"], "move:reason")
        self.assertTrue(labels["Reason for moving"]["required"])
        self.assertEqual(sorted(o["label"] for o in labels["Destination category"]["options"]),
                         ["Billing", "Technical Support"])
        self.assertEqual([o["label"] for o in labels["Destination category"]["options"]
                          if o.get("description") == "Current category"], ["Billing"])

    def test_submitted_values_reach_the_handler(self):
        channel = _Channel(category=_Category(111, "Billing"))
        modal = views.MoveTicketModal({"id": 4, "channel_id": channel.id}, channel)
        modal._refresh(None, [
            {"type": 18, "component": {"type": 3, "custom_id": "move:destination",
                                       "values": ["support"]}},
            {"type": 18, "component": {"type": 4, "custom_id": "move:reason", "value": "client paid"}},
        ], {})
        self.assertEqual(modal.destination.values, ["support"])
        self.assertEqual(modal.reason.value, "client paid")


if __name__ == "__main__":
    unittest.main()
