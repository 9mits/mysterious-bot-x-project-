from __future__ import annotations

import time
from datetime import timedelta
from typing import Dict, Optional, Tuple

import aiohttp
import discord
from discord.ext import commands, tasks

from core.constants import DEFAULT_GUILD_ID, SCOPE_ROLES, SCOPE_SUPPORT
from core.context import set_bot
from core.data import DataManager, resolve_bot_token
from core.services import get_feature_flag, ticket_needs_sla_alert
from core.utils import iso_to_dt, now_iso


EXTENSIONS = (
    "cogs.cases",
    "cogs.moderation",
    "cogs.roles",
    "cogs.derole",
    "cogs.modmail",
    "cogs.automod",
    "cogs.config",
    "cogs.analytics",
    "cogs.system",
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
    if hasattr(intents, "auto_moderation_configuration"):
        intents.auto_moderation_configuration = True
    if hasattr(intents, "auto_moderation_execution"):
        intents.auto_moderation_execution = True
    return intents


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

    async def setup_hook(self):
        from core.data import AntiAbuseSystem

        self.session = aiohttp.ClientSession()
        self.data_manager = DataManager(self)
        self.abuse_system = AntiAbuseSystem()
        await self.data_manager.load_all()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        self._remove_disabled_application_commands()
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

    async def close(self):
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
    async def check_tempbans(self):
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
    async def background_save_task(self):
        if self.data_manager:
            await self.data_manager.save_all()

    @tasks.loop(minutes=30)
    async def status_task(self):
        await self.change_presence(activity=discord.Game(name="DM for modmail"))

    @tasks.loop(minutes=10)
    async def modmail_sla_task(self):
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
    async def role_cleanup_task(self):
        from cogs.shared import send_log
        from cogs.shared import get_custom_role_limit
        from cogs.shared import format_reason_value, make_embed

        if not self.data_manager or not get_feature_flag(self.data_manager.config, "role_cleanup", True):
            return

        guild = self.get_guild(self.data_manager.config.get("guild_id", DEFAULT_GUILD_ID))
        if not guild:
            return

        removed_any = False
        for user_id, record in list(self.data_manager.roles.items()):
            if not isinstance(record, dict):
                continue

            role_id = record.get("role_id")
            role = guild.get_role(role_id) if role_id else None
            member = guild.get_member(int(user_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None

            if member and get_custom_role_limit(member) > 0:
                continue

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

    @status_task.before_loop
    async def before_status_task(self):
        await self.wait_until_ready()

    @modmail_sla_task.before_loop
    async def before_modmail_sla_task(self):
        await self.wait_until_ready()

    @role_cleanup_task.before_loop
    async def before_role_cleanup_task(self):
        await self.wait_until_ready()


def create_bot() -> MGXBot:
    bot = MGXBot(command_prefix="!", intents=_build_intents())
    set_bot(bot)
    return bot


def run() -> None:
    bot = create_bot()
    bot.run(resolve_bot_token())
