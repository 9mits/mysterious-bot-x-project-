"""MGXBot class, background tasks, extension loading, and bot lifecycle."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from discord.ext import commands, tasks

logger = logging.getLogger("MGXBot")

from core.constants import DEFAULT_GUILD_ID, SCOPE_ROLES, SCOPE_SUPPORT, TEST_GUILD_ID
from core.context import set_bot
from core.data import DataManager, resolve_bot_token
from core.services import get_feature_flag, ticket_needs_sla_alert
from core.utils import iso_to_dt, now_iso


EXTENSIONS = (
    "cogs.cases",
    "cogs.history",
    "cogs.case_panel",
    "cogs.moderation",
    "cogs.roles",
    "cogs.derole",
    "cogs.modmail",
    "cogs.automod",
    "cogs.config",
    "cogs.analytics",
    "cogs.admin",
    "cogs.events",
    "cogs.event_leaderboard",
)

DISABLED_APPLICATION_COMMANDS = frozenset({
    "ban",
    "kick",
    "modmail",
    "onboarding",
    "feature",
    "publicpunish",
    "role-create",
    "rolecreate",
    "create-role",
    "listcommands",
    "rolehelp",
    "rolesettings",
    "undopunish",
    "unlockdown",
    "antinuke",
    "help",
})


def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = True
    intents.message_content = True
    intents.voice_states = True  # required for the VC event leaderboard
    if hasattr(intents, "auto_moderation_configuration"):
        intents.auto_moderation_configuration = True
    if hasattr(intents, "auto_moderation_execution"):
        intents.auto_moderation_execution = True
    return intents


def command_payloads(tree: discord.app_commands.CommandTree) -> List[dict]:
    """Serialise the current global command tree to plain dicts for hashing."""
    payloads = []
    for command in tree.get_commands():
        try:
            payloads.append(command.to_dict(tree))
        except Exception:
            # Fall back to a coarse identity if a command can't be serialised, so
            # a change there still nudges the fingerprint rather than crashing.
            payloads.append({"name": getattr(command, "qualified_name", repr(command))})
    return payloads


def fingerprint_payloads(payloads: List[dict]) -> str:
    """Order-independent SHA-256 of serialised commands; same set → same hash."""
    encoded = sorted(json.dumps(p, sort_keys=True, default=str) for p in payloads)
    return hashlib.sha256("\n".join(encoded).encode("utf-8")).hexdigest()


class MGXBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session: Optional[aiohttp.ClientSession] = None
        self.data_manager: Optional[DataManager] = None
        self.start_time = time.time()
        self.active_executions = {}
        self.dm_modmail_prompt_cooldowns: Dict[int, float] = {}
        self.native_automod_event_cache: Dict[Tuple[int, int, int, str, str], float] = {}
        self.abuse_system = None

    async def setup_hook(self) -> None:
        from core.data import AntiAbuseSystem

        self.session = aiohttp.ClientSession()
        self.data_manager = DataManager(self)
        self.abuse_system = AntiAbuseSystem()
        await self.data_manager.load_all()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        if os.environ.get("TEST_MODE"):
            await self.load_extension("cogs.testkit")
            logger.info("TEST_MODE active — testkit cog loaded")

        self._remove_disabled_application_commands()
        await self._auto_sync_commands()
        await self._restore_persistent_views()

        self.check_tempbans.start()
        self.background_save_task.start()
        self.status_task.start()
        self.modmail_sla_task.start()
        self.role_cleanup_task.start()

    async def _restore_persistent_views(self) -> None:
        from cogs.modmail import ModmailControlView, ModmailPanelView

        self.add_view(ModmailPanelView())
        if not self.data_manager:
            return

        for uid, data in self.data_manager.modmail.items():
            if data.get("status") == "open":
                log_id = data.get("log_id")
                if log_id:
                    self.add_view(ModmailControlView(uid), message_id=log_id)

    def _remove_disabled_application_commands(self) -> None:
        for command_name in DISABLED_APPLICATION_COMMANDS:
            for command_type in (
                discord.AppCommandType.chat_input,
                discord.AppCommandType.user,
                discord.AppCommandType.message,
            ):
                self.tree.remove_command(command_name, type=command_type)

    async def _auto_sync_commands(self) -> None:
        """Sync slash commands to this instance's guild on startup.

        Targets ``TEST_GUILD_ID`` under TEST_MODE (so staging registers privately)
        and the configured guild in production — each single-guild instance keeps
        its own guild current on deploy, with no manual ``!sync``. A fingerprint of
        the command set is stored in config so unchanged restarts skip the API call
        and don't burn Discord's command-sync rate limit. ``!sync`` stays as a
        manual override. A sync failure here must never block startup.
        """
        if not self.data_manager:
            return

        if os.environ.get("TEST_MODE"):
            target_id = TEST_GUILD_ID
        else:
            target_id = self.data_manager.config.get("guild_id", DEFAULT_GUILD_ID)
        if not target_id:
            return

        fingerprint = fingerprint_payloads(command_payloads(self.tree))
        state_key = f"synced_command_fingerprint_{target_id}"
        if self.data_manager.config.get(state_key) == fingerprint:
            logger.info("Slash commands unchanged for guild %s — skipping sync", target_id)
            return

        guild = discord.Object(id=int(target_id))
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            logger.warning("Auto-sync to guild %s failed: %s", target_id, exc)
            return

        self.data_manager.config[state_key] = fingerprint
        self.data_manager.mark_config_dirty()
        await self.data_manager.save_all()
        logger.info("Auto-synced %d slash commands to guild %s", len(synced), target_id)

    async def close(self) -> None:
        for task_loop in (
            self.check_tempbans,
            self.background_save_task,
            self.status_task,
            self.modmail_sla_task,
            self.role_cleanup_task,
        ):
            task_loop.cancel()

        if self.data_manager:
            await self.data_manager.save_all(force=True)
        if self.session:
            await self.session.close()
        await super().close()

    @tasks.loop(minutes=1)
    async def check_tempbans(self) -> None:
        now = discord.utils.utcnow()
        changed = False
        if not self.data_manager:
            return

        for uid, records in self.data_manager.punishments.items():
            for record in records:
                if record.get("type") == "ban" and record.get("active", False):
                    minutes = record.get("duration_minutes", 0)
                    if minutes > 0:
                        issued_at = iso_to_dt(record.get("timestamp"))
                        if issued_at and now >= issued_at + timedelta(minutes=minutes):
                            guild = self.get_guild(self.data_manager.config.get("guild_id", DEFAULT_GUILD_ID))
                            if guild:
                                try:
                                    await guild.unban(discord.Object(id=int(uid)), reason="Tempban Expired")
                                except Exception:
                                    pass
                            record["active"] = False
                            changed = True

        if changed:
            await self.data_manager.save_punishments()

    @tasks.loop(minutes=2)
    async def background_save_task(self) -> None:
        if self.data_manager:
            await self.data_manager.save_all()

    @tasks.loop(minutes=30)
    async def status_task(self) -> None:
        # "Listening to DMs for support" — reads cleanly and reflects modmail
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="DMs for support")
        )

    @tasks.loop(minutes=10)
    async def modmail_sla_task(self) -> None:
        from cogs.shared import make_embed

        if not self.data_manager or not get_feature_flag(self.data_manager.config, "advanced_modmail", True):
            return

        guild = self.get_guild(self.data_manager.config.get("guild_id", DEFAULT_GUILD_ID))
        if not guild:
            return

        now = discord.utils.utcnow()
        sla_minutes = max(5, int(self.data_manager.config.get("modmail_sla_minutes", 60)))
        changed = False

        for ticket in self.data_manager.modmail.values():
            if not isinstance(ticket, dict):
                continue
            if not ticket_needs_sla_alert(ticket, now, sla_minutes):
                continue

            thread_id = ticket.get("thread_id")
            thread = guild.get_thread(thread_id) if thread_id else None
            if not thread and thread_id:
                try:
                    thread = await self.fetch_channel(thread_id)
                except Exception:
                    thread = None

            assigned = ticket.get("assigned_moderator")
            assigned_text = f"<@{assigned}>" if assigned else "Unassigned"
            embed = make_embed(
                "Reply Reminder",
                f"> This ticket has not received a staff reply in over **{sla_minutes} minute{'s' if sla_minutes != 1 else ''}**.",
                kind="warning",
                scope=SCOPE_SUPPORT,
            )
            embed.add_field(name="Assigned To", value=assigned_text, inline=True)
            embed.add_field(name="SLA Threshold", value=f"{sla_minutes} min", inline=True)
            if thread:
                try:
                    await thread.send(embed=embed)
                except Exception:
                    pass

            ticket["last_sla_alert_at"] = now_iso()
            changed = True

        if changed:
            await self.data_manager.save_modmail()

    @tasks.loop(hours=6)
    async def role_cleanup_task(self) -> None:
        from cogs.shared import send_log
        from cogs.shared import get_custom_role_limit
        from cogs.shared import format_reason_value, make_embed

        if not self.data_manager or not get_feature_flag(self.data_manager.config, "role_cleanup", True):
            return

        guild = self.get_guild(self.data_manager.config.get("guild_id", DEFAULT_GUILD_ID))
        if not guild:
            return

        removed_any = False
        for user_id, records in list(self.data_manager.roles.items()):
            # Records are stored as a list of role dicts per user
            records = records if isinstance(records, list) else [records]

            member = guild.get_member(int(user_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None

            # Still eligible — leave all their roles intact
            if member and get_custom_role_limit(member) > 0:
                continue

            # No longer eligible — remove every custom role this user owns
            for record in records:
                if not isinstance(record, dict):
                    continue
                role_id = record.get("role_id")
                role = guild.get_role(role_id) if role_id else None
                if role:
                    try:
                        await role.delete(reason="Custom role eligibility cleanup")
                    except Exception:
                        pass

            self.data_manager.roles.pop(user_id, None)
            removed_any = True

            embed = make_embed(
                "Custom Role Cleanup",
                "> A custom role was removed because the owner no longer meets the eligibility requirements.",
                kind="warning",
                scope=SCOPE_ROLES,
                guild=guild,
            )
            embed.add_field(name="Target", value=f"<@{user_id}> (`{user_id}`)", inline=True)
            embed.add_field(
                name="Reason",
                value=format_reason_value("Lost booster or approved-role eligibility", limit=300),
                inline=False,
            )
            await send_log(guild, embed)

        if removed_any:
            await self.data_manager.save_roles()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id)
        # BisectHosting panel watches for this exact phrase to flip the server
        # state from "starting" to "running".
        print("successfully finished startup", flush=True)

    @status_task.before_loop
    async def before_status_task(self) -> None:
        await self.wait_until_ready()

    @modmail_sla_task.before_loop
    async def before_modmail_sla_task(self) -> None:
        await self.wait_until_ready()

    @role_cleanup_task.before_loop
    async def before_role_cleanup_task(self) -> None:
        await self.wait_until_ready()


def create_bot() -> MGXBot:
    bot = MGXBot(command_prefix="!", intents=_build_intents())
    set_bot(bot)
    return bot


def run() -> None:
    bot = create_bot()
    bot.run(resolve_bot_token())
