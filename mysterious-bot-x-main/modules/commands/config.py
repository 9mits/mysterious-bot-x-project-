# modules/commands/config.py
# Server configuration views and /setup, /config commands.

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import copy
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Union, Set, Tuple, Any
from collections import Counter, deque, defaultdict
import html
import re
import io
import logging
from pathlib import Path

from modules.constants import (
    BRAND_NAME,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    DEFAULT_ROLE_OWNER,
    DEFAULT_RULES,
    DEFAULT_ANCHOR_ROLE_ID,
    EMBED_PALETTE,
    FEATURE_FLAG_LABELS,
    SCOPE_MODERATION,
    SCOPE_SUPPORT,
    SCOPE_SYSTEM,
    TOKEN_ENV_VARS,
)
from modules.models import CaseNote
from modules.services import (
    DEFAULT_CANNED_REPLIES,
    DEFAULT_ESCALATION_MATRIX,
    DEFAULT_FEATURE_FLAGS,
    DEFAULT_NATIVE_AUTOMOD_SETTINGS,
    DEFAULT_SCHEMA_VERSION,
    DEFAULT_TICKET_PRIORITIES,
    export_config_payload,
    get_feature_flag,
    get_escalation_steps,
    get_native_automod_settings,
    has_capability,
    import_config_payload,
    normalize_modmail_ticket,
    resolve_escalation_duration,
    run_schema_migrations,
    sanitize_evidence_links,
    sanitize_linked_cases,
    sanitize_tags,
    ticket_needs_sla_alert,
    validate_guild_configuration,
)
from modules.context import abuse_system, bot, tree
from modules.utils import iso_to_dt, now_iso, parse_duration_str
from .system import check_admin, check_owner
from .shared import (
    logger,
    DB_DIR,
    CONFIG_FILE,
    PUNISHMENTS_FILE,
    MODMAIL_FILE,
    truncate_text,
    format_duration,
    format_log_quote,
    format_plain_log_block,
    format_reason_value,
    make_embed,
    brand_embed,
    make_empty_state_embed,
    make_error_embed,
    make_confirmation_embed,
    join_lines,
    upsert_embed_field,
    get_user_display_name,
    format_user_ref,
    format_user_id_ref,
    get_primary_guild,
    get_context_guild,
    send_log,
    has_permission_capability,
    respond_with_error,
    is_staff_member,
    is_staff,
    resolve_member,
    get_general_log_channel_id,
    get_general_log_channel_ids,
    get_punishment_log_channel_id,
    get_punishment_log_channel_ids,
    build_setup_dashboard_embed,
    build_modmail_settings_embed,
    build_config_dashboard_embed,
    build_rules_dashboard_embed,
    build_feature_flags_embed,
    build_escalation_matrix_embed,
    build_canned_replies_embed,
    build_setup_validation_embed,
    get_feature_flag_name,
)
from .automod import (
    ensure_native_rule_override_policy,
    AutoModBridgeSettingsView,
    AutoModRuleBrowserView,
    AutoModPolicyEditorView,
    AutoModChannelSettingsView,
    AutoModImmunityView,
    SmartAutoModSettingsView,
    AutoModDashboardView,
)
from .modmail import ModmailSettingsView

class ConfigRoleSelect(discord.ui.RoleSelect):
    def __init__(self, config_key: str, config_name: str):
        super().__init__(placeholder=f"Select {config_name}...", min_values=1, max_values=1)
        self.config_key = config_key
        self.config_name = config_name

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        bot.data_manager.config[self.config_key] = role.id
        await bot.data_manager.save_config()
        await interaction.response.send_message(f"✅ **{self.config_name}** updated to {role.mention}", ephemeral=True)

class MultiConfigRoleSelect(discord.ui.RoleSelect):
    def __init__(self, config_key: str, config_name: str):
        super().__init__(placeholder=f"Select {config_name}...", min_values=1, max_values=25)
        self.config_key = config_key
        self.config_name = config_name

    async def callback(self, interaction: discord.Interaction):
        roles = self.values
        role_ids = [r.id for r in roles]
        bot.data_manager.config[self.config_key] = role_ids
        await bot.data_manager.save_config()
        mentions = ", ".join([r.mention for r in roles])
        await interaction.response.send_message(f"✅ **{self.config_name}** updated to: {mentions}", ephemeral=True)

class ConfigChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, config_key: str, config_name: str, channel_types=None):
        super().__init__(placeholder=f"Select {config_name}...", min_values=1, max_values=1, channel_types=channel_types)
        self.config_key = config_key
        self.config_name = config_name

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        channel = interaction.guild.get_channel(selected.id) or await interaction.guild.fetch_channel(selected.id)
        bot.data_manager.config[self.config_key] = channel.id
        if self.config_key == "general_log_channel_id":
            bot.data_manager.config["log_channel_id"] = channel.id
        await bot.data_manager.save_config()
        
        if self.config_key == "modmail_panel_channel":
            await interaction.response.defer(ephemeral=True)
            try:
                from .shared import send_modmail_panel_message
                await send_modmail_panel_message(channel, interaction.guild)
                await interaction.followup.send(f"✅ **{self.config_name}** updated to {channel.mention} and panel sent.", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"✅ **{self.config_name}** updated to {channel.mention}, but failed to send panel: {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ **{self.config_name}** updated to {channel.mention}", ephemeral=True)

class ConfigTypeSelect(discord.ui.Select):
    def __init__(self, category: str, *, row: Optional[int] = None):
        self.category = category
        options = []
        if category == "roles":
            options = [
                discord.SelectOption(label="Owner Role", value="role_owner", description="Main owner-level bot access role."),
                discord.SelectOption(label="Admin Role", value="role_admin", description="Admin access for bot systems."),
                discord.SelectOption(label="Mod Role", value="role_mod", description="Moderator access role."),
                discord.SelectOption(label="Community Manager", value="role_community_manager", description="Community manager access role."),
                discord.SelectOption(label="Anchor Role", value="role_anchor", description="Placement anchor for custom roles."),
                discord.SelectOption(label="Modmail Ping Roles", value="modmail_ping_roles", description="Roles pinged when a new ticket opens."),
            ]
        elif category == "channels":
            options = [
                discord.SelectOption(label="General Bot Log Channel", value="general_log_channel_id", description="Fallback log channel for general actions."),
                discord.SelectOption(label="Punishment Log Channel", value="punishment_log_channel_id", description="Primary punishment history log channel."),
                discord.SelectOption(label="Appeal Log Channel", value="appeal_channel_id", description="Where punishment appeals should go."),
                discord.SelectOption(label="AutoMod Log Channel", value="automod_log_channel_id", description="Where AutoMod bridge events should be logged."),
                discord.SelectOption(label="AutoMod Report Channel", value="automod_report_channel_id", description="Where user AutoMod reports should be sent."),
                discord.SelectOption(label="Archive Category", value="category_archive", description="Category for archive or storage channels."),
                discord.SelectOption(label="Modmail Inbox", value="modmail_inbox_channel", description="Channel where ticket threads are created."),
                discord.SelectOption(label="Modmail Logs", value="modmail_action_log_channel", description="Channel for modmail action updates."),
                discord.SelectOption(label="Modmail Panel Location", value="modmail_panel_channel", description="Where the public modmail panel is posted."),
            ]
        super().__init__(
            placeholder=f"Select {category[:-1]} to configure...",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        name = next(o.label for o in self.options if o.value == key)
        
        view = discord.ui.View()
        if self.category == "roles":
            if key == "modmail_ping_roles":
                view.add_item(MultiConfigRoleSelect(key, name))
            else:
                view.add_item(ConfigRoleSelect(key, name))
        elif self.category == "channels":
            c_types = [discord.ChannelType.text]
            if "category" in key:
                c_types = [discord.ChannelType.category]
            view.add_item(ConfigChannelSelect(key, name, channel_types=c_types))
            
        await interaction.response.send_message(f"Select the new **{name}** below:", view=view, ephemeral=True)

class ModmailDiscussionThreadSelect(discord.ui.Select):
    def __init__(self):
        enabled = bot.data_manager.config.get("modmail_discussion_threads", True)
        options = [
            discord.SelectOption(
                label="Discussion Threads On",
                value="on",
                description="Create a separate internal discussion thread for each ticket.",
                default=enabled,
            ),
            discord.SelectOption(
                label="Discussion Threads Off",
                value="off",
                description="Keep only the main ticket thread without the extra staff discussion thread.",
                default=not enabled,
            ),
        ]
        super().__init__(
            placeholder="Choose the ticket discussion thread behavior...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        bot.data_manager.config["modmail_discussion_threads"] = self.values[0] == "on"
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_modmail_settings_embed(interaction.guild), view=ModmailSettingsView())



class FeatureFlagSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for key, enabled in sorted(bot.data_manager.config.get("feature_flags", {}).items()):
            options.append(
                discord.SelectOption(
                    label=get_feature_flag_name(key),
                    value=key,
                    description=f"Currently {'on' if enabled else 'off'}",
                )
            )
        super().__init__(placeholder="Choose a feature to turn on or off...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        flags = bot.data_manager.config.setdefault("feature_flags", {})
        flags[key] = not bool(flags.get(key, False))
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_feature_flags_embed(interaction.guild), view=FeatureFlagView())


class FeatureFlagView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(FeatureFlagSelect())


class EscalationMatrixModal(discord.ui.Modal, title="Edit Punishment Scaling"):
    matrix_json = discord.ui.TextInput(
        label="Punishment Scaling JSON",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
    )

    def __init__(self):
        super().__init__()
        self.matrix_json.default = json.dumps(bot.data_manager.config.get("escalation_matrix", DEFAULT_ESCALATION_MATRIX), indent=2)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            payload = json.loads(self.matrix_json.value)
            if not isinstance(payload, list):
                raise ValueError("Matrix must be a JSON array.")
        except Exception as exc:
            await respond_with_error(interaction, f"Invalid punishment scaling JSON: {exc}", scope=SCOPE_SYSTEM)
            return

        bot.data_manager.config["escalation_matrix"] = payload
        await bot.data_manager.save_config()
        await interaction.response.send_message(
            embed=make_confirmation_embed(
                "Punishment Scaling Saved",
                "> The punishment scaling settings were updated successfully.",
                scope=SCOPE_SYSTEM,
                guild=interaction.guild,
            ),
            ephemeral=True,
        )


class EscalationMatrixView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Edit JSON", style=discord.ButtonStyle.primary)
    async def edit_matrix(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EscalationMatrixModal())

    @discord.ui.button(label="Reset Defaults", style=discord.ButtonStyle.secondary)
    async def reset_matrix(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot.data_manager.config["escalation_matrix"] = json.loads(json.dumps(DEFAULT_ESCALATION_MATRIX))
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_escalation_matrix_embed(interaction.guild), view=self)


class CannedReplyModal(discord.ui.Modal, title="Save Quick Reply"):
    template_name = discord.ui.TextInput(label="Template Name", placeholder="Acknowledged", max_length=60)
    reply_body = discord.ui.TextInput(label="Reply Body", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        replies = bot.data_manager.config.setdefault("modmail_canned_replies", {})
        replies[self.template_name.value.strip()] = self.reply_body.value.strip()
        await bot.data_manager.save_config()
        await interaction.response.send_message(
            embed=make_confirmation_embed(
                "Quick Reply Saved",
                "> The saved reply is now available in modmail.",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            ),
            ephemeral=True,
        )


class CannedRepliesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Add or Update Saved Reply", style=discord.ButtonStyle.primary)
    async def add_reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CannedReplyModal())



class ConfigImportModal(discord.ui.Modal, title="Paste Settings Backup"):
    config_json = discord.ui.TextInput(
        label="Settings JSON",
        style=discord.TextStyle.paragraph,
        placeholder='{"feature_flags": {...}}',
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            payload = json.loads(self.config_json.value)
            if not isinstance(payload, dict):
                raise ValueError("Config import payload must be a JSON object.")
        except Exception as exc:
            await respond_with_error(interaction, f"Invalid config JSON: {exc}", scope=SCOPE_SYSTEM)
            return

        merged, warnings = import_config_payload(bot.data_manager.config, payload)
        bot.data_manager.config = merged
        bot.data_manager._configure_cache_limits()
        await bot.data_manager.save_config()
        description = "> Settings were imported successfully."
        if warnings:
            description += "\n> " + "\n> ".join(warnings)
        await interaction.response.send_message(
            embed=make_confirmation_embed("Settings Imported", description, scope=SCOPE_SYSTEM, guild=interaction.guild),
            ephemeral=True,
        )


class ConfigDashboardActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Download Settings", value="export", description="Export a safe JSON backup of the current settings."),
            discord.SelectOption(label="Paste Settings", value="import", description="Import a settings backup from raw JSON."),
            discord.SelectOption(label="Feature Toggles", value="features", description="Turn bot features on or off."),
            discord.SelectOption(label="Punishment Scaling", value="scaling", description="Edit the escalation matrix used by punishments."),
            discord.SelectOption(label="Saved Replies", value="replies", description="Manage canned replies used in modmail."),
        ]
        super().__init__(
            placeholder="Choose a settings action...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "export":
            payload = export_config_payload(bot.data_manager.config)
            buffer = io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
            file = discord.File(buffer, filename="mbx-config-export.json")
            await interaction.response.send_message(
                embed=make_confirmation_embed(
                    "Settings Backup Ready",
                    "> A safe settings backup was generated successfully.",
                    scope=SCOPE_SYSTEM,
                    guild=interaction.guild,
                ),
                file=file,
                ephemeral=True,
            )
            return
        if action == "import":
            await interaction.response.send_modal(ConfigImportModal())
            return
        if action == "features":
            await interaction.response.send_message(embed=build_feature_flags_embed(interaction.guild), view=FeatureFlagView(), ephemeral=True)
            return
        if action == "scaling":
            await interaction.response.send_message(embed=build_escalation_matrix_embed(interaction.guild), view=EscalationMatrixView(), ephemeral=True)
            return
        if action == "replies":
            await interaction.response.send_message(embed=build_canned_replies_embed(interaction.guild), view=CannedRepliesView(), ephemeral=True)


class ConfigDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ConfigDashboardActionSelect())


class GuildIdModal(discord.ui.Modal, title="Set Guild ID"):
    guild_id = discord.ui.TextInput(label="Guild ID", max_length=25)

    def __init__(self, current_guild_id: int):
        super().__init__()
        self.guild_id.default = str(current_guild_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not self.guild_id.value.isdigit():
            await interaction.response.send_message("Invalid ID.", ephemeral=True)
            return
        bot.data_manager.config["guild_id"] = int(self.guild_id.value)
        await bot.data_manager.save_config()
        await interaction.response.send_message(f"Guild ID set to `{self.guild_id.value}`.", ephemeral=True)


class SetupDashboardActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Modmail Settings", value="modmail", description="Open the modmail behavior controls."),
            discord.SelectOption(label="Set Guild ID", value="guild_id", description="Change the guild ID used by the bot."),
            discord.SelectOption(label="Validate Setup", value="validate", description="Run the configuration validation checks."),
        ]
        super().__init__(
            placeholder="Choose another setup action...",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "modmail":
            await interaction.response.send_message(
                embed=build_modmail_settings_embed(interaction.guild),
                view=ModmailSettingsView(),
                ephemeral=True,
            )
            return
        if action == "guild_id":
            await interaction.response.send_modal(GuildIdModal(interaction.guild.id))
            return
        if action == "validate":
            if not get_feature_flag(bot.data_manager.config, "setup_validation", True):
                await respond_with_error(interaction, "The setup check is currently turned off in the feature settings.", scope=SCOPE_SYSTEM)
                return
            me = interaction.guild.me or interaction.guild.get_member(bot.user.id)
            if not me:
                await respond_with_error(interaction, "The bot member object could not be resolved for validation.", scope=SCOPE_SYSTEM)
                return
            findings = validate_guild_configuration(bot.data_manager.config, interaction.guild, me)
            await interaction.response.send_message(embed=build_setup_validation_embed(interaction.guild, findings), ephemeral=True)


class SetupDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ConfigTypeSelect("roles", row=0))
        self.add_item(ConfigTypeSelect("channels", row=1))
        self.add_item(SetupDashboardActionSelect())


async def setup(interaction: discord.Interaction):
    embed = build_setup_dashboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, view=SetupDashboardView(), ephemeral=True)

@tree.command(name="config", description="Open the bot settings panel | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def config_cmd(interaction: discord.Interaction):
    if not get_feature_flag(bot.data_manager.config, "config_panel", True):
        await respond_with_error(interaction, "The bot settings panel is currently turned off in the feature settings.", scope=SCOPE_SYSTEM)
        return
    embed = build_config_dashboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, view=ConfigDashboardView(), ephemeral=True)


