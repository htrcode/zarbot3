"""Client ticket system: numbered channels, claim/close/reopen, transcripts and linked follow-ups."""
import asyncio
import hashlib
import json
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import ui
from desk.database import ticket_number
from desk.utils import (TICKET_KINDS, TICKET_PRIORITIES, days_since, fmt, is_ticket_staff,
                   safe_channel_name, staff_interaction_check, to_cents)
from desk.views import TicketOpenPanelView, build_ticket_view, simple_card

log = logging.getLogger("invoice-bot.tickets")

REFRESH_DELAY = 0.5  # seconds: bursts of changes to one ticket become a single dashboard edit


def _custom_ids(components):
    for c in components or []:
        if getattr(c, "custom_id", None):
            yield c.custom_id
        yield from _custom_ids(getattr(c, "children", None))


class Tickets(commands.GroupCog, group_name="ticket", group_description="Client tickets and follow-ups"):
    def __init__(self, bot):
        self.bot = bot
        self.bot.tickets = self  # views call back into this cog
        super().__init__()
        self._creating: set[int] = set()   # users whose ticket is being created (double-click guard)
        self._moving: set[int] = set()     # tickets being moved right now
        self._pending: set[int] = set()    # tickets with a dashboard refresh already queued
        self._locks: dict[int, asyncio.Lock] = {}
        self._rendered: dict[int, tuple] = {}   # ticket id -> (message id, fingerprint) last written
        self._moved: dict[int, Optional[str]] = {}  # category name just applied (gateway cache may lag)
        self._tasks: set = set()
        self.stale_digest.start()

    async def cog_load(self):
        self.bot.db.subscribe(self._on_db_event)  # dashboards follow database changes
        if not config.TICKET_DEFAULT_CATEGORY_ID:
            log.warning("TICKET_DEFAULT_CATEGORY_ID is not set: new tickets go to the Billing category "
                        "(TICKET_CATEGORY_ID) or the server root.")
        # Re-attach button handlers so panels keep working after a restart.
        self.bot.add_view(TicketOpenPanelView(self.bot))
        rows = await self.bot.db.fetchall("SELECT id FROM tickets ORDER BY id DESC LIMIT 300")
        for row in rows:
            view = await build_ticket_view(self.bot, row["id"])
            if view:
                self.bot.add_view(view)
        log.info("Restored %s ticket panels", len(rows))

    async def cog_unload(self):
        self.stale_digest.cancel()
        try:
            self.bot.db.listeners.remove(self._on_db_event)
        except ValueError:
            pass
        for task in list(self._tasks):
            task.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _overwrites(guild: discord.Guild, client_id: int | None, closed: bool = False):
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        member = guild.get_member(client_id) if client_id else None
        target = member or (discord.Object(id=client_id) if client_id else None)
        if target:
            overwrites[target] = discord.PermissionOverwrite(
                view_channel=not closed, send_messages=not closed, attach_files=not closed,
                read_message_history=True, add_reactions=not closed, embed_links=not closed,
            )
        for role_id in config.TICKET_STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=not closed, attach_files=not closed,
                    read_message_history=True, manage_messages=True, add_reactions=not closed,
                )
        me = guild.me
        if me:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, read_message_history=True,
                manage_channels=True, manage_messages=True, embed_links=True,
            )
        return overwrites

    @staticmethod
    def _category(guild: discord.Guild, category_id):
        if not category_id:
            return None
        channel = guild.get_channel(category_id)
        return channel if isinstance(channel, discord.CategoryChannel) else None

    async def _respond(self, interaction: discord.Interaction, text: str, *, ephemeral: bool = True):
        await ui.say(interaction, text, ephemeral=ephemeral)

    async def _channel(self, channel_id):
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except (discord.HTTPException, TypeError):
            return None

    async def _resolve(self, interaction: discord.Interaction, ticket_id: int | None) -> dict:
        ticket = await self.bot.db.get_ticket(ticket_id) if ticket_id else \
            await self.bot.db.ticket_by_channel(interaction.channel_id)
        if not ticket:
            raise ValueError("Ticket not found. Use this inside a ticket channel or pass a ticket ID.")
        return ticket

    # ------------------------------------------------------------------ live dashboard
    def begin_create(self, user_id: int) -> bool:
        if user_id in self._creating:
            return False
        self._creating.add(user_id)
        return True

    def end_create(self, user_id: int):
        self._creating.discard(user_id)

    async def _on_db_event(self, event: str, payload: dict):
        if event == "ticket.updated":
            self.request_refresh(payload["ticket_id"])

    def request_refresh(self, ticket_id: int):
        """Queue ONE in-place dashboard edit. Calls arriving within REFRESH_DELAY share it."""
        if ticket_id in self._pending:
            return
        self._pending.add(ticket_id)
        task = asyncio.create_task(self._refresh_later(ticket_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refresh_later(self, ticket_id: int):
        try:
            await asyncio.sleep(REFRESH_DELAY)
            self._pending.discard(ticket_id)  # changes from here on queue a new refresh
            await self.refresh_dashboard(ticket_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Dashboard refresh failed for ticket %s", ticket_id)
        finally:
            self._pending.discard(ticket_id)

    async def _find_panel(self, channel, ticket_id: int) -> Optional[int]:
        """Tickets from before panel ids were stored: find the bot's dashboard message once."""
        me = self.bot.user.id if self.bot.user else 0
        wanted = f"ticket:claim:{ticket_id}"
        try:
            async for message in channel.history(limit=100):
                if message.author.id == me and wanted in set(_custom_ids(message.components)):
                    return message.id
        except discord.HTTPException:
            log.debug("Could not search %s for the dashboard", channel.id, exc_info=True)
        return None

    async def refresh_dashboard(self, ticket_id: int, channel=None) -> bool:
        """Re-render the ticket dashboard from the database and edit its message in place.

        The message is posted only if it does not exist (first time, or someone deleted it);
        an unchanged dashboard costs no API call.
        """
        async with self._locks.setdefault(ticket_id, asyncio.Lock()):
            ticket = await self.bot.db.get_ticket(ticket_id)
            if not ticket or not ticket.get("channel_id"):
                return False
            channel = channel or await self._channel(ticket["channel_id"])
            if channel is None:
                return False
            category = self._moved.pop(ticket_id, None) or getattr(getattr(channel, "category", None), "name", None)
            view = await build_ticket_view(self.bot, ticket_id, category)
            if view is None:
                return False
            fingerprint = hashlib.sha1(
                json.dumps(view.to_components(), sort_keys=True, default=str).encode()).hexdigest()
            message_id = ticket.get("panel_message_id") or await self._find_panel(channel, ticket_id)
            try:
                if message_id:
                    if self._rendered.get(ticket_id) == (message_id, fingerprint):
                        return True  # already showing exactly this
                    try:
                        await channel.get_partial_message(message_id).edit(view=view)
                    except discord.NotFound:
                        message_id = None  # the dashboard was deleted: post a fresh one below
                if not message_id:
                    message_id = (await channel.send(view=view)).id
                if message_id != ticket.get("panel_message_id"):
                    await self.bot.db.set_ticket_panel(ticket_id, message_id)
                self._rendered[ticket_id] = (message_id, fingerprint)
                return True
            except discord.HTTPException:
                log.warning("Could not update the dashboard of ticket %s", ticket_id, exc_info=True)
                return False

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        """Moved or renamed by hand (or by us): the dashboard shows the category."""
        if not isinstance(after, discord.TextChannel):
            return
        if before.category_id == after.category_id and before.name == after.name:
            return
        ticket = await self.bot.db.ticket_by_channel(after.id)
        if ticket:
            self.request_refresh(ticket["id"])

    # ------------------------------------------------------------------ create
    async def open_ticket(self, interaction: discord.Interaction, *, client: Optional[discord.Member],
                          kind: str = "purchase", subject: str = "", details: str = "",
                          service: str = "", budget: int = 0, parent_id: Optional[int] = None,
                          priority: str = "normal") -> Optional[int]:
        """Create the channel, the ticket row and the panel. Used by commands and panels."""
        guild = interaction.guild
        if guild is None:
            return None
        kind = (kind or "support").lower()
        if kind not in TICKET_KINDS:
            await self._respond(interaction, "❌ Unknown ticket type.")
            return None
        priority = (priority or "normal").lower()
        if priority not in TICKET_PRIORITIES:
            priority = "normal"
        subject = (subject or "").strip()[:120]
        if not subject:
            await self._respond(interaction, "❌ Add a short subject.")
            return None

        parent = None
        if parent_id:
            parent = await self.bot.db.get_ticket(parent_id)
            if not parent:
                await self._respond(interaction, "❌ The ticket you are following up doesn't exist.")
                return None
            if client is None and parent.get("client_discord_id"):
                client = guild.get_member(parent["client_discord_id"])
                if client is None:
                    try:
                        client = await guild.fetch_member(parent["client_discord_id"])
                    except discord.HTTPException:
                        client = None
            service = service or ""
            if not details:
                details = f"Follow-up of {ticket_number(parent['id'])} — {parent['subject']}"

        if client is None:
            await self._respond(interaction, "❌ Pick the client for this ticket.")
            return None
        if not is_ticket_staff(interaction.user):
            open_count = await self.bot.db.count_open_tickets(client.id)
            if config.TICKET_MAX_OPEN_PER_USER and open_count >= config.TICKET_MAX_OPEN_PER_USER:
                await self._respond(
                    interaction,
                    f"❌ {client.mention} already has {open_count} open tickets. "
                    "Close one first or raise `TICKET_MAX_OPEN_PER_USER`.",
                )
                return None

        # New tickets go to the "Tickets" category (Billing / server root only if it is not configured).
        category_id = config.TICKET_DEFAULT_CATEGORY_ID or config.TICKET_CATEGORY_ID
        category = self._category(guild, category_id)
        if category_id and category is None:
            log.error("Ticket category %s was not found in guild %s", category_id, guild.id)
            await self._respond(interaction, "❌ The tickets category is not set up correctly. "
                                             "Please tell a staff member.")
            return None
        channel = None
        try:
            channel = await guild.create_text_channel(
                safe_channel_name(f"{config.TICKET_PREFIX}-{subject}", 90),
                category=category,
                overwrites=self._overwrites(guild, client.id),
                topic=f"{kind.title()} · {subject[:100]} · client {client}"[:1024],
                reason=f"Ticket opened by {interaction.user}",
            )
            ticket_id = await self.bot.db.create_ticket(
                interaction.user.id, subject=subject, kind=kind, priority=priority,
                client_discord_id=client.id, client_name=client.display_name,
                channel_id=channel.id, parent_ticket_id=parent["id"] if parent else None,
                budget=budget, service=service[:120], note=details,
            )
        except discord.HTTPException as e:
            if channel:
                try:
                    await channel.delete(reason="Ticket creation failed")
                except discord.HTTPException:
                    pass
            log.exception("Could not create ticket channel")
            await self._respond(interaction, f"❌ Could not create the ticket channel: {e}")
            return None
        except Exception as e:
            if channel:
                try:
                    await channel.delete(reason="Ticket creation failed")
                except discord.HTTPException:
                    pass
            log.exception("Could not create ticket")
            await self._respond(interaction, f"❌ Could not create the ticket: {e}")
            return None

        label = ticket_number(ticket_id)
        try:
            await channel.edit(name=f"{config.TICKET_PREFIX}-{ticket_id:04d}",
                               topic=f"{label} · {kind.title()} · {subject[:90]} · client {client}"[:1024])
        except discord.HTTPException:
            log.warning("Could not rename ticket channel %s", channel.id)

        # Acknowledge first — channel creation is the slow part. A failed reply must
        # never prevent the channel/panel from being set up.
        try:
            await self._respond(
                interaction,
                f"✅ Ticket {label} created: {channel.mention}\n"
                f"Client: {client.mention} · Type: **{kind}** · "
                f"{('Follow-up of ' + ticket_number(parent['id'])) if parent else 'New ticket'}",
            )
        except discord.HTTPException:
            log.warning("Ticket %s created but the confirmation reply failed", ticket_id)

        body = [f"{client.mention} your ticket **{label}** is open. "
                "A staff member will reply here — no need to open another ticket."]
        if details:
            clean = discord.utils.escape_mentions(details.strip())[:1500]
            body.append(f"**Request Details**\n{clean}")
        if parent:
            body.append(f"🔗 Follow-up of **{ticket_number(parent['id'])}**.")
        try:
            await channel.send(view=ui.build_card(f"Ticket {label} is open", *body, timeout=180),
                               allowed_mentions=discord.AllowedMentions(users=[client]))
            await self.refresh_dashboard(ticket_id, channel)  # posts the dashboard once and remembers it
        except discord.HTTPException:
            log.exception("Ticket %s created but the panel could not be posted", ticket_id)
        return ticket_id

    # ------------------------------------------------------------------ close / reopen
    async def save_transcript(self, ticket_id: int, guild: Optional[discord.Guild] = None,
                              channel=None) -> str:
        ticket = await self.bot.db.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found.")
        if channel is None:
            channel = self.bot.get_channel(ticket.get("channel_id") or 0)
            if channel is None and guild:
                try:
                    channel = await self.bot.fetch_channel(ticket["channel_id"])
                except (discord.HTTPException, TypeError):
                    channel = None
        if channel is None:
            raise ValueError("The ticket channel no longer exists, so there is nothing to transcribe.")
        os.makedirs(config.TICKET_TRANSCRIPT_DIR, exist_ok=True)
        path = os.path.join(config.TICKET_TRANSCRIPT_DIR, f"{ticket_number(ticket_id)}.txt")
        lines = [
            f"Ticket {ticket_number(ticket_id)} — {ticket['subject']}",
            f"Client: {ticket['client_name']} ({ticket.get('client_discord_id') or 'unknown'})",
            f"Type: {ticket['kind']} · Priority: {ticket['priority']} · Status: {ticket['status']}",
            f"Opened: {ticket['created_at']} · Last activity: {ticket['last_activity']}",
            f"Claimed by: {ticket.get('claimed_by') or 'nobody'}",
            "-" * 70,
            "",
        ]
        count = 0
        try:
            async for message in channel.history(limit=5000, oldest_first=True):
                count += 1
                body = (message.clean_content or "").strip().replace("\n", "\n    ")
                if body:
                    lines.append(f"[{message.created_at:%Y-%m-%d %H:%M}] {message.author} ({message.author.id}): {body}")
                for attachment in message.attachments:
                    lines.append(f"    attachment: {attachment.url}")
        except discord.HTTPException as e:
            lines.append(f"[transcript incomplete: {e}]")
        if not count:
            lines.append("(no messages)")
        text = "\n".join(lines) + "\n"

        def _write():
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)

        await asyncio.to_thread(_write)
        return path

    async def _log(self, title: str, body: str = "", file_path: str | None = None):
        """Post a flat V2 card (optionally with a transcript attached) to the ticket log channel."""
        channel = self.bot.get_channel(config.TICKET_LOG_CHANNEL_ID) if config.TICKET_LOG_CHANNEL_ID else None
        if channel is None:
            return
        try:
            if file_path and os.path.exists(file_path):
                name = os.path.basename(file_path)
                await channel.send(view=ui.file_card(title, body, name),
                                   file=discord.File(file_path, filename=name))
            else:
                await channel.send(view=ui.build_card(title, body, timeout=180))
        except discord.HTTPException:
            log.exception("Could not post to the ticket log channel")
            try:
                await channel.send(view=ui.build_card(title, body, timeout=180))
            except discord.HTTPException:
                pass

    async def send_transcript(self, interaction: discord.Interaction, ticket_id: int, path: str):
        """Send a saved transcript privately as a V2 card with the file attached."""
        name = os.path.basename(path)
        card = ui.file_card(f"Transcript · {ticket_number(ticket_id)}", "", name)
        await interaction.followup.send(view=card, file=discord.File(path, filename=name), ephemeral=True)

    async def close_ticket(self, guild: discord.Guild, ticket_id: int, actor, reason: str = "") -> dict:
        ticket = await self.bot.db.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found.")
        if ticket["status"] == "closed":
            raise ValueError("That ticket is already closed.")
        channel = await self._channel(ticket.get("channel_id"))
        transcript = None
        if channel:
            try:
                transcript = await self.save_transcript(ticket_id, guild, channel)
            except (ValueError, OSError) as e:
                log.warning("Transcript failed for ticket %s: %s", ticket_id, e)
            kwargs = dict(
                name=f"{config.TICKET_CLOSED_PREFIX}-{ticket_id:04d}",
                overwrites=self._overwrites(guild, ticket.get("client_discord_id"), closed=True),
                reason=f"Closed by {actor}",
            )
            closed_category = self._category(guild, config.TICKET_CLOSED_CATEGORY_ID)
            if closed_category:
                kwargs["category"] = closed_category
                self._moved[ticket_id] = closed_category.name
            try:
                await channel.edit(**kwargs)
                await channel.send(view=ui.build_card(
                    "Ticket closed",
                    f"{ui._field('Closed By', actor.mention)}\n"
                    f"{ui._field('Reason', discord.utils.escape_mentions(reason or 'not given'))}",
                    timeout=180))
            except discord.HTTPException:
                log.exception("Could not lock ticket channel %s", ticket_id)
        await self.bot.db.set_ticket_status(actor.id, ticket_id, "closed", reason, transcript)
        await self._log(
            f"Ticket closed · {ticket_number(ticket_id)}",
            "\n".join([ui._field("Closed By", actor.mention),
                       ui._field("Client Name", ticket["client_name"]),
                       ui._field("Type", ticket["kind"]),
                       ui._field("Reason", reason or "—")]), transcript)
        return await self.bot.db.get_ticket(ticket_id)

    async def reopen_ticket(self, guild: discord.Guild, ticket_id: int, actor) -> dict:
        ticket = await self.bot.db.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found.")
        if ticket["status"] != "closed":
            raise ValueError("That ticket is not closed.")
        channel = await self._channel(ticket.get("channel_id"))
        if channel:
            kwargs = dict(
                name=f"{config.TICKET_PREFIX}-{ticket_id:04d}",
                overwrites=self._overwrites(guild, ticket.get("client_discord_id"), closed=False),
                reason=f"Reopened by {actor}",
            )
            category = self._category(guild, config.TICKET_DEFAULT_CATEGORY_ID or config.TICKET_CATEGORY_ID)
            if category:
                kwargs["category"] = category
                self._moved[ticket_id] = category.name
            try:
                await channel.edit(**kwargs)
                await channel.send(view=ui.build_card(
                    "Ticket reopened", ui._field("Reopened By", actor.mention), timeout=180))
            except discord.HTTPException:
                log.exception("Could not reopen ticket channel %s", ticket_id)
        await self.bot.db.set_ticket_status(actor.id, ticket_id, "open")
        await self._log(f"Ticket reopened · {ticket_number(ticket_id)}",
                        "\n".join([ui._field("Reopened By", actor.mention),
                                   ui._field("Client Name", ticket["client_name"])]))
        # The existing dashboard is edited in place by the database event (no second panel).
        return await self.bot.db.get_ticket(ticket_id)

    # ------------------------------------------------------------------ complete purchase
    def purchase_target(self, guild: discord.Guild, key: str):
        """(label, category) for 'support' or 'partnership'. ValueError says what to fix."""
        label, cid, var = {
            "support": ("Technical Support", config.TICKET_SUPPORT_CATEGORY_ID, "TICKET_SUPPORT_CATEGORY_ID"),
            "partnership": ("Partnership", config.TICKET_PARTNERSHIP_CATEGORY_ID, "TICKET_PARTNERSHIP_CATEGORY_ID"),
        }[key]
        if not cid:
            raise ValueError(f"The {label} category is not configured. Set `{var}` in config.py.")
        category = self._category(guild, cid)
        if category is None:
            raise ValueError(f"The {label} category (`{cid}`) was not found in this server. Check `{var}`.")
        return label, category

    async def complete_purchase(self, guild: discord.Guild, ticket_id: int, actor, key: str,
                                details: dict) -> bool:
        """Rename + move the SAME ticket channel and post the purchase summary.

        Returns False if the move worked but the summary could not be posted.
        Raises ValueError (already worded for staff) when nothing was changed.
        """
        ticket = await self.bot.db.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found.")
        if ticket["status"] == "closed":
            raise ValueError("That ticket is closed. Reopen it first.")
        if ticket.get("purchase_completed_at"):
            raise ValueError("This purchase was already completed.")
        label, category = self.purchase_target(guild, key)
        channel = await self._channel(ticket.get("channel_id"))
        if channel is None:
            raise ValueError("The ticket channel no longer exists.")
        me = guild.me
        if me and not (channel.permissions_for(me).manage_channels
                       and category.permissions_for(me).manage_channels):
            raise ValueError(f"I need **Manage Channels** on this ticket and on the **{label}** category.")

        # Claim first (atomic), so a double click / two staff members can never post twice.
        if not await self.bot.db.claim_purchase_completion(actor.id, ticket_id, {**details, "moved_to": label}):
            raise ValueError("This purchase was already completed.")
        who = details["client"] if details["client"] != "Not provided" else ticket["client_name"]
        try:
            # No sync_permissions: the channel keeps its own overwrites, messages and history.
            await channel.edit(name=safe_channel_name(f"{key}-{ticket_id:04d}-{who}", 90), category=category,
                               reason=f"Purchase completed by {actor}")
        except discord.Forbidden:
            await self.bot.db.release_purchase_completion(actor.id, ticket_id)
            raise ValueError("Discord refused the move (missing permissions). Nothing was changed.")
        except discord.HTTPException as e:
            await self.bot.db.release_purchase_completion(actor.id, ticket_id)
            raise ValueError(f"Discord could not move the channel ({e.status}). Nothing was changed, try again.")

        self._moved[ticket_id] = category.name
        self.request_refresh(ticket_id)  # dashboard: new category, Complete Purchase greyed out
        clean = lambda v: discord.utils.escape_markdown(discord.utils.escape_mentions(str(v)))[:200]
        body = "\n".join([
            ui._field("Client Name", clean(details["client"])),
            ui._field("Service Name", clean(details["service"])),
            ui._field("Payment Status", clean(details["payment"])),
            ui._field("Server Name / UUID", clean(details["server"])),
            ui._field("Expiry Date", clean(details["expiry"])),
            ui._field("Moved To", label),
            ui._field("Completed By", actor.mention),
        ])
        title = f"Purchase completed · {ticket_number(ticket_id)}"
        await self._log(title, body)
        try:
            await channel.send(view=ui.build_card(title, body, timeout=180))
            return True
        except discord.HTTPException:
            log.exception("Ticket %s moved but the purchase summary could not be posted", ticket_id)
            return False

    # ------------------------------------------------------------------ move / rename
    def category_target(self, guild: discord.Guild, key: str):
        """(label, category) for a key of config.TICKET_CATEGORIES. ValueError says what to fix."""
        label, var, cid = config.TICKET_CATEGORIES[key]
        if not cid:
            raise ValueError(f"The {label} category is not configured. Set `{var}` in config.py.")
        category = self._category(guild, cid)
        if category is None:
            raise ValueError(f"The {label} category (`{cid}`) was not found in this server. Check `{var}`.")
        return label, category

    async def move_ticket(self, guild: discord.Guild, ticket_id: int, actor, key: str,
                          new_name: Optional[str] = None) -> str:
        """Move (and optionally rename) the SAME channel. Permissions, messages and records stay as they are.

        Raises ValueError (worded for staff) when nothing was changed.
        """
        ticket = await self.bot.db.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found.")
        if ticket["status"] == "closed":
            raise ValueError("That ticket is closed. Reopen it first.")
        label, category = self.category_target(guild, key)
        channel = await self._channel(ticket.get("channel_id"))
        if channel is None:
            raise ValueError("The ticket channel no longer exists.")

        wanted = safe_channel_name(new_name, 90) if (new_name or "").strip() else channel.name
        changes = {}
        if channel.category_id != category.id:
            changes["category"] = category
        if wanted != channel.name:
            changes["name"] = wanted
        if not changes:
            raise ValueError("Nothing to change: the ticket is already in that category with that name.")
        me = guild.me
        if me and not (channel.permissions_for(me).manage_channels
                       and ("category" not in changes or category.permissions_for(me).manage_channels)):
            raise ValueError(f"I need **Manage Channels** on this ticket and on the **{label}** category.")
        if ticket_id in self._moving:
            raise ValueError("This ticket is already being moved. Wait a moment and check the channel.")

        before = {"category": getattr(channel.category, "name", None), "name": channel.name}
        self._moving.add(ticket_id)
        try:
            # No sync_permissions / overwrites: the channel keeps its own permissions and history.
            await asyncio.wait_for(channel.edit(reason=f"Moved by {actor}", **changes), timeout=20)
        except asyncio.TimeoutError:
            raise ValueError("Discord did not answer in time (channel edits are rate limited, renames to "
                             "2 per 10 minutes). Check the channel, then try again later.")
        except discord.Forbidden:
            raise ValueError("Discord refused the change (missing permissions). Nothing was changed.")
        except discord.HTTPException as e:
            raise ValueError(f"Discord could not edit the channel ({e.status}: {(e.text or '')[:100]}). "
                             "Nothing was changed.")
        finally:
            self._moving.discard(ticket_id)

        self._moved[ticket_id] = category.name if "category" in changes else before["category"]
        try:
            await self.bot.db.record_ticket_move(actor.id, ticket_id, {
                "from": before, "to": {"category": label if "category" in changes else before["category"],
                                       "name": wanted}})  # also refreshes the dashboard
        except Exception:
            log.exception("Ticket %s moved but the history entry failed", ticket_id)
            self.request_refresh(ticket_id)
        await self._log(f"Ticket moved · {ticket_number(ticket_id)}", "\n".join([
            ui._field("Moved By", actor.mention),
            ui._field("Category", f"{before['category'] or '—'} → {label}" if "category" in changes
                      else before["category"] or "—"),
            ui._field("Channel", f"#{before['name']} → #{wanted}" if "name" in changes else f"#{wanted}")]))
        done = []
        if "category" in changes:
            done.append(f"moved to **{label}**")
        if "name" in changes:
            done.append(f"renamed to **#{wanted}**")
        return f"{ticket_number(ticket_id)} " + " and ".join(done) + "."

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="open", description="Open a numbered ticket channel for a client")
    @app_commands.choices(kind=[app_commands.Choice(name=k.title(), value=k) for k in TICKET_KINDS],
                          priority=[app_commands.Choice(name=p.title(), value=p) for p in TICKET_PRIORITIES])
    async def open_cmd(self, interaction: discord.Interaction, client: discord.Member, subject: str,
                       kind: str = "purchase", priority: str = "normal",
                       budget: app_commands.Range[float, 0.0, 100_000_000.0] = 0.0,
                       service: Optional[str] = None, details: Optional[str] = None,
                       parent: Optional[int] = None):
        await self.open_ticket(
            interaction, client=client, kind=kind, subject=subject, details=details or "",
            service=service or "", budget=to_cents(budget), parent_id=parent, priority=priority,
        )

    @app_commands.command(name="panel", description="Post the public ticket-opening panel")
    async def panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        target = channel or interaction.channel
        if target is None:
            return await ui.say(interaction, "❌ Pick a channel for the panel.")
        await interaction.response.defer(ephemeral=True)
        try:
            await target.send(view=TicketOpenPanelView(self.bot))
        except discord.HTTPException as e:
            return await ui.say(interaction, f"❌ Could not post the panel: {e}")
        await ui.say(interaction, f"✅ Ticket panel posted in {target.mention}.")

    @app_commands.command(name="info", description="Show a ticket panel")
    async def info(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        view = await build_ticket_view(self.bot, row["id"])
        if not view:
            return await ui.say(interaction, "❌ Ticket not found.")
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="list", description="List tickets (filter by status, type or client)")
    @app_commands.choices(status=[app_commands.Choice(name=s.title(), value=s)
                                  for s in ("open", "pending", "closed", "all")],
                          kind=[app_commands.Choice(name=k.title(), value=k) for k in TICKET_KINDS])
    async def list_cmd(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None,
                       kind: Optional[app_commands.Choice[str]] = None,
                       client: Optional[discord.Member] = None):
        rows = await self.bot.db.list_tickets(status.value if status else "open",
                                              kind.value if kind else None,
                                              client.id if client else None, limit=25)
        if not rows:
            return await interaction.response.send_message(
                view=simple_card("Tickets", "No tickets match that filter."), ephemeral=True)
        lines = []
        for row in rows:
            icon = {"open": "🟢", "pending": "🟡", "closed": "⚫"}.get(row["status"], "•")
            claim = f" · <@{row['claimed_by']}>" if row.get("claimed_by") else " · unclaimed"
            channel = f"<#{row['channel_id']}>" if row.get("channel_id") else "no channel"
            lines.append(f"{icon} **{ticket_number(row['id'])}** · {row['kind']} · {row['priority']} · "
                         f"{row['client_name'][:20]} · {channel}{claim} · {row['last_activity'][:10]}")
        await interaction.response.send_message(
            view=simple_card("Tickets · " + (status.value if status else "open"), "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="claim", description="Claim (or unclaim) a ticket")
    async def claim(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        claimed = None if row.get("claimed_by") == interaction.user.id else interaction.user.id
        await self.bot.db.set_ticket_claimed(interaction.user.id, row["id"], claimed)
        await ui.say(interaction, f"{'🙋 Claimed' if claimed else '↩️ Released'} **{ticket_number(row['id'])}**.")

    @app_commands.command(name="assign", description="Assign a ticket to a staff member")
    async def assign(self, interaction: discord.Interaction, staff: discord.Member,
                     ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        if not is_ticket_staff(staff):
            return await ui.say(interaction, "❌ That member has no ticket staff role. Add their role to `TICKET_STAFF_ROLE_IDS` first.")
        await self.bot.db.set_ticket_claimed(interaction.user.id, row["id"], staff.id)
        await ui.say(interaction, f"🙋 **{ticket_number(row['id'])}** assigned to {staff.mention}.")

    @app_commands.command(name="note", description="Save an internal note on a ticket")
    async def note(self, interaction: discord.Interaction, note: str, ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
            await self.bot.db.add_ticket_note(interaction.user.id, row["id"], note)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, "📝 Note saved.")

    @app_commands.command(name="priority", description="Change a ticket's priority")
    @app_commands.choices(priority=[app_commands.Choice(name=p.title(), value=p) for p in TICKET_PRIORITIES])
    async def priority_cmd(self, interaction: discord.Interaction, priority: app_commands.Choice[str],
                           ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
            await self.bot.db.set_ticket_priority(interaction.user.id, row["id"], priority.value)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"✅ **{ticket_number(row['id'])}** is now **{priority.value}**.")

    @app_commands.command(name="close", description="Close a ticket, lock the channel and save a transcript")
    async def close_cmd(self, interaction: discord.Interaction, ticket: Optional[int] = None,
                        reason: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self._resolve(interaction, ticket)
            await self.close_ticket(interaction.guild, row["id"], interaction.user, reason or "")
        except (ValueError, discord.HTTPException) as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"⚫ **{ticket_number(row['id'])}** closed.")

    @app_commands.command(name="reopen", description="Reopen a closed ticket")
    async def reopen_cmd(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self._resolve(interaction, ticket)
            await self.reopen_ticket(interaction.guild, row["id"], interaction.user)
        except (ValueError, discord.HTTPException) as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"🔓 **{ticket_number(row['id'])}** reopened.")

    @app_commands.command(name="transcript", description="Download a ticket transcript")
    async def transcript(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self._resolve(interaction, ticket)
            path = await self.save_transcript(row["id"], interaction.guild)
        except (ValueError, OSError, discord.HTTPException) as e:
            return await ui.say(interaction, f"❌ {e}")
        await self.send_transcript(interaction, row["id"], path)

    @app_commands.command(name="dashboard", description="Ticket and sales overview")
    async def dashboard(self, interaction: discord.Interaction):
        stats = await self.bot.db.ticket_stats()
        pipeline = await self.bot.db.pipeline()
        stale = await self.bot.db.stale_tickets(config.TICKET_STALE_DAYS)
        lines = [
            f"**Open:** {stats['open']}  ·  **Pending:** {stats['pending']}  ·  "
            f"**Closed:** {stats['closed']}  ·  **Unclaimed:** {stats['unclaimed']}",
            "",
            "**Sales pipeline**",
        ]
        for row in pipeline:
            if row["stage"] in ("won", "lost"):
                continue
            lines.append(f"• {row['stage'].title()}: **{row['count']}** deals · "
                         f"value {fmt(row['value'])} · weighted {fmt(row['weighted'])}")
        total = sum(row["count"] for row in pipeline)
        if not total:
            lines.append("• No deals yet — buying-enquiry tickets create them automatically.")
        lines += ["", f"**Stale tickets** (no activity for {config.TICKET_STALE_DAYS}+ days): {len(stale)}"]
        for row in stale[:8]:
            link = f"<#{row['channel_id']}>" if row.get("channel_id") else ticket_number(row["id"])
            idle = days_since((row.get("last_activity") or "")[:10])
            lines.append(f"• {link} · {row['client_name'][:20]} · "
                         f"{idle if idle is not None else '?'}d · {row['subject'][:40]}")
        await interaction.response.send_message(view=simple_card("🎫 Ticket dashboard", "\n".join(lines)),
                                                ephemeral=True)

    # ------------------------------------------------------------------ background
    @tasks.loop(hours=24)
    async def stale_digest(self):
        try:
            stale = await self.bot.db.stale_tickets(config.TICKET_STALE_DAYS)
            if not stale:
                return
            lines = []
            for row in stale[:15]:
                channel = f"<#{row['channel_id']}>" if row.get("channel_id") else ticket_number(row["id"])
                lines.append(f"• {channel} · {row['client_name']} · {row['subject'][:60]} · "
                             f"last {row['last_activity'][:10]}")
            await self._log(f"Stale tickets · {len(stale)}",
                            f"No activity for {config.TICKET_STALE_DAYS}+ days:\n" + "\n".join(lines))
        except Exception:
            log.exception("Stale ticket digest failed")

    @stale_digest.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        """Close the ticket row when its channel is deleted manually."""
        try:
            ticket = await self.bot.db.ticket_by_channel(channel.id)
            if not ticket or ticket["status"] == "closed":
                return
            actor_id = self.bot.user.id if self.bot.user else 0
            await self.bot.db.set_ticket_status(actor_id, ticket["id"], "closed", "Channel deleted")
            await self._log(f"Ticket closed · {ticket_number(ticket['id'])}",
                            "The ticket channel was deleted, so the ticket was marked closed.")
        except Exception:
            log.exception("Could not close ticket for deleted channel %s", channel.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        try:
            if await self.bot.db.ticket_by_channel(message.channel.id):
                await self.bot.db.touch_ticket(message.channel.id)
        except Exception:
            log.debug("Could not touch ticket activity", exc_info=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
