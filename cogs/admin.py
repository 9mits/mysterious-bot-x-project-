"""Admin commands, anti-nuke views, and branding — split from system.py."""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List


from core.constants import (
    DEFAULT_ANCHOR_ROLE_ID,
    DEFAULT_ARCHIVE_CAT_ID,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    DEFAULT_ROLE_OWNER,
    DEFAULT_RULES,
    SCOPE_MODERATION,
    SCOPE_SYSTEM,
)
from core.context import bot, tree
from .shared import (
    logger,
    DANGEROUS_PERMISSIONS,
    truncate_text,
    format_duration,
    format_log_quote,
    format_reason_value,
    make_embed,
    brand_embed,
    format_user_ref,
    send_log,
    has_permission_capability,
    is_staff,
    build_status_embed,
    build_rules_dashboard_embed,
)
from .cases import (
    get_case_label,
    describe_punishment_record,
)
from .case_panel import AccessView, RuleEditModal, RulesDashboardView

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

    async def callback(self, interaction: discord.Interaction) -> None:
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
    
    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message(embed=make_embed("No Rules", "> No rules to delete.", kind="info", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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

            await interaction.response.send_message(embed=make_embed("Rule Deleted", f"> Rule **{name}** deleted.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        else:
            await interaction.response.send_message(embed=make_embed("Not Found", "> Rule not found.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


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

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message(embed=make_embed("No Rules", "> No rules to edit.", kind="info", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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
            await interaction.response.send_message(embed=make_embed("Not Found", "> Rule not found.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)



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
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Disable view immediately to prevent double-clicks
        await interaction.response.edit_message(embed=make_embed("Processing", "> Processing the archive request...", kind="muted", scope=SCOPE_SYSTEM, guild=interaction.guild), view=None)
        
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
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=make_embed("Archive Cancelled", "> The channel was not archived.", kind="muted", scope=SCOPE_SYSTEM, guild=interaction.guild), view=None)
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
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=make_embed("Processing", "> Processing the clone & archive request...", kind="muted", scope=SCOPE_SYSTEM, guild=interaction.guild), view=None)
        
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
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=make_embed("Clone Cancelled", "> The channel was not cloned or archived.", kind="muted", scope=SCOPE_SYSTEM, guild=interaction.guild), view=None)
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
    async def toggle_boost(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if "debug" not in bot.data_manager.config:
            bot.data_manager.config["debug"] = {}
        current = bot.data_manager.config["debug"].get("bypass_boost", False)
        bot.data_manager.config["debug"]["bypass_boost"] = not current
        await bot.data_manager.save_config()
        embed = build_test_env_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Toggle Cooldown Bypass", style=discord.ButtonStyle.primary)
    async def toggle_cooldown(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
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
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        uid = self.user_id.value.strip()
        if not uid.isdigit():
            await interaction.response.send_message(embed=make_embed("Invalid ID", "> Invalid user ID.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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
        await interaction.response.send_message(embed=make_embed("Immunity Updated", f"> {msg}", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)

class SafetyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Add User", style=discord.ButtonStyle.success)
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ImmunityModal("add"))

    @discord.ui.button(label="Remove User", style=discord.ButtonStyle.danger)
    async def remove_user(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ImmunityModal("remove"))

    @discord.ui.button(label="View List", style=discord.ButtonStyle.secondary)
    async def view_list(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        lst = bot.data_manager.config.get("immunity_list", [])
        if not lst:
            await interaction.response.send_message(embed=make_embed("Immunity List", "> Immunity list is empty.", kind="info", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        else:
            mentions = [f"<@{uid}>" for uid in lst]
            await interaction.response.send_message(embed=make_embed("Immune Users", "> " + ", ".join(mentions), kind="info", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)

class AntiNukeResolveConfirm2(discord.ui.View):
    def __init__(self, restore_data, origin_message):
        super().__init__(timeout=60)
        self.restore_data = restore_data
        self.origin_message = origin_message

    @discord.ui.button(label="YES, RESTORE PERMISSIONS/ROLES", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
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
                except Exception as exc:
                    logger.warning("Anti-Nuke resolve: failed to restore stripped roles to %s: %s", actor.id, exc)

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
                except Exception as exc:
                    logger.warning("Anti-Nuke resolve: failed to restore role %s to %s: %s", role.id, target.id, exc)

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

        await interaction.response.edit_message(embed=make_embed("Action Resolved", "> Original permissions and roles have been restored.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), view=None)

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
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="**FINAL WARNING**\n> This will give back the dangerous permissions/roles to the user and restore the moderator's powers.\n> Are you absolutely sure?",
            view=AntiNukeResolveConfirm2(self.restore_data, self.origin_message)
        )

class AntiNukeResolveView(discord.ui.View):
    def __init__(self, restore_data):
        super().__init__(timeout=None)
        self.restore_data = restore_data

    @discord.ui.button(label="Resolve", style=discord.ButtonStyle.success)
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        owner_role = bot.data_manager.config.get("role_owner", DEFAULT_ROLE_OWNER)
        if not any(r.id == owner_role for r in interaction.user.roles):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> Only the Owner can use this.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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
@app_commands.default_permissions(administrator=True)
async def list_commands(interaction: discord.Interaction):
    # Owner/Admin only
    conf = bot.data_manager.config
    if not any(r.id in {conf.get("role_admin", DEFAULT_ROLE_ADMIN), conf.get("role_owner", DEFAULT_ROLE_OWNER)} for r in interaction.user.roles):
        await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this command.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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
        await interaction.response.send_message(embed=make_embed("Category Not Found", f"> Archive category ({target_cat_id}) not found.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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
    await interaction.response.send_message(embed=make_embed("Confirm Archive", f"> Are you sure you want to archive **{channel.name}**?", kind="warning", scope=SCOPE_SYSTEM, guild=interaction.guild), view=view, ephemeral=True)

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
            await interaction.followup.send(embed=make_embed("Migration Notice", f"> Channel ID mismatch detected (Server Transfer?). Migrated archive data from `{found_old_id}` to `{cid}`.", kind="info", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        else:
            await interaction.followup.send(embed=make_embed("Not Archived", "> This channel is not in the archive registry.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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
        await interaction.followup.send(embed=make_embed("Failed", f"> Failed to unarchive channel: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        return

    # Cleanup
    del bot.data_manager.config["archived_channels"][cid]
    await bot.data_manager.save_config()

    await interaction.followup.send(embed=make_embed("Channel Unarchived", "> Channel unarchived and restored.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
    
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
        await interaction.response.send_message(embed=make_embed("Category Not Found", f"> Archive category ({target_cat_id}) not found.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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
    await interaction.response.send_message(embed=make_embed("Confirm Clone & Archive", f"> **WARNING:** This will archive **{channel.name}** and create a fresh clone. Are you sure?", kind="warning", scope=SCOPE_SYSTEM, guild=interaction.guild), view=view, ephemeral=True)

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
        
    await interaction.followup.send(embed=make_embed("Server Lockdown Active", f"> Hidden {channels_affected} channels from @everyone.", kind="danger", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)

@tree.command(name="lift-lockdown", description="Restore channel visibility after lockdown.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_owner)
async def lift_lockdown(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    lockdown_data = bot.data_manager.lockdown
    
    if not lockdown_data:
        await interaction.followup.send(embed=make_embed("No Lockdown", "> No lockdown data found.", kind="info", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
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
    
    await interaction.followup.send(embed=make_embed("Lockdown Lifted", f"> Restored visibility for {restored_count} channels.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)

@commands.command(name="sync")
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
        await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this command.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        return

    embed = build_status_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="serverinfo", description="View detailed information about this server.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.check(check_admin)
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

    embed.add_field(name="Owner",   value=f"<@{g.owner_id}>",                            inline=True)
    embed.add_field(name="Created", value=f"<t:{created_ts}:D> — <t:{created_ts}:R>",    inline=True)
    embed.add_field(name="ID",      value=str(g.id),                                     inline=True)

    embed.add_field(
        name="Members",
        value=f"**{total_members}** total\n{humans} humans · {bots} bots",
        inline=True,
    )
    embed.add_field(
        name="Channels",
        value=f"**{total_channels}** total\n{text_channels} text · {voice_channels} voice · {stage_channels} stage · {forum_channels} forum\n{categories} categories",
        inline=True,
    )
    embed.add_field(name="​", value="​", inline=True)

    embed.add_field(
        name="Roles & Server",
        value=f"**{role_count}** roles\nBoost: Level {boost_level} · {boosters} boosts\nVerification: {str(g.verification_level).replace('_', ' ').title()}",
        inline=True,
    )
    embed.add_field(
        name="Content",
        value=f"**{len(g.emojis)}** / {g.emoji_limit} emojis\n**{len(g.stickers)}** / {g.sticker_limit} stickers\nFilter: {str(g.explicit_content_filter).replace('_', ' ').title()}",
        inline=True,
    )
    embed.add_field(name="​", value="​", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)



async def _fetch_image(url: str) -> bytes:
    async with bot.session.get(url) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status}")
        return await resp.read()


class GlobalUsernameModal(discord.ui.Modal, title="Change Bot Username"):
    username = discord.ui.TextInput(label="New Username", min_length=2, max_length=32)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await bot.user.edit(username=self.username.value.strip())
            await interaction.followup.send(embed=make_embed("Username Updated", f"> Global username updated to **{self.username.value.strip()}**.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


class GlobalAvatarModal(discord.ui.Modal, title="Change Global Avatar"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://example.com/image.png", min_length=10, max_length=500)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            data = await _fetch_image(self.url.value.strip())
            await bot.user.edit(avatar=data)
            await interaction.followup.send(embed=make_embed("Avatar Updated", "> Global avatar updated.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


class GlobalBannerModal(discord.ui.Modal, title="Change Global Banner"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://example.com/banner.png", min_length=10, max_length=500)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            data = await _fetch_image(self.url.value.strip())
            await bot.user.edit(banner=data)
            await interaction.followup.send(embed=make_embed("Banner Updated", "> Global banner updated.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


# ── Server branding modals ──

class ServerNicknameModal(discord.ui.Modal, title="Change Server Nickname"):
    nickname = discord.ui.TextInput(label="Nickname", placeholder="Leave blank to clear back to username", max_length=32, required=False)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            nick = self.nickname.value.strip() or None
            await interaction.guild.me.edit(nick=nick)
            msg = f"Server nickname set to **{nick}**." if nick else "Server nickname cleared."
            await interaction.followup.send(embed=make_embed("Nickname Updated", f"> {msg}", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


class ServerAvatarModal(discord.ui.Modal, title="Change Server Avatar"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://example.com/image.png", min_length=10, max_length=500)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            data = await _fetch_image(self.url.value.strip())
            await interaction.guild.me.edit(avatar=data)
            await interaction.followup.send(embed=make_embed("Avatar Updated", "> Server avatar updated.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


class ServerBannerModal(discord.ui.Modal, title="Change Server Banner"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://example.com/banner.png", min_length=10, max_length=500)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            data = await _fetch_image(self.url.value.strip())
            await interaction.guild.me.edit(banner=data)
            await interaction.followup.send(embed=make_embed("Banner Updated", "> Server banner updated.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


class ServerBioModal(discord.ui.Modal, title="Change Server Bio"):
    bio = discord.ui.TextInput(
        label="Bio",
        style=discord.TextStyle.paragraph,
        placeholder="Enter a bio for this server...",
        max_length=190,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            bio_value = self.bio.value.strip() or None
            await interaction.guild.me.edit(bio=bio_value)
            msg = "Server bio updated." if bio_value else "Server bio cleared."
            await interaction.followup.send(embed=make_embed("Bio Updated", f"> {msg}", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


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
        user = bot.user
        options = [
            discord.SelectOption(label="Change Username", value="username", description="Update the global bot username."),
            discord.SelectOption(label="Change Avatar", value="avatar", description="Upload a global avatar from an image URL."),
            discord.SelectOption(label="Change Banner", value="banner", description="Upload a global banner from an image URL."),
        ]
        if user.avatar:
            options.append(discord.SelectOption(label="╌ Remove Avatar", value="remove_avatar", description="Reset back to the default avatar."))
        if user.banner:
            options.append(discord.SelectOption(label="╌ Remove Banner", value="remove_banner", description="Clear the global banner."))
        super().__init__(placeholder="Choose a global branding action...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
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
                await interaction.followup.send(embed=make_embed("Avatar Removed", "> Global avatar removed.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
            elif action == "remove_banner":
                await bot.user.edit(banner=None)
                await interaction.followup.send(embed=make_embed("Banner Removed", "> Global banner removed.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


class GlobalBrandingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(GlobalBrandingActionSelect())

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=_build_global_branding_embed(), view=GlobalBrandingView())


class ServerBrandingActionSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild):
        me = guild.me
        options = [
            discord.SelectOption(label="Change Nickname", value="nickname", description="Update the bot nickname for this server."),
            discord.SelectOption(label="Change Avatar", value="avatar", description="Upload a server avatar from an image URL."),
            discord.SelectOption(label="Change Banner", value="banner", description="Upload a server banner from an image URL."),
            discord.SelectOption(label="Change Bio", value="bio", description="Update the server-specific bot bio."),
        ]
        if me.nick:
            options.append(discord.SelectOption(label="╌ Clear Nickname", value="clear_nickname", description="Revert back to the global bot username."))
        if me.guild_avatar:
            options.append(discord.SelectOption(label="╌ Remove Avatar", value="remove_avatar", description="Revert to the global bot avatar."))
        if getattr(me, "guild_banner", None):
            options.append(discord.SelectOption(label="╌ Remove Banner", value="remove_banner", description="Clear the server-specific banner."))
        super().__init__(placeholder="Choose a server branding action...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
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
                await interaction.followup.send(embed=make_embed("Nickname Cleared", "> Server nickname cleared.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
            elif action == "remove_avatar":
                await interaction.guild.me.edit(avatar=None)
                await interaction.followup.send(embed=make_embed("Avatar Removed", "> Server avatar removed (reverted to global).", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
            elif action == "remove_banner":
                await interaction.guild.me.edit(banner=None)
                await interaction.followup.send(embed=make_embed("Banner Removed", "> Server banner removed.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(embed=make_embed("Failed", f"> Failed: {e}", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


class ServerBrandingView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(ServerBrandingActionSelect(guild))

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=_build_server_branding_embed(interaction.guild), view=ServerBrandingView(interaction.guild))


# ── Commands ──

branding_group = app_commands.Group(name="branding", description="Manage bot profile and server appearance.", default_permissions=discord.Permissions(administrator=True))


@branding_group.command(name="global", description="Edit the bot's global profile.")
@app_commands.check(check_owner)
async def branding_global(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_global_branding_embed(), view=GlobalBrandingView(), ephemeral=True)


@branding_group.command(name="server", description="Edit this server's bot profile.")
@app_commands.check(check_owner)
async def branding_server(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_server_branding_embed(interaction.guild), view=ServerBrandingView(interaction.guild), ephemeral=True)


# ──────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot


async def setup(bot) -> None:
    await bot.add_cog(AdminCog(bot))
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
