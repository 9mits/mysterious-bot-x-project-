"""Modmail relay: ticket creation, control views, modals, and ticket management."""

import discord
from discord.ext import commands
from typing import Optional, Union

import io
from types import SimpleNamespace

from core.constants import (
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    EMBED_PALETTE,
    MODMAIL_PANEL_CATEGORIES,
    SCOPE_SUPPORT,
)
from core.services import (
    DEFAULT_TICKET_PRIORITIES,
    normalize_modmail_ticket,
    sanitize_tags,
)
from core.context import bot
from core.utils import iso_to_dt, now_iso
from .shared import (
    logger,
    truncate_text,
    make_embed,
    brand_embed,
    make_confirmation_embed,
    join_lines,
    get_context_guild,
    respond_with_error,
    is_staff,
    send_modmail_thread_intro,
    upsert_embed_field,
    get_modal_item_label,
    build_canned_replies_embed,
)
from .cases import _split_case_input
from .case_panel import generate_transcript_html

async def log_modmail_action(guild, title, fields):
    cid = bot.data_manager.config.get("modmail_action_log_channel")
    if not cid: return
    channel = guild.get_channel(cid)
    if not channel: return

    embed = make_embed(title, "> A staff action was performed on a modmail ticket.", kind="support", scope=SCOPE_SUPPORT, guild=guild)
    for n, v in fields:
        embed.add_field(name=n, value=v, inline=True)
    try: await channel.send(embed=embed)
    except Exception: pass


def apply_modmail_ticket_state(embed: discord.Embed, ticket: dict, guild: discord.Guild) -> discord.Embed:
    status = str(ticket.get("status", "open")).title()
    priority = str(ticket.get("priority", "normal")).title()
    tags = ", ".join(f"`{tag}`" for tag in ticket.get("tags", [])) or "None"
    assigned = ticket.get("assigned_moderator")
    assignee = f"<@{assigned}>" if assigned else "Unclaimed"
    last_user = iso_to_dt(ticket.get("last_user_message_at"))
    last_staff = iso_to_dt(ticket.get("last_staff_message_at"))

    embed.color = EMBED_PALETTE["danger"] if ticket.get("status") == "closed" else (EMBED_PALETTE["warning"] if ticket.get("priority") in {"high", "urgent"} else EMBED_PALETTE["support"])
    upsert_embed_field(embed, "Status", status, inline=True)
    upsert_embed_field(embed, "Urgency", priority, inline=True)
    upsert_embed_field(embed, "Assigned To", assignee, inline=True)
    upsert_embed_field(
        embed,
        "Activity",
        join_lines([
            f"User: {discord.utils.format_dt(last_user, 'R') if last_user else 'Unknown'}",
            f"Staff: {discord.utils.format_dt(last_staff, 'R') if last_staff else 'No reply yet'}",
        ]),
        inline=True,
    )
    upsert_embed_field(embed, "Tags", tags, inline=True)
    brand_embed(embed, guild=guild, scope=SCOPE_SUPPORT)
    return embed


async def refresh_modmail_message(
    message: Optional[discord.Message],
    guild: Optional[discord.Guild],
    user_id: str,
    view: "ModmailControlView",
) -> bool:
    ticket = bot.data_manager.modmail.get(user_id)
    if not ticket or message is None or not message.embeds or guild is None:
        return False
    view.sync_buttons(ticket)
    embed = apply_modmail_ticket_state(message.embeds[0], ticket, guild)
    try:
        await message.edit(embed=embed, view=view)
        return True
    except discord.NotFound:
        logger.warning("Modmail panel message for user %s no longer exists.", user_id)
    except discord.Forbidden:
        logger.warning("Missing permission to refresh modmail panel message for user %s.", user_id)
    except discord.HTTPException as exc:
        logger.warning("Failed to refresh modmail panel message for user %s: %s", user_id, exc)
    return False


