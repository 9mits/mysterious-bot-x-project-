# modules/commands/system.py
# System commands, event handlers, anti-nuke, native AutoMod bridge, and bot lifecycle.

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import json
import time
from datetime import timedelta
from typing import Optional, Dict, List, Any
import re

from core.constants import (
    DEFAULT_ANCHOR_ROLE_ID,
    DEFAULT_ARCHIVE_CAT_ID,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    DEFAULT_ROLE_OWNER,
    DEFAULT_RULES,
    DEFAULT_SPAM_ROLE_ID,
    SCOPE_MODERATION,
    SCOPE_SUPPORT,
    SCOPE_SYSTEM,
)
from core.services import (
    get_feature_flag,
    get_native_automod_settings,
    resolve_native_automod_policy,
)
from core.context import abuse_system, bot, tree
from core.utils import now_iso
from .shared import (
    logger,
    DANGEROUS_PERMISSIONS,
    has_dangerous_perm,
    truncate_text,
    format_duration,
    format_log_quote,
    format_reason_value,
    make_action_log_embed,
    make_embed,
    brand_embed,
    make_error_embed,
    get_user_display_name,
    format_user_ref,
    format_user_id_ref,
    get_primary_guild,
    send_log,
    send_punishment_log,
    has_permission_capability,
    respond_with_error,
    is_staff,
    resolve_member,
    get_valid_duration,
    get_punishment_log_channel_ids,
    prepare_modmail_relay_attachments,
    maybe_send_dm_modmail_panel,
    build_status_embed,
    build_rules_dashboard_embed,
    punish_rogue_mod,
    extract_snowflake_id,
)
from .cases import (
    get_case_label,
    describe_punishment_record,
    build_punishment_execution_log_embed,
    AccessView,
    RuleEditModal,
    RulesDashboardView,
)
from .modmail import (
    refresh_modmail_ticket_log,
    resolve_modmail_thread,
    resolve_modmail_user,
)
from .automod import (
    apply_native_automod_escalation,
    claim_native_automod_execution,
    record_native_automod_event,
    record_native_automod_step_application,
    get_triggered_native_automod_step,
    native_automod_rule_has_enforcement,
    is_native_automod_exempt,
    get_native_automod_action_label,
    AutoModWarningView,
)


class ActiveSelect(discord.ui.Select):
    def __init__(self, active_list):
        self.active_list = active_list
        options = []
        for idx, (uid, rec, expiry, case_num, name) in enumerate(active_list[:25]):
            reason = rec.get("reason", "Unknown")
            label = f"{name} ({get_case_label(rec, case_num)})"
            if len(label) > 100: label = label[:100]
            
            dur = rec.get("duration_minutes", 0)
            p_type = rec.get("type", "timeout")
            
            if dur == -1:
                desc = f"Banned • {reason}"
            elif dur > 0:
                remaining = expiry - discord.utils.utcnow()
                if remaining.days > 0:
                    rem_str = f"{remaining.days}d"
                else:
                    hours = remaining.seconds // 3600
                    if hours > 0:
                        rem_str = f"{hours}h"
                    else:
                        rem_str = f"{remaining.seconds // 60}m"
                desc = f"{'Tempban' if p_type=='ban' else 'Timeout'} • Expires in {rem_str}"
            
            if len(desc) > 100: desc = desc[:97] + "..."
            options.append(discord.SelectOption(label=label, description=desc, value=str(idx)))
            
        super().__init__(placeholder="Select active punishment to view details...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        uid, rec, expiry, case_num, name = self.active_list[idx]

        embed = make_embed(
            f"{get_case_label(rec, case_num)} Active Details",
            "> Current punishment state, timing, and staff notes.",
            kind="danger",
            scope=SCOPE_MODERATION,
            guild=interaction.guild,
        )

        embed.add_field(name="User", value=f"<@{uid}> (`{uid}`)", inline=True)

        mod_id = rec.get("moderator")
        embed.add_field(name="Moderator", value=f"<@{mod_id}> (`{mod_id}`)", inline=True)
        embed.add_field(name="Action", value=describe_punishment_record(rec), inline=True)
        embed.add_field(name="Violation", value=format_reason_value(rec.get("reason", "Unknown"), limit=250), inline=False)

        dur = rec.get("duration_minutes")
        if dur == -1:
            exp_str = "Never"
        else:
            exp_str = discord.utils.format_dt(expiry, "F")
        embed.add_field(name="Expires", value=exp_str, inline=True)
        if rec.get("escalated", False):
            embed.add_field(name="Escalated", value="Yes", inline=True)

        note = truncate_text(str(rec.get("note") or "").strip(), 1000)
        if note:
            embed.add_field(name="Internal Note", value=format_log_quote(note, limit=1000), inline=False)

        user_msg = rec.get("user_msg")
        if user_msg:
            embed.add_field(name="Message to User", value=format_log_quote(user_msg, limit=1000), inline=False)

        await interaction.response.edit_message(embed=embed, view=self.view)



class RuleDeleteSelect(discord.ui.Select):
    def __init__(self):
        rules = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        options = [discord.SelectOption(label=r) for r in list(rules.keys())[:25]]
        if not options:
            options = [discord.SelectOption(label="No rules found", value="none")]
        super().__init__(placeholder="Select rule to delete...", min_values=1, max_values=1, options=options)
    
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No rules to delete.", ephemeral=True)
            return
            
        name = self.values[0]
        rules = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        if name in rules:
            del rules[name]
            bot.data_manager.config["punishment_rules"] = rules
            await bot.data_manager.save_config()
            
            # Log
            log_embed = make_embed(
                "Punishment Rule Deleted",
                "> A punishment escalation rule was removed from the dashboard.",
                kind="danger",
                scope=SCOPE_SYSTEM,
                guild=interaction.guild,
            )
            log_embed.add_field(name="Actor", value=format_user_ref(interaction.user), inline=True)
            log_embed.add_field(name="Rule", value=name, inline=True)
            await send_log(interaction.guild, log_embed)
            
            await interaction.response.send_message(f"Rule **{name}** deleted.", ephemeral=True)
        else:
            await interaction.response.send_message("Rule not found.", ephemeral=True)


class RuleSelectForEdit(discord.ui.Select):
    def __init__(self):
        rules = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        options = []
        for name in list(rules.keys())[:25]:
            data = rules[name]
            desc = f"{format_duration(data['base'])} -> {format_duration(data['escalated'])}"
            options.append(discord.SelectOption(label=name, value=name, description=desc))
        
        if not options:
            options = [discord.SelectOption(label="No rules found", value="none")]
            
        super().__init__(placeholder="Select rule to edit...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No rules to edit.", ephemeral=True)
            return
            
        name = self.values[0]
        rules = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        if name in rules:
            data = rules[name]
            modal = RuleEditModal()
            modal.rule_name.default = name
            # Fix: Display "Ban" instead of -1
            modal.base_dur.default = "Ban" if data['base'] == -1 else str(data['base'])
            modal.esc_dur.default = "Ban" if data['escalated'] == -1 else str(data['escalated'])
            
            modal.title = f"Edit Rule: {name}"[:45]
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.send_message("Rule not found.", ephemeral=True)



class ArchiveConfirmView(discord.ui.View):
    def __init__(self, channel, target_cat, old_name, new_name, overwrites_save_data, final_overwrites):
        super().__init__(timeout=120)
        self.channel = channel
        self.target_cat = target_cat
        self.old_name = old_name
        self.new_name = new_name
        self.overwrites_save_data = overwrites_save_data
        self.final_overwrites = final_overwrites

    @discord.ui.button(label="Yes, Archive", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable view immediately to prevent double-clicks
        await interaction.response.edit_message(content="> Processing archive request...", view=None)
        
        # Save Config
        if "archived_channels" not in bot.data_manager.config: bot.data_manager.config["archived_channels"] = {}
        bot.data_manager.config["archived_channels"][str(self.channel.id)] = {
            "original_name": self.old_name,
            "category_id": self.channel.category_id,
            "overwrites": self.overwrites_save_data
        }
        await bot.data_manager.save_config()

        try:
            # Combine operations to reduce API calls and avoid rate limits (1 call vs 2)
            await self.channel.edit(
                name=self.new_name,
                category=self.target_cat,
                overwrites=self.final_overwrites,
                reason=f"Archived by {interaction.user}"
            )

        except Exception as e:
            await interaction.edit_original_response(content=f"Failed to archive channel: {e}")
            return

        await interaction.edit_original_response(content=f"Channel archived successfully to **{self.target_cat.name}**.")

        # Log
        log_embed = make_embed(
            "Channel Archived",
            "> A live channel was archived and moved into the configured archive category.",
            kind="info",
            scope=SCOPE_SYSTEM,
            guild=interaction.guild,
        )
        log_embed.add_field(name="Actor", value=format_user_ref(interaction.user), inline=True)
        log_embed.add_field(name="Original Name", value=self.old_name, inline=True)
        log_embed.add_field(name="Archived Name", value=self.new_name, inline=True)
        log_embed.add_field(name="Category", value=f"{self.target_cat.name} (`{self.target_cat.id}`)", inline=False)
        await send_log(interaction.guild, log_embed)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Archive operation cancelled.", view=None)
        self.stop()

class CloneConfirmView(discord.ui.View):
    def __init__(self, channel, target_cat, old_name, new_name, overwrites_save_data, final_overwrites):
        super().__init__(timeout=120)
        self.channel = channel
        self.target_cat = target_cat
        self.old_name = old_name
        self.new_name = new_name
        self.overwrites_save_data = overwrites_save_data
        self.final_overwrites = final_overwrites

    @discord.ui.button(label="Yes, Clone & Archive", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="> Processing clone & archive request...", view=None)
        
        # 1. Clone the channel
        try:
            new_channel = await self.channel.clone(reason=f"Cloned by {interaction.user}")
            await new_channel.edit(position=self.channel.position)
        except Exception as e:
            await interaction.edit_original_response(content=f"Failed to clone channel: {e}")
            return

        # 2. Archive the old channel
        if "archived_channels" not in bot.data_manager.config: bot.data_manager.config["archived_channels"] = {}
        bot.data_manager.config["archived_channels"][str(self.channel.id)] = {
            "original_name": self.old_name,
            "category_id": self.channel.category_id,
            "overwrites": self.overwrites_save_data
        }
        await bot.data_manager.save_config()

        try:
            await self.channel.edit(
                name=self.new_name,
                category=self.target_cat,
                overwrites=self.final_overwrites,
                reason=f"Archived (Cloned) by {interaction.user}"
            )
        except Exception as e:
            await interaction.edit_original_response(content=f"Channel cloned to {new_channel.mention}, but failed to archive old channel: {e}")
            return

        await interaction.edit_original_response(content=f"Success! Channel cloned to {new_channel.mention} and original archived.")
        
        try:
            embed = make_embed(
                "Channel Renewed",
                "> This channel was refreshed from a clean clone while the previous version was archived.",
                kind="success",
                scope=SCOPE_SYSTEM,
                guild=interaction.guild,
            )
            embed.add_field(name="Handled By", value=interaction.user.display_name, inline=True)
            await new_channel.send(embed=embed)
        except Exception:
            pass

        # Log
        log_embed = make_embed(
            "Channel Cloned and Archived",
            "> The original channel was archived and a fresh replacement was created.",
            kind="info",
            scope=SCOPE_SYSTEM,
            guild=interaction.guild,
        )
        log_embed.add_field(name="Actor", value=format_user_ref(interaction.user), inline=True)
        log_embed.add_field(name="Archived Channel", value=f"{self.channel.mention} (`{self.channel.id}`)", inline=True)
        log_embed.add_field(name="Fresh Clone", value=f"{new_channel.mention} (`{new_channel.id}`)", inline=True)
        await send_log(interaction.guild, log_embed)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Clone operation cancelled.", view=None)
        self.stop()



def build_test_env_embed():
    debug = bot.data_manager.config.get("debug", {})
    boost_status = "Enabled (Requirement Ignored)" if debug.get("bypass_boost") else "Disabled (Requirement Enforced)"
    cd_status = "Enabled (No Cooldowns)" if debug.get("bypass_cooldown") else "Disabled (Standard Cooldowns)"

    embed = make_embed(
        "Test Environment Control",
        "> Toggle debug-only flags used to validate premium and cooldown flows.",
        kind="warning",
        scope=SCOPE_SYSTEM,
    )
    embed.add_field(name="Boost Requirement Bypass", value=boost_status, inline=False)
    embed.add_field(name="Cooldown Bypass", value=cd_status, inline=False)
    return embed


class TestEnvView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Toggle Boost Bypass", style=discord.ButtonStyle.primary)
    async def toggle_boost(self, interaction: discord.Interaction, button: discord.ui.Button):
        if "debug" not in bot.data_manager.config:
            bot.data_manager.config["debug"] = {}
        current = bot.data_manager.config["debug"].get("bypass_boost", False)
        bot.data_manager.config["debug"]["bypass_boost"] = not current
        await bot.data_manager.save_config()
        embed = build_test_env_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Toggle Cooldown Bypass", style=discord.ButtonStyle.primary)
    async def toggle_cooldown(self, interaction: discord.Interaction, button: discord.ui.Button):
        if "debug" not in bot.data_manager.config:
            bot.data_manager.config["debug"] = {}
        current = bot.data_manager.config["debug"].get("bypass_cooldown", False)
        bot.data_manager.config["debug"]["bypass_cooldown"] = not current
        await bot.data_manager.save_config()
        embed = build_test_env_embed()
        await interaction.response.edit_message(embed=embed, view=self)


class ImmunityModal(discord.ui.Modal):
    def __init__(self, action):
        super().__init__(title=f"{action.capitalize()} Immunity")
        self.action = action
    
    user_id = discord.ui.TextInput(label="User ID", min_length=17, max_length=20)
    
    async def on_submit(self, interaction: discord.Interaction):
        uid = self.user_id.value.strip()
        if not uid.isdigit():
            await interaction.response.send_message("Invalid ID.", ephemeral=True)
            return
            
        lst = bot.data_manager.config.get("immunity_list", [])
        
        if self.action == "add":
            if uid not in lst:
                lst.append(uid)
                msg = f"Added <@{uid}> to immunity list."
            else:
                msg = "User is already immune."
        else:
            if uid in lst:
                lst.remove(uid)
                msg = f"Removed <@{uid}> from immunity list."
            else:
                msg = "User not found in immunity list."
        
        bot.data_manager.config["immunity_list"] = lst
        await bot.data_manager.save_config()
        await interaction.response.send_message(msg, ephemeral=True)

class SafetyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Add User", style=discord.ButtonStyle.success)
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImmunityModal("add"))

    @discord.ui.button(label="Remove User", style=discord.ButtonStyle.danger)
    async def remove_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImmunityModal("remove"))

    @discord.ui.button(label="View List", style=discord.ButtonStyle.secondary)
    async def view_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        lst = bot.data_manager.config.get("immunity_list", [])
        if not lst:
            await interaction.response.send_message("Immunity list is empty.", ephemeral=True)
        else:
            mentions = [f"<@{uid}>" for uid in lst]
            await interaction.response.send_message("**Immune Users:**\n" + ", ".join(mentions), ephemeral=True)