async def refresh_modmail_ticket_log(guild: discord.Guild, user_id: str):
    ticket = bot.data_manager.modmail.get(user_id)
    if not ticket:
        return
    log_channel_id = bot.data_manager.config.get("modmail_inbox_channel")
    log_id = ticket.get("log_id")
    if not log_channel_id or not log_id:
        return
    channel = guild.get_channel(log_channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(log_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    view = ModmailControlView(user_id)
    view.message = message
    await refresh_modmail_message(message, guild, user_id, view)


async def export_modmail_transcript(thread: discord.Thread, user_id: str) -> discord.File:
    messages = []
    async for message in thread.history(limit=None, oldest_first=True):
        messages.append({
            "author_name": message.author.display_name,
            "author_avatar_url": message.author.display_avatar.url,
            "created_at": message.created_at,
            "content": message.content,
            "attachments": [{"filename": attachment.filename, "url": attachment.url} for attachment in message.attachments],
            "channel_id": thread.id,
            "deleted": False,
            "edited": bool(message.edited_at),
        })
    transcript_user = SimpleNamespace(display_name=f"Ticket {user_id}", id=int(user_id))
    html_content = generate_transcript_html(messages, transcript_user)
    return discord.File(io.BytesIO(html_content.encode("utf-8")), filename=f"modmail_transcript_{user_id}.html")


def _parse_user_id(value: Union[str, int, None]) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def resolve_modmail_user(user_id: Union[str, int, None]) -> Optional[discord.User]:
    normalized_user_id = _parse_user_id(user_id)
    if normalized_user_id is None:
        return None
    cached = bot.get_user(normalized_user_id)
    if cached is not None:
        return cached
    try:
        return await bot.fetch_user(normalized_user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def resolve_modmail_thread(guild: Optional[discord.Guild], ticket: Optional[dict]) -> Optional[discord.Thread]:
    if guild is None or not isinstance(ticket, dict):
        return None

    thread_id = _parse_user_id(ticket.get("thread_id"))
    if thread_id is None:
        return None

    candidate = guild.get_thread(thread_id) or guild.get_channel_or_thread(thread_id)
    if isinstance(candidate, discord.Thread):
        return candidate

    try:
        fetched = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.Thread) else None

class ModmailPrioritySelect(discord.ui.Select):
    def __init__(self, panel: "ModmailControlView"):
        self.panel = panel
        ticket = bot.data_manager.modmail.get(panel.user_id, {})
        current = str(ticket.get("priority", "normal")).lower()
        options = [
            discord.SelectOption(label=priority.title(), value=priority, default=priority == current)
            for priority in DEFAULT_TICKET_PRIORITIES
        ]
        super().__init__(placeholder="Choose how urgent this ticket is...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return
        ticket = bot.data_manager.modmail.get(self.panel.user_id)
        if not ticket:
            await respond_with_error(interaction, "Ticket data not found.", scope=SCOPE_SUPPORT)
            return
        ticket["priority"] = self.values[0]
        await bot.data_manager.save_modmail()
        await refresh_modmail_message(self.panel.message or interaction.message, interaction.guild, self.panel.user_id, self.panel)
        await log_modmail_action(interaction.guild, "Ticket Priority Updated", [
            ("User", f"<@{self.panel.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Priority", self.values[0].title()),
        ])
        await interaction.response.edit_message(
            embed=make_confirmation_embed(
                "Ticket Priority Updated",
                f"> Priority set to **{self.values[0].title()}**.",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            ),
            view=None,
        )


class ModmailPriorityView(discord.ui.View):
    def __init__(self, panel: "ModmailControlView"):
        super().__init__(timeout=120)
        self.add_item(ModmailPrioritySelect(panel))


class ModmailTagsModal(discord.ui.Modal, title="Update Ticket Tags"):
    tags = discord.ui.TextInput(
        label="Tags",
        placeholder="bug, urgent, follow-up",
        max_length=200,
        required=False,
    )

    def __init__(self, panel: "ModmailControlView"):
        super().__init__()
        self.panel = panel
        ticket = bot.data_manager.modmail.get(panel.user_id, {})
        self.tags.default = ", ".join(ticket.get("tags", []))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return
        ticket = bot.data_manager.modmail.get(self.panel.user_id)
        if not ticket:
            await respond_with_error(interaction, "Ticket data not found.", scope=SCOPE_SUPPORT)
            return
        ticket["tags"] = sanitize_tags(_split_case_input(self.tags.value), limit=10)
        await bot.data_manager.save_modmail()
        await refresh_modmail_message(self.panel.message, interaction.guild, self.panel.user_id, self.panel)
        await log_modmail_action(interaction.guild, "Ticket Tags Updated", [
            ("User", f"<@{self.panel.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Tags", ", ".join(ticket["tags"]) or "None"),
        ])
        await interaction.response.send_message(
            embed=make_confirmation_embed("Ticket Tags Updated", "> Ticket tags were updated.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            ephemeral=True,
        )


class CannedReplySelect(discord.ui.Select):
    def __init__(self, panel: "ModmailControlView"):
        self.panel = panel
        replies = bot.data_manager.config.get("modmail_canned_replies", {})
        options = [
            discord.SelectOption(label=key, value=key, description=truncate_text(value, 100))
            for key, value in list(replies.items())[:25]
        ]
        if not options:
            options.append(discord.SelectOption(label="No saved replies", value="__empty__", description="Add reply templates in /config"))
        super().__init__(placeholder="Choose a quick reply...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return
        if self.values[0] == "__empty__":
            await respond_with_error(interaction, "No saved replies have been set up yet.", scope=SCOPE_SUPPORT)
            return
        ticket = bot.data_manager.modmail.get(self.panel.user_id)
        if not ticket:
            await respond_with_error(interaction, "Ticket data not found.", scope=SCOPE_SUPPORT)
            return
        reply_key = self.values[0]
        reply_body = bot.data_manager.config.get("modmail_canned_replies", {}).get(reply_key, "")
        user = await resolve_modmail_user(self.panel.user_id)
        if user is None:
            await respond_with_error(interaction, "Unable to resolve the user for this ticket.", scope=SCOPE_SUPPORT)
            return
        try:
            embed = make_embed(
                "Staff Reply",
                truncate_text(reply_body, 4096),
                kind="info",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            )
            await user.send(embed=embed)
        except discord.Forbidden:
            await respond_with_error(interaction, "Unable to DM the user with the saved reply.", scope=SCOPE_SUPPORT)
            return
        except discord.HTTPException as exc:
            await respond_with_error(interaction, f"Failed to send the saved reply: {exc}", scope=SCOPE_SUPPORT)
            return

        ticket["last_staff_message_at"] = now_iso()
        await bot.data_manager.save_modmail()
        if isinstance(interaction.channel, discord.Thread):
            await interaction.channel.send(f"Sent quick reply `{reply_key}` to <@{self.panel.user_id}>.")
        await refresh_modmail_message(self.panel.message or interaction.message, interaction.guild, self.panel.user_id, self.panel)
        await log_modmail_action(interaction.guild, "Canned Reply Sent", [
            ("User", f"<@{self.panel.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Template", reply_key),
        ])
        await interaction.response.edit_message(
            embed=make_confirmation_embed("Quick Reply Sent", "> The saved reply was sent to the user.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            view=None,
        )


class CannedReplyView(discord.ui.View):
    def __init__(self, panel: "ModmailControlView"):
        super().__init__(timeout=120)
        self.add_item(CannedReplySelect(panel))


class ModmailControlView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.message: Optional[discord.Message] = None
        self.sync_buttons(bot.data_manager.modmail.get(self.user_id, {}))

    def sync_buttons(self, ticket: dict) -> None:
        status = ticket.get("status", "open")
        assigned = ticket.get("assigned_moderator")
        self.close_ticket.disabled = status == "closed"
        self.open_ticket.disabled = status != "closed"
        self.claim_ticket.label = "Unclaim Ticket" if assigned else "Claim Ticket"
        self.claim_ticket.style = discord.ButtonStyle.secondary if assigned else discord.ButtonStyle.success

    def _get_ticket(self) -> Optional[dict]:
        return bot.data_manager.modmail.get(self.user_id)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="mm_close", row=0)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        self.message = interaction.message
        ticket = self._get_ticket()
        if not ticket or ticket.get("status") == "closed":
            await interaction.response.send_message(embed=make_embed("Already Closed", "> Ticket is already closed.", kind="info", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        ticket["status"] = "closed"
        ticket["last_staff_message_at"] = now_iso()
        await bot.data_manager.save_modmail()

        thread = await resolve_modmail_thread(interaction.guild, ticket)

        transcript_file = None
        if isinstance(thread, discord.Thread):
            try:
                transcript_file = await export_modmail_transcript(thread, self.user_id)
            except Exception as exc:
                logger.warning("Failed to export modmail transcript for %s: %s", self.user_id, exc)

        await refresh_modmail_message(interaction.message, interaction.guild, self.user_id, self)

        if isinstance(thread, discord.Thread):
            try:
                await thread.send(f"**Ticket Closed** by {interaction.user.mention}.")
                await thread.edit(locked=True, archived=True)
            except discord.HTTPException as exc:
                logger.warning("Failed to finalize closed thread for %s: %s", self.user_id, exc)

        user = await resolve_modmail_user(self.user_id)
        if user is not None:
            dm_embed = make_embed(
                "Ticket Closed",
                "> Your support ticket has been closed by the staff team.\n> If you need anything else, open a new ticket anytime.",
                kind="danger",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            )
            try:
                await user.send(embed=dm_embed)
            except discord.HTTPException as exc:
                logger.warning("Failed to DM closed-ticket notice to %s: %s", self.user_id, exc)

        log_channel_id = bot.data_manager.config.get("modmail_action_log_channel")
        log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None
        if transcript_file and log_channel:
            try:
                await log_channel.send(content=f"Transcript for closed ticket <@{self.user_id}>", file=transcript_file)
            except discord.HTTPException as exc:
                logger.warning("Failed to upload modmail transcript for %s: %s", self.user_id, exc)

        await log_modmail_action(interaction.guild, "Ticket Closed", [
            ("User", f"<@{self.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Priority", str(ticket.get("priority", "normal")).title()),
            ("Ticket ID", str(ticket.get("thread_id", "N/A"))),
        ])
        await interaction.followup.send(
            embed=make_confirmation_embed("Ticket Closed", "> Ticket closed and transcript exported when available.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.success, custom_id="mm_open", disabled=True, row=0)
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        self.message = interaction.message
        ticket = self._get_ticket()
        if not ticket:
            await interaction.response.send_message(embed=make_embed("Ticket Not Found", "> Ticket data not found.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        ticket["status"] = "open"
        ticket["last_staff_message_at"] = now_iso()
        await bot.data_manager.save_modmail()
        await refresh_modmail_message(interaction.message, interaction.guild, self.user_id, self)

        thread = await resolve_modmail_thread(interaction.guild, ticket)

        if isinstance(thread, discord.Thread):
            try:
                await thread.edit(locked=False, archived=False)
                await thread.send(f"**Ticket Re-opened** by {interaction.user.mention}.")
            except discord.HTTPException as exc:
                logger.warning("Failed to reopen thread for %s: %s", self.user_id, exc)

        user = await resolve_modmail_user(self.user_id)
        if user is not None:
            dm_embed = make_embed(
                "Ticket Re-opened",
                "> Your support ticket has been re-opened. You can continue messaging the staff team.",
                kind="success",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            )
            try:
                await user.send(embed=dm_embed)
            except discord.HTTPException as exc:
                logger.warning("Failed to DM reopened-ticket notice to %s: %s", self.user_id, exc)

        await log_modmail_action(interaction.guild, "Ticket Re-opened", [
            ("User", f"<@{self.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Ticket ID", str(ticket.get("thread_id", "N/A"))),
        ])
        await interaction.followup.send(
            embed=make_confirmation_embed("Ticket Re-opened", "> Ticket reopened successfully.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, custom_id="mm_claim", row=0)
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        self.message = interaction.message
        ticket = self._get_ticket()
        if not ticket:
            await interaction.response.send_message(embed=make_embed("Ticket Not Found", "> Ticket data not found.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        current = ticket.get("assigned_moderator")
        ticket["assigned_moderator"] = None if current == interaction.user.id else interaction.user.id
        ticket["claimed_at"] = now_iso() if ticket.get("assigned_moderator") else None
        await bot.data_manager.save_modmail()
        await refresh_modmail_message(interaction.message, interaction.guild, self.user_id, self)
        await log_modmail_action(interaction.guild, "Ticket Assignment Updated", [
            ("User", f"<@{self.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Assigned", interaction.user.mention if ticket.get("assigned_moderator") else "Unclaimed"),
        ])
        await interaction.followup.send(embed=make_embed("Assignment Updated", "> Ticket assignment has been updated.", kind="success", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)

    @discord.ui.button(label="Urgency", style=discord.ButtonStyle.primary, custom_id="mm_priority", row=1)
    async def priority(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return
        self.message = interaction.message
        await interaction.response.send_message(
            embed=make_embed("Ticket Urgency", "> Choose how urgent this ticket is for staff.", kind="warning", scope=SCOPE_SUPPORT, guild=interaction.guild),
            view=ModmailPriorityView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="Tags", style=discord.ButtonStyle.primary, custom_id="mm_tags", row=1)
    async def tags(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return
        self.message = interaction.message
        await interaction.response.send_modal(ModmailTagsModal(self))

    @discord.ui.button(label="Quick Reply", style=discord.ButtonStyle.secondary, custom_id="mm_canned", row=1)
    async def canned_reply(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return
        self.message = interaction.message
        await interaction.response.send_message(embed=build_canned_replies_embed(interaction.guild), view=CannedReplyView(self), ephemeral=True)

    @discord.ui.button(label="Download Transcript", style=discord.ButtonStyle.secondary, custom_id="mm_export", row=1)
    async def export_transcript(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return
        ticket = self._get_ticket()
        thread = await resolve_modmail_thread(interaction.guild, ticket)
        if not isinstance(thread, discord.Thread):
            await respond_with_error(interaction, "Transcript export is only available from the ticket thread.", scope=SCOPE_SUPPORT)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        file = await export_modmail_transcript(thread, self.user_id)
        await interaction.followup.send(
            embed=make_confirmation_embed("Transcript Ready", "> The ticket transcript has been generated.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            file=file,
            ephemeral=True,
        )

class ModmailModal(discord.ui.Modal):
    def __init__(self, category: str):
        super().__init__(title=f"Open {category} Ticket")
        self.category = category
        
        if category == "Report":
            self.add_item(discord.ui.TextInput(label="Reported User (ID or Name)", placeholder="e.g. 123456789...", required=True))
            self.add_item(discord.ui.TextInput(label="Reason", placeholder="Short summary...", required=True))
            self.add_item(discord.ui.TextInput(label="Evidence / Details", style=discord.TextStyle.paragraph, placeholder="Please provide links or detailed explanation...", required=True))
        elif category == "Partnership":
            self.add_item(discord.ui.TextInput(label="Server Name", required=True))
            self.add_item(discord.ui.TextInput(label="Server Link (Permanent)", required=True))
            self.add_item(discord.ui.TextInput(label="Subject", style=discord.TextStyle.paragraph, required=True))
        else:
            # Support
            self.add_item(discord.ui.TextInput(label="Subject", placeholder="Brief title...", required=True))
            self.add_item(discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, placeholder="How can we help?", required=True))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = get_context_guild(interaction)
        if guild is None:
            await interaction.followup.send(embed=make_embed("Server Not Found", "> This server could not be resolved for modmail. Ask an administrator to set the Guild ID in setup.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        existing_ticket = bot.data_manager.modmail.get(str(interaction.user.id))
        if existing_ticket and existing_ticket.get("status") == "open":
            await interaction.followup.send(embed=make_embed("Ticket Already Open", "> You already have an open ticket. Keep replying in DM and staff will receive it.", kind="info", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        log_channel_id = bot.data_manager.config.get("modmail_inbox_channel")
        if not log_channel_id:
            await interaction.followup.send(embed=make_embed("Configuration Error", "> Modmail system is not fully configured (Inbox channel missing). Contact admin.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        log_channel = guild.get_channel(log_channel_id)
        if not log_channel:
            await interaction.followup.send(embed=make_embed("Channel Not Found", "> Inbox channel not found.", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)
            return

        # Create Log Embed
        embed = make_embed(
            f"New Ticket: {self.category}",
            "> A new ticket has been submitted through the support panel.",
            kind="support",
            scope=SCOPE_SUPPORT,
            guild=guild,
            thumbnail=interaction.user.display_avatar.url,
            author_name=f"{interaction.user.display_name} ({interaction.user.id})",
            author_icon=interaction.user.display_avatar.url,
        )
        
        fields_data = []
        for child in self.children:
            field_label = get_modal_item_label(child)
            embed.add_field(name=field_label, value=f">>> {child.value}", inline=False)
            fields_data.append(f"**{field_label}**: {child.value}")

        ticket_payload = {
            "status": "open",
            "category": self.category,
            "created_at": now_iso(),
            "priority": "normal",
            "tags": [],
            "assigned_moderator": None,
            "claimed_at": None,
            "last_user_message_at": now_iso(),
            "last_staff_message_at": None,
            "last_sla_alert_at": None,
        }
        normalize_modmail_ticket(ticket_payload)
        apply_modmail_ticket_state(embed, ticket_payload, guild)
        
        # Send Log & Create Thread
        try:
            view = ModmailControlView(str(interaction.user.id))
            
            ping_roles = bot.data_manager.config.get("modmail_ping_roles", [])
            if ping_roles:
                pings = " ".join([f"<@&{rid}>" for rid in ping_roles])
            else:
                r_mod = bot.data_manager.config.get("role_mod", DEFAULT_ROLE_MOD)
                r_admin = bot.data_manager.config.get("role_admin", DEFAULT_ROLE_ADMIN)
                r_cm = bot.data_manager.config.get("role_community_manager", DEFAULT_ROLE_COMMUNITY_MANAGER)
                pings = f"<@&{r_mod}> <@&{r_admin}> <@&{r_cm}>"

            log_msg = await log_channel.send(content=f"New Ticket from {interaction.user.mention} {pings}", embed=embed, view=view)
            thread = await log_msg.create_thread(name=f"ticket-{interaction.user.name}")
            
            # Create Staff Discussion Thread
            if bot.data_manager.config.get("modmail_discussion_threads", True):
                disc_msg = await log_channel.send(f"**Staff Discussion** for {interaction.user.mention} (Ticket #{log_msg.id})")
                await disc_msg.create_thread(name=f"discuss-{interaction.user.name}")
            
            # Save Ticket Data
            ticket_payload["thread_id"] = thread.id
            ticket_payload["log_id"] = log_msg.id
            bot.data_manager.modmail[str(interaction.user.id)] = ticket_payload
            await bot.data_manager.save_modmail()
            
            # Initial Thread Msg
            await send_modmail_thread_intro(thread, interaction.user, self.category, fields_data)
            
            # DM User
            dm_embed = make_embed(
                "Ticket Created",
                f"> Your **{self.category}** ticket has been opened.\n> A staff member will be with you shortly.\n> Reply to this DM to send further details.",
                kind="support",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            )
            await interaction.user.send(embed=dm_embed)
            
            # Log Action
            await log_modmail_action(guild, "Ticket Created", [
                ("User", interaction.user.mention),
                ("Category", self.category),
                ("Ticket ID", str(thread.id))
            ])
            
            await interaction.followup.send(embed=make_embed("Ticket Created", "> Ticket created successfully! Check your DMs.", kind="success", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)

        except Exception as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed to create ticket: {e}", kind="error", scope=SCOPE_SUPPORT, guild=interaction.guild), ephemeral=True)

class ModmailPanelSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=label, description=truncate_text(description, 100))
            for label, description in MODMAIL_PANEL_CATEGORIES
        ]
        super().__init__(
            placeholder="Choose the ticket type you want to open...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="mm_ticket_type_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ModmailModal(self.values[0]))


class ModmailPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ModmailPanelSelect())



class ModmailCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(ModmailCog(bot))