class AntiNukeResolveConfirm2(discord.ui.View):
    def __init__(self, restore_data, origin_message):
        super().__init__(timeout=60)
        self.restore_data = restore_data
        self.origin_message = origin_message

    @discord.ui.button(label="YES, RESTORE PERMISSIONS/ROLES", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Execute Restore
        guild = interaction.guild
        actor_id = self.restore_data.get("actor_id")
        stripped_ids = self.restore_data.get("stripped_roles", [])
        
        # 1. Restore Actor Roles
        actor = guild.get_member(actor_id)
        if not actor:
            try: actor = await guild.fetch_member(actor_id)
            except Exception: pass
        
        if actor and stripped_ids:
            roles_to_add = []
            for rid in stripped_ids:
                r = guild.get_role(rid)
                if r: roles_to_add.append(r)
            if roles_to_add:
                try:
                    await actor.add_roles(*roles_to_add, reason="Anti-Nuke: Action Resolved by Owner")
                except Exception:
                    pass

        # 2. Restore Original Action
        r_type = self.restore_data.get("type")
        if r_type == "role_perm":
            role = guild.get_role(self.restore_data.get("target_id"))
            perms_val = self.restore_data.get("permissions")
            if role and perms_val is not None:
                try:
                    await role.edit(permissions=discord.Permissions(perms_val), reason="Anti-Nuke: Action Resolved by Owner")
                except Exception:
                    pass
        elif r_type == "member_role":
            target = guild.get_member(self.restore_data.get("target_id"))
            role = guild.get_role(self.restore_data.get("extra_id"))
            if target and role:
                try:
                    await target.add_roles(role, reason="Anti-Nuke: Action Resolved by Owner")
                except Exception:
                    pass

        # 3. Disable the button on the original log message to prevent reuse
        if self.origin_message:
            try:
                embed = self.origin_message.embeds[0]
                embed.color = discord.Color.green()
                embed.add_field(name="Status", value="> Resolved by Owner", inline=True)
                brand_embed(embed, guild=guild, scope=SCOPE_SYSTEM)
                await self.origin_message.edit(embed=embed, view=None)
            except Exception:
                pass

        await interaction.response.edit_message(content="**Action Resolved.** Original permissions/roles restored.", view=None)

        embed = make_embed(
            "Security Alert: Anti-Nuke Resolved",
            "> A server owner manually restored the original state after an anti-nuke intervention.",
            kind="success",
            scope=SCOPE_SYSTEM,
            guild=guild,
        )
        embed.add_field(name="Actor", value=f"<@{actor_id}> (`{actor_id}`)", inline=True)
        embed.add_field(name="Resolution", value="Original permissions or roles restored", inline=True)
        await send_log(guild, embed)

class AntiNukeResolveConfirm1(discord.ui.View):
    def __init__(self, restore_data, origin_message):
        super().__init__(timeout=60)
        self.restore_data = restore_data
        self.origin_message = origin_message

    @discord.ui.button(label="Yes, I want to resolve", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="**FINAL WARNING**\n> This will give back the dangerous permissions/roles to the user and restore the moderator's powers.\n> Are you absolutely sure?",
            view=AntiNukeResolveConfirm2(self.restore_data, self.origin_message)
        )

class AntiNukeResolveView(discord.ui.View):
    def __init__(self, restore_data):
        super().__init__(timeout=None)
        self.restore_data = restore_data

    @discord.ui.button(label="Resolve", style=discord.ButtonStyle.success)
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        owner_role = bot.data_manager.config.get("role_owner", DEFAULT_ROLE_OWNER)
        if not any(r.id == owner_role for r in interaction.user.roles):
            await interaction.response.send_message("Only the Owner can use this.", ephemeral=True)
            return
        
        await interaction.response.send_message(
            "**Resolve Anti-Nuke Action?**\n> This will revert the bot's protection and allow the original action.",
            view=AntiNukeResolveConfirm1(self.restore_data, interaction.message),
            ephemeral=True
        )

# ----------------- Modmail System -----------------

def check_admin(interaction: discord.Interaction) -> bool:
    return has_permission_capability(interaction, "setup_panel")

def check_owner(interaction: discord.Interaction) -> bool:
    return has_permission_capability(interaction, "owner_panel")

@tree.command(name="commands", description="View registered slash commands.")
async def list_commands(interaction: discord.Interaction):
    # Owner/Admin only
    conf = bot.data_manager.config
    if not any(r.id in {conf.get("role_admin", DEFAULT_ROLE_ADMIN), conf.get("role_owner", DEFAULT_ROLE_OWNER)} for r in interaction.user.roles):
        await interaction.response.send_message("Access Denied.", ephemeral=True)
        return
        
    embed = make_embed(
        "System Command Registry",
        "> Registered application commands available to this bot instance.",
        kind="warning",
        scope=SCOPE_SYSTEM,
        guild=interaction.guild,
    )
    cmds = []
    for cmd in bot.tree.walk_commands():
        cmds.append(f"**/{cmd.name}**: {cmd.description}")
    
    desc = "\n".join(cmds)
    if len(desc) > 4000: desc = desc[:4000] + "..."
    embed.description = desc or "> No commands were found."
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def internals(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conf = bot.data_manager.config

    embed = make_embed(
        "System Internals",
        "> Read-only view of the bot's configured safety constants and operational roles.",
        kind="muted",
        scope=SCOPE_SYSTEM,
        guild=interaction.guild,
    )
    
    # Dangerous Permissions
    perms_list = [p.replace('_', ' ').title() for p in DANGEROUS_PERMISSIONS]
    embed.add_field(name="Dangerous Permissions (Anti-Nuke Triggers)", value=">>> " + "\n".join(perms_list), inline=False)
    
    # Current Config
    roles_info = (
        f"**Owner Role:** <@&{conf.get('role_owner', DEFAULT_ROLE_OWNER)}>\n"
        f"**Admin Role:** <@&{conf.get('role_admin', DEFAULT_ROLE_ADMIN)}>\n"
        f"**Mod Role:** <@&{conf.get('role_mod', DEFAULT_ROLE_MOD)}>\n"
        f"**Community Manager:** <@&{conf.get('role_community_manager', DEFAULT_ROLE_COMMUNITY_MANAGER)}>\n"
        f"**Anchor Role:** <@&{conf.get('role_anchor', DEFAULT_ANCHOR_ROLE_ID)}>"
    )
    embed.add_field(name="Current Role Configuration", value=f">>> {roles_info}", inline=False)
    
    mod_commands = [
        "/punish", "/history", "/active", "/undo",
        "/lock", "/unlock", "/purge"
    ]
    mod_cmds_fmt = "\n".join(mod_commands)
    embed.add_field(name="Classified Mod Commands", value=f">>> {mod_cmds_fmt}", inline=False)
    
    # Immunity List
    immune_count = len(bot.data_manager.config.get("immunity_list", []))
    embed.add_field(name="Immunity List", value=f"> {immune_count} users immune", inline=False)
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="archive", description="Archive the current channel.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def archive(interaction: discord.Interaction):
    # Do not defer immediately, we need to send the confirmation view first
    channel = interaction.channel
    guild = interaction.guild
    target_cat_id = bot.data_manager.config.get("category_archive", DEFAULT_ARCHIVE_CAT_ID)
    target_cat = guild.get_channel(target_cat_id)

    if not target_cat or not isinstance(target_cat, discord.CategoryChannel):
        await interaction.response.send_message(f"Archive category ({target_cat_id}) not found.", ephemeral=True)
        return

    old_name = channel.name
    new_name = f"archived-{old_name}"[:100]

    # Save state before archiving
    overwrites_data = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        overwrites_data.append({
            "id": target.id,
            "type": "role" if isinstance(target, discord.Role) else "member",
            "allow": allow.value,
            "deny": deny.value
        })
        
    # Overwrites: Reset all, set @everyone to deny view
    final_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
    }

    view = ArchiveConfirmView(channel, target_cat, old_name, new_name, overwrites_data, final_overwrites)
    await interaction.response.send_message(f"Are you sure you want to archive **{channel.name}**?", view=view, ephemeral=True)

@tree.command(name="unarchive", description="Restore an archived channel.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def unarchive(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    cid = str(channel.id)
    archives = bot.data_manager.config.get("archived_channels", {})

    if cid not in archives:
        # Migration Logic: Check for name match
        found_old_id = None
        for old_id, entry in archives.items():
            orig = entry.get("original_name", "")
            expected = f"archived-{orig}"[:100]
            if channel.name == expected:
                found_old_id = old_id
                break
        
        if found_old_id:
            data = archives.pop(found_old_id)
            archives[cid] = data
            bot.data_manager.config["archived_channels"] = archives
            await bot.data_manager.save_config()
            await interaction.followup.send(f"**System:** Channel ID mismatch detected (Server Transfer?).\n> Migrated archive data from `{found_old_id}` to `{cid}`.", ephemeral=True)
        else:
            await interaction.followup.send("This channel is not in the archive registry.", ephemeral=True)
            return
    
    data = archives[cid]
    
    # Restore Logic
    new_name = data.get("original_name", channel.name.replace("archived-", ""))
    cat_id = data.get("category_id")
    category = interaction.guild.get_channel(cat_id) if cat_id else None
    
    # Reconstruct Overwrites
    new_overwrites = {}
    for item in data.get("overwrites", []):
        obj_id = item["id"]
        target = interaction.guild.get_role(obj_id) if item["type"] == "role" else interaction.guild.get_member(obj_id)
        if target:
            allow = discord.Permissions(item["allow"])
            deny = discord.Permissions(item["deny"])
            new_overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)
    
    try:
        await channel.edit(name=new_name, category=category, overwrites=new_overwrites, reason=f"Unarchived by {interaction.user}")
    except Exception as e:
        await interaction.followup.send(f"Failed to unarchive channel: {e}", ephemeral=True)
        return
        
    # Cleanup
    del bot.data_manager.config["archived_channels"][cid]
    await bot.data_manager.save_config()
    
    await interaction.followup.send("Channel unarchived and restored.", ephemeral=True)
    
    # Log
    log_embed = make_embed(
        "Channel Unarchived",
        "> An archived channel was restored to its previous structure and permissions.",
        kind="success",
        scope=SCOPE_SYSTEM,
        guild=interaction.guild,
    )
    log_embed.add_field(name="Actor", value=format_user_ref(interaction.user), inline=True)
    log_embed.add_field(name="Channel", value=f"{channel.mention} (`{channel.id}`)", inline=True)
    log_embed.add_field(name="Restored Name", value=new_name, inline=True)
    await send_log(interaction.guild, log_embed)

@tree.command(name="clone", description="Archive this channel and create a replacement.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def clone(interaction: discord.Interaction):
    channel = interaction.channel
    guild = interaction.guild
    target_cat_id = bot.data_manager.config.get("category_archive", DEFAULT_ARCHIVE_CAT_ID)
    target_cat = guild.get_channel(target_cat_id)

    if not target_cat or not isinstance(target_cat, discord.CategoryChannel):
        await interaction.response.send_message(f"Archive category ({target_cat_id}) not found.", ephemeral=True)
        return

    old_name = channel.name
    new_name = f"archived-{old_name}"[:100]

    overwrites_data = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        overwrites_data.append({
            "id": target.id,
            "type": "role" if isinstance(target, discord.Role) else "member",
            "allow": allow.value,
            "deny": deny.value
        })
        
    final_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
    }

    view = CloneConfirmView(channel, target_cat, old_name, new_name, overwrites_data, final_overwrites)
    await interaction.response.send_message(f"**WARNING:** This will archive **{channel.name}** and create a fresh clone.\nAre you sure?", view=view, ephemeral=True)

@tree.command(name="rules", description="Configure punishment scaling rules.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def rules(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_rules_dashboard_embed(interaction.guild), view=RulesDashboardView(), ephemeral=True)

@tree.command(name="security", description="Manage anti-nuke protections.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def safety_panel(interaction: discord.Interaction):
    embed = make_embed(
        "Anti-Nuke Safety Panel",
        "> Manage users who are immune to automated anti-nuke enforcement.",
        kind="warning",
        scope=SCOPE_SYSTEM,
        guild=interaction.guild,
    )
    await interaction.response.send_message(embed=embed, view=SafetyView(), ephemeral=True)

@tree.command(name="access", description="Manage moderation access roles.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_owner)

async def access(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    roles = bot.data_manager.config.get("mod_roles", [])
    mentions = [f"<@&{rid}>" for rid in roles]
    desc = "**Allowed Mod Roles:**\n" + ", ".join(mentions) if mentions else "No specific roles configured (Admins & Mods allowed)."
    embed = make_embed(
        "Mod Access Configuration",
        f"> {desc}",
        kind="info",
        scope=SCOPE_SYSTEM,
        guild=interaction.guild,
    )
    view = AccessView()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@tree.command(name="lockdown", description="Hide server channels in an emergency.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_owner)
async def lockdown(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    
    # Save current state
    lockdown_data = {}
    channels_affected = 0
    
    for channel in guild.channels:
        # Skip if not a text/voice/stage channel (categories handled implicitly or skipped)
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)):
            continue
            
        overwrite = channel.overwrites_for(guild.default_role)
        # Save the current 'view_channel' setting (True, False, or None)
        lockdown_data[str(channel.id)] = overwrite.view_channel
        
        # Apply Lockdown
        overwrite.view_channel = False
        try:
            await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=f"Server Lockdown by {interaction.user}")
            channels_affected += 1
        except Exception:
            pass
    
    bot.data_manager.lockdown = lockdown_data
    await bot.data_manager.save_lockdown()
        
    await interaction.followup.send(f"**SERVER LOCKDOWN ACTIVE.**\n> Hidden {channels_affected} channels from @everyone.", ephemeral=True)

@tree.command(name="lift-lockdown", description="Restore channel visibility after lockdown.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_owner)
async def lift_lockdown(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    lockdown_data = bot.data_manager.lockdown
    
    if not lockdown_data:
        await interaction.followup.send("No lockdown data found.", ephemeral=True)
        return

    restored_count = 0
    for cid, original_perm in lockdown_data.items():
        channel = guild.get_channel(int(cid))
        if channel:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.view_channel = original_perm
            try:
                await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=f"Lockdown Lifted by {interaction.user}")
                restored_count += 1
            except Exception: pass

    bot.data_manager.lockdown = {}
    await bot.data_manager.save_lockdown()
    
    await interaction.followup.send(f"**LOCKDOWN LIFTED.**\n> Restored visibility for {restored_count} channels.", ephemeral=True)

async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=make_error_embed("Access Denied", "> You do not have permission to use this command.", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        return

    if isinstance(error, app_commands.CommandInvokeError):
        if isinstance(error.original, discord.NotFound) and error.original.code == 10062:
            logger.warning("Interaction timed out (10062).")
            return
        logger.exception("Command invoke failure [%s]: %s", interaction.command.qualified_name if interaction.command else "unknown", error.original)
    else:
        logger.exception("Command failed [%s]: %s", interaction.command.qualified_name if interaction.command else "unknown", error)
    
    try:
        await respond_with_error(
            interaction,
            "The bot hit an unexpected error while processing this action. No further changes were applied.",
            scope=SCOPE_SYSTEM,
        )
    except Exception:
        pass

@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    # Check if dangerous permissions were ADDED
    if not has_dangerous_perm(before.permissions) and has_dangerous_perm(after.permissions):
        # Calculate dangerous added permissions IMMEDIATELY before reverting
        dangerous_added = []
        for p in DANGEROUS_PERMISSIONS:
            if getattr(after.permissions, p) and not getattr(before.permissions, p):
                dangerous_added.append(p.replace('_', ' ').title())
        val_str = ", ".join(dangerous_added) if dangerous_added else "Unknown"

        # Fetch audit log to find the culprit
        async for entry in after.guild.audit_logs(limit=1, action=discord.AuditLogAction.role_update):
            if entry.target.id == after.id:
                actor = entry.user
                if actor.id == bot.user.id: return # Ignore self
                
                # Check Immunity
                if str(actor.id) in bot.data_manager.config.get("immunity_list", []):
                    return
                
                # Capture dangerous state for potential resolve
                restore_data = {"type": "role_perm", "target_id": after.id, "permissions": after.permissions.value}
                
                # REVERT
                try:
                    await after.edit(permissions=before.permissions, reason=f"Anti-Nuke: Reverting unauthorized permission change by {actor}")
                except Exception:
                    pass
                
                # Build Detailed Embed
                embed = make_embed(
                    "Security Alert: Dangerous Permissions Added",
                    "> A protected role permission change was reverted automatically.",
                    kind="danger",
                    scope=SCOPE_SYSTEM,
                    guild=after.guild,
                )
                embed.add_field(name="Actor", value=f"{actor.mention} (`{actor.id}`)", inline=True)
                joined_at = getattr(actor, "joined_at", None)
                embed.add_field(name="Actor Account Age", value=f"Created: {discord.utils.format_dt(actor.created_at, 'R')}\nJoined: {discord.utils.format_dt(joined_at, 'R') if joined_at else 'Unknown'}", inline=True)
                
                embed.add_field(name="Role", value=f"{after.mention} (`{after.id}`)", inline=True)
                embed.add_field(name="Role Created", value=discord.utils.format_dt(after.created_at, 'F'), inline=True)
                
                embed.add_field(name="Permissions Added", value=f"> {val_str}", inline=True)
                embed.add_field(name="Immediate Action", value="> Changes Reverted", inline=True)

                # PUNISH
                await punish_rogue_mod(after.guild, actor, f"Added dangerous permissions to role **{after.name}**", embed=embed, restore_data=restore_data)
                break

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id: return
    
    if payload.message_id in bot.active_executions:
        data = bot.active_executions[payload.message_id]
        
        # Prevent duplicate executions by removing immediately if threshold met
        # We check count first
        
        # Only count ✅
        if str(payload.emoji) != "✅": return
        
        channel = bot.get_channel(payload.channel_id)
        if not channel: return
        
        try:
            msg = await channel.fetch_message(payload.message_id)
        except Exception:
            return
            
        reaction = discord.utils.get(msg.reactions, emoji="✅")
        if not reaction: return
        
        # Count includes bot's reaction, so we check total count
        if reaction.count >= data["count"]:
            # Remove immediately to prevent race conditions
            del bot.active_executions[payload.message_id]
            
            # EXECUTE
            guild = bot.get_guild(payload.guild_id)
            if not guild:
                return
            
            try:
                target = await guild.fetch_member(data["target_id"])
            except discord.NotFound:
                try:
                    target = await bot.fetch_user(data["target_id"])
                except Exception:
                    target = None
            target_member = target if isinstance(target, discord.Member) else await resolve_member(guild, data["target_id"])
            
            try:
                moderator = await guild.fetch_member(data["moderator_id"])
            except Exception:
                moderator = None
            
            if target:
                # DM User
                try:
                    # 1:1 Match with execute_punishment DM
                    p_type = data["type"]
                    minutes = data["duration"]
                    
                    action_verb = "Banned" if p_type == "ban" else ("Kicked" if p_type == "kick" else "Timed Out")
                    
                    dm_embed = make_embed(
                        "Public Execution Result",
                        f"> You have been **{action_verb}** in **{guild.name}** through a public execution vote.",
                        kind="danger",
                        scope=SCOPE_MODERATION,
                        guild=guild,
                    )
                    dm_embed.add_field(name="Reason", value=format_reason_value(data["reason"], limit=1000), inline=False)
                    if data["user_msg"]:
                        dm_embed.add_field(name="Moderator Message", value=format_log_quote(data["user_msg"], limit=1024), inline=False)
                    
                    if p_type == "ban" and minutes == -1:
                        dm_embed.add_field(name="Duration", value="Ban", inline=True)
                    elif minutes > 0:
                        dm_embed.add_field(name="Duration", value=format_duration(minutes), inline=True)

                    from .roles import AppealView
                    view = AppealView(guild.id, target.id, data["moderator_id"], minutes, now_iso(), data["reason"])
                    await target.send(embed=dm_embed, view=view)
                except Exception: pass
                
                # Action
                try:
                    p_type = data["type"]
                    minutes = data["duration"]
                    reason = f"Public Execution (Vote passed) - {data['reason']}"
                    
                    if p_type == "ban":
                        await guild.ban(target, reason=reason)
                    elif p_type == "kick":
                        if not target_member:
                            raise ValueError("User is not in the server, cannot kick.")
                        await guild.kick(target_member, reason=reason)
                    elif p_type == "timeout":
                        if not target_member:
                            raise ValueError("User is not in the server, cannot timeout.")
                        await target_member.timeout(get_valid_duration(minutes), reason=reason)
                    elif p_type == "softban":
                        await guild.ban(target, reason=reason, delete_message_days=1)
                        await guild.unban(discord.Object(id=target.id), reason="Softban cleanup")
                    
                    # Log
                    record = {
                        "reason": f"Public Execution: {data['reason']}",
                        "moderator": moderator.id if moderator else data["moderator_id"],
                        "duration_minutes": minutes,
                        "timestamp": now_iso(),
                        "escalated": data["escalated"],
                        "note": data["note"],
                        "user_msg": data["user_msg"],
                        "target_name": get_user_display_name(target),
                        "type": p_type,
                        "active": p_type == "ban"
                    }
                    record = await bot.data_manager.add_punishment(str(target.id), record)
                    case_label = get_case_label(record)
                    
                    action_msg = "has been banned"
                    if p_type == "kick": action_msg = "has been kicked"
                    elif p_type == "timeout": action_msg = "has been timed out"
                    elif p_type == "warn": action_msg = "has been warned"
                    
                    await channel.send(f"{case_label}: {target.mention} {action_msg}.")
                    
                    # Log to channel
                    actor_ref = format_user_ref(moderator) if moderator else format_user_id_ref(data["moderator_id"])
                    log_embed = build_punishment_execution_log_embed(
                        guild=guild,
                        case_label=case_label,
                        actor=actor_ref,
                        target=format_user_ref(target),
                        record=record,
                        thumbnail=target.display_avatar.url,
                    )
                    log_embed.title = f"{case_label} Public Execution"
                    log_embed.description = "> A community vote threshold was reached and the configured action was executed."
                    log_embed.insert_field_at(2, name="Votes Reached", value=str(data["count"]), inline=True)
                    await send_punishment_log(guild, log_embed)
                    
                except Exception as e:
                    await channel.send(f"Execution failed: {e}")
            else:
                # Target not found (left server and fetch_user failed), clean up
                pass

@bot.command()
async def sync(ctx):
    # Check for Owner Role, Server Owner, or Administrator
    owner_role = bot.data_manager.config.get("role_owner", DEFAULT_ROLE_OWNER)
    is_owner = ctx.author.id == ctx.guild.owner_id
    has_role = any(r.id == owner_role for r in ctx.author.roles)
    is_admin = ctx.author.guild_permissions.administrator
    
    if not (is_owner or has_role or is_admin):
        await ctx.send("Access Denied: You need the Owner role, Server Owner status, or Administrator permission.")
        return
    
    guild = ctx.guild
    await ctx.send(f"Cleaning and syncing commands for **{guild.name}**...")
    bot._remove_disabled_application_commands()

    bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)

    global_deleted = await delete_remote_commands(guild=None)
    bot.tree.copy_global_to(guild=guild)
    guild_cmds = await bot.tree.sync(guild=guild)
    global_text = f" Removed {len(global_deleted)} stale global command(s)." if global_deleted else ""
    await ctx.send(f"Synced {len(guild_cmds)} server commands.{global_text}")
    logger.info(
        "Synced guild commands: %s | removed global commands: %s",
        [c.name for c in guild_cmds],
        global_deleted,
    )


async def delete_remote_commands(*, guild: Optional[discord.Guild]) -> List[str]:
    try:
        remote_commands = await bot.tree.fetch_commands(guild=guild)
    except discord.HTTPException as exc:
        scope = guild.name if guild else "global"
        logger.warning("Failed to fetch %s commands before sync: %s", scope, exc)
        return []

    deleted = []
    for command in remote_commands:
        try:
            await command.delete()
        except discord.HTTPException as exc:
            logger.warning("Failed to delete stale command /%s: %s", command.name, exc)
            continue
        deleted.append(command.name)
    return deleted

@tree.command(name="status", description="View bot latency and uptime.")
@app_commands.default_permissions(moderate_members=True)
async def status_cmd(interaction: discord.Interaction):
    if not is_staff(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    embed = build_status_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="serverinfo", description="View detailed information about this server.")
@app_commands.default_permissions(view_channel=True)
async def serverinfo_cmd(interaction: discord.Interaction):
    g = interaction.guild
    await g.fetch_channels()

    # Counts
    text_channels   = sum(1 for c in g.channels if isinstance(c, discord.TextChannel))
    voice_channels  = sum(1 for c in g.channels if isinstance(c, discord.VoiceChannel))
    stage_channels  = sum(1 for c in g.channels if isinstance(c, discord.StageChannel))
    forum_channels  = sum(1 for c in g.channels if isinstance(c, discord.ForumChannel))
    categories      = sum(1 for c in g.channels if isinstance(c, discord.CategoryChannel))
    total_channels  = text_channels + voice_channels + stage_channels + forum_channels

    # Members
    total_members = g.member_count or len(g.members)
    bots   = sum(1 for m in g.members if m.bot)
    humans = total_members - bots

    # Boost
    boost_level = g.premium_tier
    boosters    = g.premium_subscription_count or 0

    # Roles (exclude @everyone)
    role_count = len(g.roles) - 1

    created_ts = int(g.created_at.timestamp())

    embed = make_embed(
        g.name,
        kind="info",
        scope=SCOPE_SYSTEM,
        guild=g,
        thumbnail=g.icon.url if g.icon else None,
    )
    if g.banner:
        embed.set_image(url=g.banner.url)

    channel_breakdown = (
        f"{text_channels} text · {voice_channels} voice"
        + (f" · {stage_channels} stage" if stage_channels else "")
        + (f" · {forum_channels} forum" if forum_channels else "")
        + f" · {categories} categories"
    )

    embed.add_field(name="Owner",   value=f"<@{g.owner_id}>",                            inline=True)
    embed.add_field(name="Created", value=f"<t:{created_ts}:D> — <t:{created_ts}:R>",    inline=True)
    embed.add_field(name="ID",      value=str(g.id),                                     inline=True)

    embed.add_field(
        name="Members",
        value=f"> **{total_members}** total\n> {humans} humans · {bots} bots",
        inline=True,
    )
    embed.add_field(
        name="Channels",
        value=f"> **{total_channels}** total\n> {text_channels} text · {voice_channels} voice · {stage_channels} stage · {forum_channels} forum\n> {categories} categories",
        inline=True,
    )
    embed.add_field(name="​", value="​", inline=True)

    embed.add_field(
        name="Roles & Server",
        value=f"> **{role_count}** roles\n> Boost: Level {boost_level} · {boosters} boosts\n> Verification: {str(g.verification_level).replace('_', ' ').title()}",
        inline=True,
    )
    embed.add_field(
        name="Content",
        value=f"> **{len(g.emojis)}** / {g.emoji_limit} emojis\n> **{len(g.stickers)}** / {g.sticker_limit} stickers\n> Filter: {str(g.explicit_content_filter).replace('_', ' ').title()}",
        inline=True,
    )
    embed.add_field(name="​", value="​", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Check if roles were added
    if len(before.roles) < len(after.roles):
        added_roles = [r for r in after.roles if r not in before.roles]
        for role in added_roles:
            if has_dangerous_perm(role.permissions):
                # Dangerous role added
                async for entry in after.guild.audit_logs(limit=1, action=discord.AuditLogAction.member_role_update):
                    if entry.target.id == after.id:
                        actor = entry.user
                        if actor.id == bot.user.id: return # Ignore self
                        
                        # Check Immunity
                        if str(actor.id) in bot.data_manager.config.get("immunity_list", []):
                            return
                        
                        # Capture dangerous state for potential resolve
                        restore_data = {"type": "member_role", "target_id": after.id, "extra_id": role.id}
                        
                        # REVERT (Remove the role from the target)
                        try:
                            await after.remove_roles(role, reason=f"Anti-Nuke: Reverting unauthorized role grant by {actor}")
                        except Exception:
                            pass
                        
                        # Build Detailed Embed
                        embed = make_embed(
                            "Security Alert: Dangerous Role Granted",
                            "> A protected role grant was reverted and the actor was flagged.",
                            kind="danger",
                            scope=SCOPE_SYSTEM,
                            guild=after.guild,
                        )
                        embed.add_field(name="Actor", value=f"{actor.mention} (`{actor.id}`)", inline=True)
                        
                        embed.add_field(name="Target", value=f"{after.mention} (`{after.id}`)", inline=True)
                        embed.add_field(name="Target Account Age", value=f"Created: {discord.utils.format_dt(after.created_at, 'R')}\nJoined: {discord.utils.format_dt(after.joined_at, 'R') if after.joined_at else 'Unknown'}", inline=True)
                        
                        embed.add_field(name="Role Granted", value=f"{role.mention} (`{role.id}`)", inline=True)
                        embed.add_field(name="Role Created", value=discord.utils.format_dt(role.created_at, 'F'), inline=True)
                        embed.add_field(name="Immediate Action", value="> Role Grant Reverted", inline=True)

                        # PUNISH
                        await punish_rogue_mod(after.guild, actor, f"Granted dangerous role **{role.name}** to {after.mention}", embed=embed, restore_data=restore_data)
                        break

def claim_native_automod_bridge_event(
    *,
    guild_id: int,
    user_id: int,
    rule_id: int,
    rule_name: str,
    channel_id: Optional[int],
    content: str,
    matched_keyword: Optional[str],
    ttl_seconds: int = 20,
) -> bool:
    now_ts = time.time()
    cache = bot.native_automod_event_cache
    for cache_key, seen_at in list(cache.items()):
        if now_ts - seen_at > ttl_seconds:
            cache.pop(cache_key, None)

    normalized_rule = str(rule_id or 0) if rule_id else str(rule_name or "unknown-rule").strip().lower()
    dedupe_key = (
        int(guild_id or 0),
        int(user_id or 0),
        0,
        str(channel_id or 0),
        truncate_text(matched_keyword or content or normalized_rule, 120).strip().lower(),
    )
    previous = cache.get(dedupe_key)
    if previous and now_ts - previous <= ttl_seconds:
        return False

    cache[dedupe_key] = now_ts
    return True


def claim_native_automod_alert_message(message: discord.Message, *, ttl_seconds: int = 300) -> bool:
    now_ts = time.time()
    cache = bot.native_automod_event_cache
    for cache_key, seen_at in list(cache.items()):
        if now_ts - seen_at > ttl_seconds:
            cache.pop(cache_key, None)

    dedupe_key = (
        int(message.guild.id if message.guild else 0),
        0,
        0,
        f"native-alert-{message.id}",
        "",
    )
    previous = cache.get(dedupe_key)
    if previous and now_ts - previous <= ttl_seconds:
        return False

    cache[dedupe_key] = now_ts
    return True


def clean_native_automod_alert_value(value: Optional[str]) -> str:
    text = str(value or "").replace(">>>", " ").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def extract_native_automod_alert_context(message: discord.Message) -> Dict[str, Any]:
    user_id = None
    channel_id = None
    rule_name = None
    content = None
    matched_keyword = None

    if message.mentions:
        for mentioned in message.mentions:
            if not getattr(mentioned, "bot", False):
                user_id = mentioned.id
                break

    for embed in message.embeds:
        if not rule_name and embed.title:
            title_value = clean_native_automod_alert_value(embed.title)
            if title_value:
                rule_name = title_value
        if not content and embed.description:
            description_value = clean_native_automod_alert_value(embed.description)
            if description_value:
                content = description_value
        for field in embed.fields:
            field_name = clean_native_automod_alert_value(field.name).lower()
            field_value = clean_native_automod_alert_value(field.value)
            if not user_id and any(key in field_name for key in ("user", "member", "sender", "author", "who")):
                user_id = extract_snowflake_id(field_value)
            if not channel_id and any(key in field_name for key in ("channel", "where", "location")):
                channel_id = extract_snowflake_id(field_value)
            if not rule_name and any(key in field_name for key in ("rule", "filter")):
                rule_name = field_value
            if not matched_keyword and any(key in field_name for key in ("keyword", "match", "trigger")):
                matched_keyword = field_value
            if not content and any(key in field_name for key in ("content", "message", "what")):
                content = field_value

    return {
        "user_id": user_id,
        "channel_id": channel_id,
        "rule_name": truncate_text(rule_name or "", 250) or None,
        "content": truncate_text(content or "", 500) or None,
        "matched_keyword": truncate_text(matched_keyword or "", 120) or None,
    }


async def find_recent_native_automod_audit_entry(
    guild: discord.Guild,
    *,
    rule_name: Optional[str] = None,
    channel_id: Optional[int] = None,
) -> Optional[discord.AuditLogEntry]:
    cutoff = discord.utils.utcnow() - timedelta(minutes=2)
    actions = {
        discord.AuditLogAction.automod_block_message,
        discord.AuditLogAction.automod_flag_message,
        discord.AuditLogAction.automod_timeout_member,
        discord.AuditLogAction.automod_quarantine_user,
    }
    try:
        async for entry in guild.audit_logs(limit=20):
            if entry.action not in actions:
                continue
            if entry.created_at < cutoff:
                continue
            entry_rule_name = getattr(getattr(entry, "extra", None), "automod_rule_name", None)
            entry_channel = getattr(getattr(entry, "extra", None), "channel", None)
            if rule_name and entry_rule_name and str(entry_rule_name).lower() != str(rule_name).lower():
                continue
            if channel_id and entry_channel and getattr(entry_channel, "id", None) and int(entry_channel.id) != int(channel_id):
                continue
            return entry
    except discord.Forbidden:
        logger.warning("Native AutoMod alert fallback could not read audit logs in guild %s.", guild.id)
    except Exception as exc:
        logger.warning("Failed to read audit logs for native AutoMod alert fallback: %s", exc)
    return None


async def find_matching_native_automod_alert_message(
    guild: discord.Guild,
    *,
    alert_channel_id: Optional[int],
    member_id: int,
    rule_name: str,
    channel_id: Optional[int],
    content: str,
    attempts: int = 3,
    delay_seconds: float = 0.75,
) -> Optional[discord.Message]:
    if not alert_channel_id:
        return None

    channel = guild.get_channel_or_thread(int(alert_channel_id)) or guild.get_channel(int(alert_channel_id))
    if channel is None or not hasattr(channel, "history"):
        return None

    expected_rule = str(rule_name or "").strip().lower()
    expected_content = clean_native_automod_alert_value(content).lower()

    for attempt in range(max(1, attempts)):
        if attempt:
            await asyncio.sleep(delay_seconds)
        try:
            async for candidate in channel.history(limit=15):
                if candidate.author.id == bot.user.id:
                    continue
                if discord.utils.utcnow() - candidate.created_at > timedelta(minutes=3):
                    break

                context = extract_native_automod_alert_context(candidate)
                context_user_id = context.get("user_id")
                context_channel_id = context.get("channel_id")
                context_rule = str(context.get("rule_name") or "").strip().lower()
                context_content = clean_native_automod_alert_value(context.get("content")).lower()

                if context_user_id and int(context_user_id) != int(member_id):
                    continue
                if channel_id and context_channel_id and int(context_channel_id) != int(channel_id):
                    continue
                if expected_rule and context_rule and expected_rule != context_rule:
                    continue
                if expected_content and context_content:
                    if expected_content not in context_content and context_content not in expected_content:
                        continue

                return candidate
        except discord.Forbidden:
            logger.warning("Could not read native AutoMod alert channel %s in guild %s.", alert_channel_id, guild.id)
            return None
        except Exception as exc:
            logger.warning("Failed while searching native AutoMod alert channel %s: %s", alert_channel_id, exc)
            return None

    return None


def get_native_automod_audit_action_label(entry: Optional[discord.AuditLogEntry]) -> str:
    if entry is None:
        return "Send Alert Message"
    mapping = {
        discord.AuditLogAction.automod_block_message: "Block Message",
        discord.AuditLogAction.automod_flag_message: "Send Alert Message",
        discord.AuditLogAction.automod_timeout_member: "Timeout Member",
        discord.AuditLogAction.automod_quarantine_user: "Block Member Interactions",
    }
    return mapping.get(entry.action, "Send Alert Message")


def is_native_automod_audit_blocked(entry: Optional[discord.AuditLogEntry]) -> bool:
    if entry is None:
        return True
    return entry.action in {
        discord.AuditLogAction.automod_block_message,
        discord.AuditLogAction.automod_timeout_member,
        discord.AuditLogAction.automod_quarantine_user,
    }


async def run_native_automod_bridge(
    *,
    guild: discord.Guild,
    member: discord.Member,
    channel_id: Optional[int],
    rule_id: int,
    rule_name: str,
    content: str,
    matched_keyword: Optional[str],
    action_label: str,
    treated_as_blocked: bool,
    preferred_log_channel_id: Optional[int],
    native_log_url: Optional[str],
    source: str,
) -> None:
    settings = get_native_automod_settings(bot.data_manager.config)
    if is_native_automod_exempt(member, channel_id, settings):
        return

    content = content or "[Unavailable due to native AutoMod alert formatting]"
    if not claim_native_automod_bridge_event(
        guild_id=guild.id,
        user_id=member.id,
        rule_id=rule_id,
        rule_name=rule_name,
        channel_id=channel_id,
        content=content,
        matched_keyword=matched_keyword,
    ):
        return

    record_native_automod_event(
        user_id=member.id,
        rule_id=rule_id,
        rule_name=rule_name,
        content=content,
        matched_keyword=matched_keyword,
    )

    policy = resolve_native_automod_policy(bot.data_manager.config, rule_id=rule_id, rule_name=rule_name)
    triggered_step, warning_count = get_triggered_native_automod_step(
        user_id=member.id,
        rule_id=rule_id,
        rule_name=rule_name,
        policy=policy,
    )

    warning_id = f"AM-{rule_id}-{member.id}-{int(time.time())}"
    escalation_applied = False
    escalation_summary = "No automatic punishment was applied."
    escalated_case = None
    if triggered_step is not None:
        escalation_applied, escalation_summary, escalated_case = await apply_native_automod_escalation(
            guild,
            member,
            rule_id=rule_id,
            rule_name=rule_name,
            content=content,
            matched_keyword=matched_keyword,
            warning_count=warning_count,
            policy=policy,
            step=triggered_step,
        )
        if escalation_applied:
            record_native_automod_step_application(
                user_id=member.id,
                rule_id=rule_id,
                rule_name=rule_name,
                step=triggered_step,
            )
    await bot.data_manager.save_mod_stats()

    action_word = "blocked" if treated_as_blocked else "flagged"
    if settings.get("warning_dm_enabled", True) and not escalation_applied:
        try:
            dm_embed = make_embed(
                "AutoMod Warning",
                "\n".join([
                    f"> Your message in **{guild.name}** was {action_word} by Discord AutoMod.",
                    "> Repeating this rule can lead to a proper punishment.",
                ]),
                kind="warning" if not escalation_applied else "danger",
                scope=SCOPE_MODERATION,
                guild=guild,
                thumbnail=guild.icon.url if guild.icon else None,
            )
            dm_embed.add_field(name="Reason", value=format_reason_value(rule_name, limit=250), inline=False)
            dm_embed.add_field(
                name="Blocked Message" if treated_as_blocked else "Flagged Message",
                value=format_log_quote(content, limit=400),
                inline=False,
            )
            view = None
            if settings.get("report_button_enabled", True):
                view = AutoModWarningView(
                    guild_id=guild.id,
                    warning_id=warning_id,
                    rule_id=rule_id,
                    rule_name=rule_name,
                    content=content,
                    matched_keyword=matched_keyword,
                )
            await member.send(embed=dm_embed, view=view)
        except discord.Forbidden:
            logger.info("Native AutoMod bridge could not DM user %s for rule %s.", member.id, rule_id)
        except Exception as exc:
            logger.warning("Failed to send native AutoMod warning DM to %s: %s", member.id, exc)

    target_channel = guild.get_channel_or_thread(channel_id) if channel_id else None
    target_label = f"<#{channel_id}>" if channel_id else "Unknown Channel"
    if isinstance(target_channel, discord.Thread):
        target_label = f"{target_channel.mention} (`{target_channel.id}`)"
    elif hasattr(target_channel, "mention"):
        target_label = f"{target_channel.mention} (`{target_channel.id}`)"

    if not native_log_url and preferred_log_channel_id:
        native_alert_message = await find_matching_native_automod_alert_message(
            guild,
            alert_channel_id=preferred_log_channel_id,
            member_id=member.id,
            rule_name=rule_name,
            channel_id=channel_id,
            content=content,
        )
        if native_alert_message is not None:
            native_log_url = native_alert_message.jump_url

    if escalation_applied and escalated_case:
        detail_embed = build_punishment_execution_log_embed(
            guild=guild,
            case_label=get_case_label(escalated_case),
            actor=format_user_ref(bot.user),
            target=format_user_ref(member),
            record=escalated_case,
            thumbnail=member.display_avatar.url,
            native_log_url=native_log_url,
        )
    else:
        detail_embed = make_action_log_embed(
            "AutoMod Warning",
            "Discord AutoMod blocked or flagged a message and the bot sent a warning.",
            guild=guild,
            kind="warning",
            scope=SCOPE_MODERATION,
            actor=format_user_ref(member),
            target=target_label,
            reason=rule_name,
            message=content,
            notes=[
                f"Action: {action_label}",
                f"Matched Keyword: {matched_keyword or 'Unknown'}",
            ],
            thumbnail=member.display_avatar.url,
        )
        detail_embed.color = discord.Color.from_rgb(255, 153, 0)
        if native_log_url:
            detail_embed.add_field(name="Discord AutoMod Log", value=f"[Open Native Log]({native_log_url})", inline=False)

    selected_log_channel_id = None
    native_alert_channel_id = int(preferred_log_channel_id or 0) if preferred_log_channel_id else None

    log_candidates: List[int] = []
    preferred_candidates = (
        get_punishment_log_channel_ids()
        if escalation_applied
        else [
            bot.data_manager.config.get("automod_log_channel_id"),
            *get_punishment_log_channel_ids(),
        ]
    )
    for raw_channel_id in preferred_candidates:
        if not raw_channel_id:
            continue
        try:
            candidate_id = int(raw_channel_id)
        except (TypeError, ValueError):
            continue
        if candidate_id not in log_candidates:
            log_candidates.append(candidate_id)

    for candidate_id in log_candidates:
        if native_alert_channel_id and candidate_id == native_alert_channel_id:
            continue
        selected_log_channel_id = candidate_id
        break

    if selected_log_channel_id:
        log_channel = guild.get_channel_or_thread(selected_log_channel_id) or guild.get_channel(selected_log_channel_id)
        if log_channel is not None:
            try:
                await log_channel.send(embed=detail_embed)
            except Exception as exc:
                logger.warning("Failed to send native AutoMod moderation log to channel %s: %s", selected_log_channel_id, exc)
    logger.info(
        "Native AutoMod bridge processed event: guild=%s user=%s rule=%s action=%s source=%s",
        guild.id,
        member.id,
        rule_id,
        action_label,
        source,
    )


async def handle_native_automod_execution(execution: discord.AutoModAction, *, source: str) -> None:
    if not getattr(bot, "data_manager", None):
        return
    if not get_feature_flag(bot.data_manager.config, "native_automod_bridge", True):
        return

    settings = get_native_automod_settings(bot.data_manager.config)
    if not settings.get("enabled", True):
        return

    tracked_actions = {
        discord.AutoModRuleActionType.block_message,
        discord.AutoModRuleActionType.send_alert_message,
        discord.AutoModRuleActionType.timeout,
        discord.AutoModRuleActionType.block_member_interactions,
    }
    if execution.action.type not in tracked_actions:
        return
    if not claim_native_automod_execution(execution):
        return

    guild = bot.get_guild(execution.guild_id) or execution.guild
    if guild is None:
        return

    member = execution.member or await resolve_member(guild, execution.user_id)
    if member is None or member.bot:
        logger.warning(
            "Skipped native AutoMod bridge event without a resolvable member: guild=%s user=%s rule=%s source=%s",
            execution.guild_id,
            execution.user_id,
            execution.rule_id,
            source,
        )
        return

    rule = None
    try:
        rule = await execution.fetch_rule()
    except discord.Forbidden:
        logger.warning(
            "Native AutoMod bridge could not fetch rule %s in guild %s. Grant Manage Guild to allow detailed rule lookups.",
            execution.rule_id,
            execution.guild_id,
        )
    except Exception as exc:
        logger.warning("Failed to fetch native AutoMod rule %s: %s", execution.rule_id, exc)

    rule_name = rule.name if rule else f"Rule {execution.rule_id}"
    action_label = get_native_automod_action_label(execution)
    treated_as_blocked = native_automod_rule_has_enforcement(rule, execution)
    content = execution.content or execution.matched_content or "[Unavailable due to content intent settings]"
    matched_keyword = execution.matched_keyword or execution.matched_content or None
    native_alert_channel_id = None
    if rule is not None:
        for action in getattr(rule, "actions", []):
            if getattr(action, "type", None) == discord.AutoModRuleActionType.send_alert_message and getattr(action, "channel_id", None):
                native_alert_channel_id = int(action.channel_id)
                break

    await run_native_automod_bridge(
        guild=guild,
        member=member,
        channel_id=execution.channel_id,
        rule_id=int(execution.rule_id),
        rule_name=rule_name,
        content=content,
        matched_keyword=matched_keyword,
        action_label=action_label,
        treated_as_blocked=treated_as_blocked,
        preferred_log_channel_id=native_alert_channel_id,
        native_log_url=None,
        source=source,
    )


async def handle_native_automod_alert_message(message: discord.Message) -> None:
    if not message.guild:
        return
    if not getattr(bot, "data_manager", None):
        return
    if not get_feature_flag(bot.data_manager.config, "native_automod_bridge", True):
        return

    settings = get_native_automod_settings(bot.data_manager.config)
    if not settings.get("enabled", True):
        return
    if not claim_native_automod_alert_message(message):
        return

    context = extract_native_automod_alert_context(message)
    audit_entry = await find_recent_native_automod_audit_entry(
        message.guild,
        rule_name=context.get("rule_name"),
        channel_id=context.get("channel_id"),
    )

    user_id = context.get("user_id")
    audit_user = getattr(audit_entry, "user", None)
    if not user_id and audit_user and not getattr(audit_user, "bot", False):
        user_id = audit_user.id

    member = await resolve_member(message.guild, int(user_id)) if user_id else None
    if member is None or member.bot:
        logger.warning(
            "Native AutoMod alert fallback could not resolve the affected member. message_id=%s channel=%s",
            message.id,
            message.channel.id,
        )
        return

    rule_name = context.get("rule_name") or getattr(getattr(audit_entry, "extra", None), "automod_rule_name", None) or "Native AutoMod Rule"
    rule_target = getattr(audit_entry, "target", None)
    rule_id = int(getattr(rule_target, "id", 0) or 0)
    action_label = get_native_automod_audit_action_label(audit_entry)
    treated_as_blocked = is_native_automod_audit_blocked(audit_entry)
    content = context.get("content") or "[Unavailable from Discord native AutoMod alert]"
    matched_keyword = context.get("matched_keyword")
    action_channel = getattr(getattr(audit_entry, "extra", None), "channel", None)
    channel_id = context.get("channel_id") or getattr(action_channel, "id", None)

    await run_native_automod_bridge(
        guild=message.guild,
        member=member,
        channel_id=channel_id,
        rule_id=rule_id,
        rule_name=rule_name,
        content=content,
        matched_keyword=matched_keyword,
        action_label=action_label,
        treated_as_blocked=treated_as_blocked,
        preferred_log_channel_id=message.channel.id,
        native_log_url=message.jump_url,
        source="native alert message",
    )


@bot.event
async def on_automod_action(execution: discord.AutoModAction):
    await handle_native_automod_execution(execution, source="gateway event")


@bot.event
async def on_socket_raw_receive(message):
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return
    if "AUTO_MODERATION_ACTION_EXECUTION" not in message:
        return

    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return

    if payload.get("t") != "AUTO_MODERATION_ACTION_EXECUTION":
        return

    data = payload.get("d")
    if not isinstance(data, dict):
        return

    try:
        execution = discord.AutoModAction(data=data, state=bot._connection)
    except Exception as exc:
        logger.warning("Failed to parse raw native AutoMod payload: %s", exc)
        return

    await handle_native_automod_execution(execution, source="raw gateway fallback")


@bot.event
async def on_message(message: discord.Message):
    if message.guild and message.type is discord.MessageType.auto_moderation_action:
        await handle_native_automod_alert_message(message)
        return
    if message.author.bot: return

    # Anti-Spam: Mentions
    # Check immunity
    is_immune = str(message.author.id) in bot.data_manager.config.get("immunity_list", [])

    # Check for mentions
    has_everyone = message.mention_everyone
    
    # Specific Role ID
    target_role_id = bot.data_manager.config.get("role_mention_spam_target", DEFAULT_SPAM_ROLE_ID)
    has_role = any(r.id == target_role_id for r in message.role_mentions)
    
    if (has_everyone or has_role) and not is_immune:
        # Only apply to staff (Admins/Mods) as requested
        mod_roles_ids = bot.data_manager.config.get("mod_roles", [])
        is_author_staff = False
        if any(r.id in mod_roles_ids for r in message.author.roles):
            is_author_staff = True
        elif message.author.guild_permissions.administrator:
            is_author_staff = True
            
        if is_author_staff:
            now = time.time()
            q = abuse_system.mention_spam_tracker[message.author.id]
            q.append(now)
            
            # Clean old timestamps (> 60s)
            while q and now - q[0] > 60:
                q.popleft()
                
            if len(q) > 2:
                # Trigger
                q.clear() # Reset tracker
                
                # Build Embed
                embed = make_embed(
                    "Security Alert: Mention Spam Detected",
                    "> The anti-spam guard detected repeated protected mentions and triggered an automatic response.",
                    kind="danger",
                    scope=SCOPE_SYSTEM,
                    guild=message.guild,
                    thumbnail=message.author.display_avatar.url,
                )
                embed.add_field(name="Actor", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
                embed.add_field(name="Violation", value="Mass mention spam (@everyone/@here/member role)", inline=True)
                
                # Prepare restore data for resolve button (restores roles only)
                restore_data = {
                    "type": "spam_pardon",
                    "actor_id": message.author.id
                }
                
                # Punish & Delete
                await punish_rogue_mod(message.guild, message.author, "Mention Spam (Mass Pings)", embed=embed, restore_data=restore_data)
                try: await message.delete()
                except Exception: pass

    # Modmail Logic
    # 1. User -> Bot (DM)
    if isinstance(message.channel, discord.DMChannel):
        guild = get_primary_guild()
        ticket = bot.data_manager.modmail.get(str(message.author.id))
        if ticket and ticket.get("status") == "open":
            thread = await resolve_modmail_thread(guild, ticket)

            if thread:
                content = message.content if message.content else None
                embed = make_embed(
                    "User Reply",
                    truncate_text(content, 4096) or None,
                    kind="success",
                    scope=SCOPE_SUPPORT,
                    guild=guild,
                    author_name=message.author.display_name,
                    author_icon=message.author.display_avatar.url,
                )

                files, attachment_notice = await prepare_modmail_relay_attachments(message.attachments)

                try:
                    relay_kwargs = {"embed": embed}
                    if files:
                        relay_kwargs["files"] = files
                    await thread.send(**relay_kwargs)
                    ticket["last_user_message_at"] = now_iso()
                    ticket["last_sla_alert_at"] = None
                    await bot.data_manager.save_modmail()
                    if guild:
                        await refresh_modmail_ticket_log(guild, str(message.author.id))
                    if attachment_notice:
                        await message.channel.send(attachment_notice)
                    await message.add_reaction("✅")
                except Exception as e:
                    await message.channel.send(f"Error relaying message: {e}")
            else:
                await message.channel.send("Your previous ticket thread could not be found, so please open a new ticket below.")
                await maybe_send_dm_modmail_panel(
                    message.author,
                    guild=guild,
                    force=True,
                    intro="> Your old ticket could not be found. Please open a new ticket below so staff can help you again.",
                )
            return

        await maybe_send_dm_modmail_panel(
            message.author,
            guild=guild,
            intro="> You can open a ticket from this DM panel. Once it is open, just keep replying here and staff will receive it.",
        )
        return

    # 2. Staff -> Bot (Thread)
    if isinstance(message.channel, discord.Thread):
        # Check if this thread is a modmail thread
        target_uid = bot.data_manager.get_modmail_user_id(message.channel.id)
        
        if target_uid:
            # It is a modmail thread
            ticket = bot.data_manager.modmail.get(target_uid)
            if ticket and ticket.get("status") == "open":
                user = await resolve_modmail_user(target_uid)
                if user is None:
                    await message.channel.send("❌ Failed to send: The ticket user could not be resolved.")
                    return
                try:
                    content = message.content if message.content else None
                    embed = make_embed(
                        "Staff Reply",
                        truncate_text(content, 4096) or None,
                        kind="info",
                        scope=SCOPE_SUPPORT,
                        guild=message.guild,
                        author_name=f"{message.guild.name} Staff Team",
                        author_icon=message.guild.icon.url if message.guild.icon else None,
                    )
                    
                    files, attachment_notice = await prepare_modmail_relay_attachments(message.attachments)
                        
                    relay_kwargs = {"embed": embed}
                    if files:
                        relay_kwargs["files"] = files
                    await user.send(**relay_kwargs)
                    ticket["last_staff_message_at"] = now_iso()
                    await bot.data_manager.save_modmail()
                    await refresh_modmail_ticket_log(message.guild, target_uid)
                    if attachment_notice:
                        await message.channel.send(attachment_notice)
                    await message.add_reaction("📨")
                except discord.Forbidden:
                    await message.channel.send("❌ Failed to send: User has blocked the bot or DMs are disabled.")
                except Exception as e:
                    await message.channel.send(f"❌ Failed to send message: {e}")
            return

# ──────────────────────────── /branding ────────────────────────────

async def _fetch_image(url: str) -> bytes:
    async with bot.session.get(url) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status}")
        return await resp.read()


# ── Global branding modals ──

class GlobalUsernameModal(discord.ui.Modal, title="Change Bot Username"):
    username = discord.ui.TextInput(label="New Username", min_length=2, max_length=32)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await bot.user.edit(username=self.username.value.strip())
            await interaction.followup.send(f"Global username updated to **{self.username.value.strip()}**.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


class GlobalAvatarModal(discord.ui.Modal, title="Change Global Avatar"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://example.com/image.png", min_length=10, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await _fetch_image(self.url.value.strip())
            await bot.user.edit(avatar=data)
            await interaction.followup.send("Global avatar updated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


class GlobalBannerModal(discord.ui.Modal, title="Change Global Banner"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://example.com/banner.png", min_length=10, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await _fetch_image(self.url.value.strip())
            await bot.user.edit(banner=data)
            await interaction.followup.send("Global banner updated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


# ── Server branding modals ──

class ServerNicknameModal(discord.ui.Modal, title="Change Server Nickname"):
    nickname = discord.ui.TextInput(label="Nickname", placeholder="Leave blank to clear back to username", max_length=32, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            nick = self.nickname.value.strip() or None
            await interaction.guild.me.edit(nick=nick)
            msg = f"Server nickname set to **{nick}**." if nick else "Server nickname cleared."
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


class ServerAvatarModal(discord.ui.Modal, title="Change Server Avatar"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://example.com/image.png", min_length=10, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await _fetch_image(self.url.value.strip())
            await interaction.guild.me.edit(avatar=data)
            await interaction.followup.send("Server avatar updated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


class ServerBannerModal(discord.ui.Modal, title="Change Server Banner"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://example.com/banner.png", min_length=10, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            data = await _fetch_image(self.url.value.strip())
            await interaction.guild.me.edit(banner=data)
            await interaction.followup.send("Server banner updated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


class ServerBioModal(discord.ui.Modal, title="Change Server Bio"):
    bio = discord.ui.TextInput(
        label="Bio",
        style=discord.TextStyle.paragraph,
        placeholder="Enter a bio for this server...",
        max_length=190,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            bio_value = self.bio.value.strip() or None
            await interaction.guild.me.edit(bio=bio_value)
            msg = "Server bio updated." if bio_value else "Server bio cleared."
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


# ── Embed builders ──

def _build_global_branding_embed() -> discord.Embed:
    user = bot.user
    embed = make_embed(
        "Global Branding",
        "> These changes apply across **all servers** the bot is in.",
        kind="info",
        scope=SCOPE_SYSTEM,
    )
    embed.add_field(name="Username", value=str(user), inline=True)
    embed.add_field(name="Bot ID", value=str(user.id), inline=True)
    embed.add_field(name="Avatar", value="Set" if user.avatar else "Default", inline=True)
    embed.add_field(name="Banner", value="Set" if user.banner else "None", inline=True)
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    if user.banner:
        embed.set_image(url=user.banner.url)
    return embed


def _build_server_branding_embed(guild: discord.Guild) -> discord.Embed:
    me = guild.me
    embed = make_embed(
        "Server Branding",
        f"> These changes only apply in **{guild.name}**.",
        kind="info",
        scope=SCOPE_SYSTEM,
        guild=guild,
    )
    embed.add_field(name="Nickname", value=me.nick or "None (using username)", inline=True)
    embed.add_field(name="Server Avatar", value="Set" if me.guild_avatar else "Using global", inline=True)
    embed.add_field(name="Server Banner", value="Set" if getattr(me, "guild_banner", None) else "None", inline=True)
    if me.guild_avatar:
        embed.set_thumbnail(url=me.guild_avatar.url)
    elif me.avatar:
        embed.set_thumbnail(url=me.avatar.url)
    return embed


# ── Views ──

class GlobalBrandingActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Change Username", value="username", description="Update the global bot username."),
            discord.SelectOption(label="Change Avatar", value="avatar", description="Upload a global avatar from an image URL."),
            discord.SelectOption(label="Change Banner", value="banner", description="Upload a global banner from an image URL."),
            discord.SelectOption(label="Remove Avatar", value="remove_avatar", description="Reset the global avatar."),
            discord.SelectOption(label="Remove Banner", value="remove_banner", description="Clear the global banner."),
        ]
        super().__init__(placeholder="Choose a global branding action...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "username":
            await interaction.response.send_modal(GlobalUsernameModal())
            return
        if action == "avatar":
            await interaction.response.send_modal(GlobalAvatarModal())
            return
        if action == "banner":
            await interaction.response.send_modal(GlobalBannerModal())
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if action == "remove_avatar":
                await bot.user.edit(avatar=None)
                await interaction.followup.send("Global avatar removed.", ephemeral=True)
            elif action == "remove_banner":
                await bot.user.edit(banner=None)
                await interaction.followup.send("Global banner removed.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


class GlobalBrandingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(GlobalBrandingActionSelect())


class ServerBrandingActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Change Nickname", value="nickname", description="Update the bot nickname for this server."),
            discord.SelectOption(label="Change Avatar", value="avatar", description="Upload a server avatar from an image URL."),
            discord.SelectOption(label="Change Banner", value="banner", description="Upload a server banner from an image URL."),
            discord.SelectOption(label="Change Bio", value="bio", description="Update the server-specific bot bio."),
            discord.SelectOption(label="Clear Nickname", value="clear_nickname", description="Use the global bot username in this server."),
            discord.SelectOption(label="Remove Avatar", value="remove_avatar", description="Revert this server to the global avatar."),
            discord.SelectOption(label="Remove Banner", value="remove_banner", description="Clear the server-specific banner."),
            discord.SelectOption(label="Clear Bio", value="clear_bio", description="Clear the server-specific bot bio."),
        ]
        super().__init__(placeholder="Choose a server branding action...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "nickname":
            await interaction.response.send_modal(ServerNicknameModal())
            return
        if action == "avatar":
            await interaction.response.send_modal(ServerAvatarModal())
            return
        if action == "banner":
            await interaction.response.send_modal(ServerBannerModal())
            return
        if action == "bio":
            await interaction.response.send_modal(ServerBioModal())
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if action == "clear_nickname":
                await interaction.guild.me.edit(nick=None)
                await interaction.followup.send("Server nickname cleared.", ephemeral=True)
            elif action == "remove_avatar":
                await interaction.guild.me.edit(avatar=None)
                await interaction.followup.send("Server avatar removed (reverted to global).", ephemeral=True)
            elif action == "remove_banner":
                await interaction.guild.me.edit(banner=None)
                await interaction.followup.send("Server banner removed.", ephemeral=True)
            elif action == "clear_bio":
                await interaction.guild.me.edit(bio=None)
                await interaction.followup.send("Server bio cleared.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)


class ServerBrandingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ServerBrandingActionSelect())


# ── Commands ──

branding_group = app_commands.Group(name="branding", description="Manage bot profile and server appearance.")


@branding_group.command(name="global", description="Edit the bot's global profile.")
@app_commands.check(check_owner)
async def branding_global(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_global_branding_embed(), view=GlobalBrandingView(), ephemeral=True)


@branding_group.command(name="server", description="Edit this server's bot profile.")
@app_commands.check(check_owner)
async def branding_server(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_server_branding_embed(interaction.guild), view=ServerBrandingView(), ephemeral=True)


# ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    bot.start_time = time.time()
    logger.info(f"[READY] Logged in as {bot.user} (ID: {bot.user.id}). System operational.")



class SystemCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        await on_guild_role_update(before, after)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        await on_member_update(before, after)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await on_message(message)

    @commands.Cog.listener()
    async def on_ready(self):
        await on_ready()

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await on_app_command_error(interaction, error)


async def setup(bot):
    await bot.add_cog(SystemCog(bot))
    bot.tree.add_command(list_commands)
    bot.tree.add_command(archive)
    bot.tree.add_command(unarchive)
    bot.tree.add_command(clone)
    bot.tree.add_command(rules)
    bot.tree.add_command(safety_panel)
    bot.tree.add_command(access)
    bot.tree.add_command(lockdown)
    bot.tree.add_command(lift_lockdown)
    bot.tree.add_command(status_cmd)
    bot.tree.add_command(serverinfo_cmd)
    bot.tree.add_command(branding_group)
    bot.add_command(sync)
    bot.tree.on_error = on_app_command_error
