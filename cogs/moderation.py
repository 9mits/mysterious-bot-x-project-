"""Punishment execution, ModGroup slash commands, and moderation context menus."""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Union

from core.constants import (
    DEFAULT_RULES,
    SCOPE_MODERATION,
)
from core.services import (
    get_feature_flag,
)
from core.context import abuse_system, bot, tree
from core.utils import iso_to_dt, now_iso, parse_duration_str
from .shared import (
    format_duration,
    format_log_quote,
    format_reason_value,
    make_embed,
    make_empty_state_embed,
    make_error_embed,
    get_user_display_name,
    format_user_ref,
    send_punishment_log,
    respond_with_error,
    is_staff,
    resolve_member,
    get_valid_duration,
    calculate_smart_punishment,
    handle_abuse,
)
from .cases import (
    get_case_label,
    build_punishment_execution_log_embed,
    build_no_history_embed,
    build_active_punishments_embed,
    build_mod_help_embed,
)
from .history import HistoryView
from .case_panel import CasePanelView, FirstConfirmClear, ActiveView
from .roles import AppealView, build_punish_embed

async def execute_punishment(interaction, target, moderator, reason, minutes, note, user_msg, is_escalated, origin_message=None, punishment_type="auto", public=False):
    uid = str(target.id)
    history = bot.data_manager.punishments.get(uid, [])
    guild = interaction.guild
    member_target = target if isinstance(target, discord.Member) else await resolve_member(guild, target.id)
    
    # Determine Type
    if punishment_type == "auto":
        if minutes == -1: punishment_type = "ban"
        elif minutes == 0: punishment_type = "warn"
        else: punishment_type = "timeout"

    is_ban = (punishment_type == "ban")
    is_kick = (punishment_type == "kick")
    is_softban = (punishment_type == "softban")
    is_warning = (punishment_type == "warn")

    # Anti-Abuse: Hierarchy Check (moderator must outrank target; guild owner always bypasses)
    if member_target and member_target.id != guild.owner_id and member_target != moderator and moderator.id != guild.owner_id:
        if member_target.top_role >= moderator.top_role:
            blocked_embed = make_embed("Anti-Abuse Blocked", "> You cannot punish a user with equal or higher role hierarchy.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild)
            if interaction.response.is_done():
                await interaction.followup.send(embed=blocked_embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=blocked_embed, ephemeral=True)
            return

    # Anti-Abuse: Rate Limit
    if abuse_system.check_rate_limit(moderator.id, bot.data_manager.config):
        await handle_abuse(interaction, moderator)
        return

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        if is_kick:
            if not member_target:
                await interaction.followup.send(embed=make_embed("Cannot Kick", "> User is not in the server, cannot kick.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
                return
            await guild.kick(member_target, reason=f"{reason} (By {moderator})")
        elif is_softban:
            # Softban: Ban (Delete 1 day of messages) -> Unban
            await guild.ban(target, reason=f"{reason} (By {moderator})", delete_message_days=1)
            await guild.unban(discord.Object(id=target.id), reason=f"Softban cleanup (By {moderator})")
        elif is_ban:
            # Handles both Perm (-1) and Temp (>0) bans
            await guild.ban(target, reason=f"{reason} (By {moderator})", delete_message_days=0)
        elif punishment_type == "timeout":
            if not member_target:
                await interaction.followup.send(embed=make_embed("Cannot Timeout", "> User is not in the server, cannot timeout.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
                return
            duration = get_valid_duration(minutes)
            await member_target.timeout(duration, reason=f"{reason} (By {moderator})")
    except discord.Forbidden:
        await interaction.followup.send(embed=make_embed("Permission Error", "> I cannot punish this user (Permission Error).", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(embed=make_embed("Error", f"> Error: {e}", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return

    timestamp_iso = now_iso()

    # DM User
    try:
        if is_kick:
            action_verb = "Kicked"
        elif is_softban:
            action_verb = "Softbanned (Kicked + Messages Purged)"
        elif is_ban:
            action_verb = "Banned" if minutes == -1 else f"Banned for {format_duration(minutes)}"
        else:
            action_verb = "Warned" if is_warning else "Timed Out"

        dm_embed = make_embed(
            "Moderation Action Issued",
            f"> You have been **{action_verb}** in **{interaction.guild.name}**.",
            kind="danger",
            scope=SCOPE_MODERATION,
            guild=interaction.guild,
            thumbnail=interaction.guild.icon.url if interaction.guild.icon else None,
        )
        dm_embed.add_field(name="Reason", value=format_reason_value(reason, limit=1000), inline=False)
        if user_msg:
            dm_embed.add_field(name="Moderator Message", value=format_log_quote(user_msg, limit=1024), inline=False)
        
        if punishment_type == "timeout":
            dm_embed.add_field(name="Duration", value=format_duration(minutes), inline=True)
            unmute_dt = discord.utils.utcnow() + get_valid_duration(minutes if minutes > 0 else 0)
            dm_embed.add_field(name="Expires", value=discord.utils.format_dt(unmute_dt, "R"), inline=True)
        elif is_ban and minutes == -1:
            dm_embed.add_field(name="Duration", value="Ban", inline=True)
        
        if interaction.guild.icon:
            dm_embed.set_thumbnail(url=interaction.guild.icon.url)
        
        view = AppealView(interaction.guild.id, target.id, moderator.id, minutes, timestamp_iso, reason)
        await target.send(embed=dm_embed, view=view)
    except discord.Forbidden:
        pass

    # Log punishment
    record = {
        "reason": reason,
        "moderator": moderator.id,
        "duration_minutes": minutes,
        "timestamp": timestamp_iso,
        "escalated": is_escalated,
        "note": note,
        "user_msg": user_msg,
        "target_name": get_user_display_name(target),
        "type": punishment_type,
        "active": is_ban
    }
    record = await bot.data_manager.add_punishment(uid, record, persist=False)
    case_label = get_case_label(record, len(history) + 1)
    
    # Update Stats
    bot.data_manager.config["stats"]["total_issued"] = bot.data_manager.config["stats"].get("total_issued", 0) + 1
    bot.data_manager.mark_config_dirty()
    await bot.data_manager.save_all()

    if is_kick:
        status = "Kicked"
    elif is_softban:
        status = "Softbanned"
    elif is_ban:
        status = "Banned"
    else:
        status = "Warning Logged" if is_warning else ("Escalated (Recidivism)" if is_escalated else "Standard")
        
    if reason == "Custom Punishment":
        status = "Custom"
        if is_ban: status = "Custom (Ban)"

    log_embed = build_punishment_execution_log_embed(
        guild=interaction.guild,
        case_label=case_label,
        actor=format_user_ref(moderator),
        target=format_user_ref(target),
        record=record,
        thumbnail=target.display_avatar.url,
    )

    # Response Embed (Private)
    response_embed = make_embed(
        "Action Successful",
        f"> **{target.mention}** has been punished successfully.",
        kind="success",
        scope=SCOPE_MODERATION,
        guild=interaction.guild,
        thumbnail=target.display_avatar.url,
    )
    response_embed.add_field(name="Case", value=case_label, inline=True)
    response_embed.add_field(name="Reason", value=format_reason_value(reason, limit=500), inline=False)
    response_embed.add_field(name="Type", value=status, inline=True)
    if not is_warning:
        response_embed.add_field(name="Duration", value=format_duration(minutes), inline=True)
    
    if interaction.message:
        try:
            await interaction.message.edit(content=None, embed=response_embed, view=None)
        except Exception:
            await interaction.followup.send(embed=response_embed, ephemeral=True)
    else:
        await interaction.followup.send(embed=response_embed, ephemeral=True)

    try:
        await interaction.delete_original_response()
    except Exception:
        pass

    if public:
        pub_embed = make_embed(
            f"{case_label} Issued",
            f"> **{target.mention}** has been punished.",
            kind="danger",
            scope=SCOPE_MODERATION,
            guild=interaction.guild,
        )
        pub_embed.add_field(name="Reason", value=format_reason_value(reason, limit=200), inline=False)
        pub_embed.add_field(name="Type", value=status, inline=True)
        if not is_warning and minutes != 0:
             pub_embed.add_field(name="Duration", value=format_duration(minutes), inline=True)
        pub_embed.add_field(name="Handled By", value=moderator.display_name, inline=True)
        try:
            await interaction.channel.send(embed=pub_embed)
        except Exception:
            pass

    await send_punishment_log(interaction.guild, log_embed)
    
    if origin_message:
        try:
            await origin_message.edit(embed=build_punish_embed(target))
        except Exception:
            pass

# ----------------- Embeds -----------------

class PunishDetailsModal(discord.ui.Modal):
    def __init__(self, target, moderator, reason, rules, origin_message=None, public=False, reaction_count=None):
        super().__init__(title=f"Punish: {target.display_name}")
        self.target = target
        self.moderator = moderator
        self.reason = reason
        self.rules = rules
        self.origin_message = origin_message
        self.public = public
        self.reaction_count = reaction_count

    mod_note = discord.ui.TextInput(
        label="Moderator Note (Internal)",
        style=discord.TextStyle.paragraph,
        placeholder="Visible only to staff. Required.",
        required=True
    )

    mod_message = discord.ui.TextInput(
        label="Message to User (Optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Visible to the user. Explain why they are being punished.",
        required=False
    )
    
    duration_override = discord.ui.TextInput(
        label="Duration/Type Override (Optional)",
        placeholder="e.g. 2d, 1w, ban, warn, kick. Leave blank for auto.",
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        reason = self.reason
        rules = self.rules
        note = self.mod_note.value
        user_msg = self.mod_message.value
        override = self.duration_override.value.strip().lower()
        
        minutes = 0
        is_escalated = False
        punishment_type = "auto"

        if override:
            if override == "kick":
                punishment_type = "kick"
            elif override == "softban":
                punishment_type = "softban"
            else:
                minutes = parse_duration_str(override)
                if minutes == -1: punishment_type = "ban"
                elif minutes == 0: punishment_type = "warn"
        else:
            # Use advanced calculation
            minutes, is_escalated, tier_info = calculate_smart_punishment(str(self.target.id), reason, rules, bot.data_manager.punishments.get(str(self.target.id), []))
            
            # Append tier info to internal note for context
            if note: note = f"[{tier_info}] {note}"
            else: note = f"[{tier_info}]"
        
        if self.reaction_count:
            action_verb = "Punish"
            if punishment_type == "ban": action_verb = "Ban"
            elif punishment_type == "kick": action_verb = "Kick"
            elif punishment_type == "timeout": action_verb = "Timeout"
            elif punishment_type == "warn": action_verb = "Warn"
            elif punishment_type == "softban": action_verb = "Softban"

            embed = make_embed(
                "Public Execution Started",
                f"React to this message to **{action_verb}** {self.target.mention}.\n\nThe execution will happen when **{self.reaction_count}** reactions are reached.",
                kind="danger",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
                thumbnail=self.target.display_avatar.url,
            )
            embed.add_field(name="Reason", value=format_reason_value(reason, limit=200), inline=False)
            if minutes > 0:
                embed.add_field(name="Duration", value=format_duration(minutes), inline=True)
            
            msg = await interaction.followup.send(embed=embed, ephemeral=False)
            await msg.add_reaction("✅")
            
            bot.active_executions[msg.id] = {
                "target_id": self.target.id,
                "count": self.reaction_count,
                "reason": reason,
                "note": note,
                "user_msg": user_msg,
                "moderator_id": self.moderator.id,
                "duration": minutes,
                "type": punishment_type,
                "escalated": is_escalated
            }
            return

        await execute_punishment(interaction, self.target, self.moderator, reason, minutes, note, user_msg, is_escalated, self.origin_message, punishment_type=punishment_type, public=self.public)

class CustomPunishDetailsModal(discord.ui.Modal):
    def __init__(self, target, moderator, p_type, origin_message, public=False, reaction_count=None):
        super().__init__(title=f"Configure {p_type.replace('_', ' ').title()}")
        self.target = target
        self.moderator = moderator
        self.p_type = p_type
        self.origin_message = origin_message
        self.public = public
        self.reaction_count = reaction_count
        
        self.custom_reason = discord.ui.TextInput(
            label="Reason",
            placeholder="e.g. Violation of rules",
            max_length=100,
            required=True
        )
        self.add_item(self.custom_reason)
        
        self.duration_str = None
        if p_type in ["timeout", "ban_temp"]:
            self.duration_str = discord.ui.TextInput(
                label="Duration",
                placeholder="e.g. 1h, 30m, 1d",
                max_length=20,
                required=True
            )
            self.add_item(self.duration_str)
            
        self.mod_note = discord.ui.TextInput(
            label="Moderator Note (Internal)",
            style=discord.TextStyle.paragraph,
            placeholder="Visible only to staff.",
            required=True
        )
        self.add_item(self.mod_note)
        
        self.mod_message = discord.ui.TextInput(
            label="Message to User (Optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Visible to the user.",
            required=False
        )
        self.add_item(self.mod_message)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        minutes = 0
        final_type = self.p_type
        
        if self.p_type == "ban_perm":
            final_type = "ban"
            minutes = -1
        elif self.p_type == "ban_temp":
            final_type = "ban"
            if self.duration_str:
                minutes = parse_duration_str(self.duration_str.value)
                if minutes <= 0:
                    await interaction.followup.send(embed=make_embed("Invalid Duration", "> Invalid duration for temporary ban.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
                    return
        elif self.p_type == "timeout":
            final_type = "timeout"
            if self.duration_str:
                minutes = parse_duration_str(self.duration_str.value)
                if minutes <= 0:
                    await interaction.followup.send(embed=make_embed("Invalid Duration", "> Invalid duration for timeout.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
                    return
        elif self.p_type == "kick":
            final_type = "kick"
            minutes = 0
        elif self.p_type == "softban":
            final_type = "softban"
            minutes = 0
        elif self.p_type == "warn":
            final_type = "warn"
            minutes = 0

        if self.reaction_count:
            action_verb = "Punish"
            if final_type == "ban": action_verb = "Ban"
            elif final_type == "kick": action_verb = "Kick"
            elif final_type == "timeout": action_verb = "Timeout"
            elif final_type == "warn": action_verb = "Warn"
            elif final_type == "softban": action_verb = "Softban"

            embed = make_embed(
                "Public Execution Started",
                f"React to this message to **{action_verb}** {self.target.mention}.\n\nThe execution will happen when **{self.reaction_count}** reactions are reached.",
                kind="danger",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
                thumbnail=self.target.display_avatar.url,
            )
            embed.add_field(name="Reason", value=format_reason_value(self.custom_reason.value, limit=200), inline=False)
            if minutes > 0:
                embed.add_field(name="Duration", value=format_duration(minutes), inline=True)
            
            msg = await interaction.followup.send(embed=embed, ephemeral=False)
            await msg.add_reaction("✅")
            
            bot.active_executions[msg.id] = {
                "target_id": self.target.id,
                "count": self.reaction_count,
                "reason": self.custom_reason.value,
                "note": self.mod_note.value,
                "user_msg": self.mod_message.value,
                "moderator_id": self.moderator.id,
                "duration": minutes,
                "type": final_type,
                "escalated": False
            }
            return

        await execute_punishment(
            interaction, 
            self.target, 
            self.moderator, 
            self.custom_reason.value, 
            minutes, 
            self.mod_note.value, 
            self.mod_message.value, 
            False, # Custom punishments don't follow auto-escalation logic
            self.origin_message,
            punishment_type=final_type,
            public=self.public
        )

class CustomTypeSelect(discord.ui.Select):
    def __init__(self, target, moderator, origin_message, public=False, reaction_count=None):
        self.target = target
        self.moderator = moderator
        self.origin_message = origin_message
        self.public = public
        self.reaction_count = reaction_count
        options = [
            discord.SelectOption(label="Timeout", value="timeout", description="Mute user for a duration"),
            discord.SelectOption(label="Kick", value="kick", description="Remove user from server"),
            discord.SelectOption(label="Softban", value="softban", description="Kick + Delete Messages"),
            discord.SelectOption(label="Ban (Temporary)", value="ban_temp", description="Ban for a duration"),
            discord.SelectOption(label="Ban (Permanent)", value="ban_perm", description="Ban indefinitely"),
            discord.SelectOption(label="Warning", value="warn", description="Log a warning")
        ]
        super().__init__(placeholder="Select punishment type...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        p_type = self.values[0]
        await interaction.response.send_modal(CustomPunishDetailsModal(self.target, self.moderator, p_type, self.origin_message, public=self.public, reaction_count=self.reaction_count))

class CustomTypeView(discord.ui.View):
    def __init__(self, target, moderator, origin_message, public=False, reaction_count=None):
        super().__init__(timeout=60)
        self.add_item(CustomTypeSelect(target, moderator, origin_message, public=public, reaction_count=reaction_count))

class PunishSelect(discord.ui.Select):
    def __init__(self, target: discord.User, moderator: discord.Member, public=False, reaction_count=None):
        self.target = target
        self.moderator = moderator
        self.public = public
        self.reaction_count = reaction_count
        rules_config = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        options = []
        for reason, rules in rules_config.items():
            base_str = format_duration(rules['base'])
            esc_str = format_duration(rules['escalated'])
            if rules['base'] == 0:
                desc = f"1st: Warning • Repeat: {esc_str}"
            else:
                desc = f"Base: {base_str} • Repeat: {esc_str}"
            options.append(discord.SelectOption(label=reason, description=desc))
        options.append(discord.SelectOption(label="Custom Punishment", value="custom", description="Define custom reason and duration"))
        super().__init__(placeholder="Select a punishment reason...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "custom":
            await interaction.response.send_message(embed=make_embed("Custom Punishment", "> Select the type of custom punishment below.", kind="info", scope=SCOPE_MODERATION, guild=interaction.guild), view=CustomTypeView(self.target, self.moderator, interaction.message, public=self.public, reaction_count=self.reaction_count), ephemeral=True)
            return
        reason = self.values[0]
        rules_config = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        rules = rules_config.get(reason)
        if not rules:
            return
        await interaction.response.send_modal(PunishDetailsModal(self.target, self.moderator, reason, rules, interaction.message, public=self.public, reaction_count=self.reaction_count))


class PunishView(discord.ui.View):
    def __init__(self, target, moderator, public=False, reaction_count=None):
        super().__init__(timeout=60)
        self.target = target
        self.moderator = moderator
        self.add_item(PunishSelect(target, moderator, public=public, reaction_count=reaction_count))

    @discord.ui.button(label="Clear History", style=discord.ButtonStyle.danger, row=1)
    async def clear_history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=make_embed("Confirm Clear", "> Are you sure you want to clear this user's punishment history?", kind="warning", scope=SCOPE_MODERATION, guild=interaction.guild),
            view=FirstConfirmClear(self.target, self.moderator, interaction.message),
            ephemeral=True
        )

    @discord.ui.button(label="View History", style=discord.ButtonStyle.secondary, row=1)
    async def view_history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = self.target if isinstance(self.target, discord.Member) else await resolve_member(interaction.guild, self.target.id)
        if not member:
            await interaction.response.send_message(embed=make_embed("User Left Server", "> This user is no longer in the server, so the interactive history panel is unavailable.", kind="info", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
            return

        uid = str(member.id)
        history_data = bot.data_manager.punishments.get(uid, [])

        if not history_data:
            await interaction.response.send_message(embed=make_embed("Clean Record", f"> **{member.display_name}** has a clean record (No history found).", kind="success", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
            return

        view = HistoryView(member)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


class RevokeUndoView(discord.ui.View):
    def __init__(self, target_id: int, record: dict, actor_id: int):
        super().__init__(timeout=None)
        self.target_id = target_id
        self.record = record
        self.actor_id = actor_id

    @discord.ui.button(label="Revoke Undo", style=discord.ButtonStyle.danger)
    async def revoke_undo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_staff(interaction):
             await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
             return

        await interaction.response.defer()
        
        # Restore record
        uid = str(self.target_id)
        await bot.data_manager.add_punishment(uid, self.record)
        
        # Re-apply physical punishment
        guild = interaction.guild
        target = guild.get_member(self.target_id)
        if not target:
            try: target = await bot.fetch_user(self.target_id)
            except Exception: pass
            
        action_taken = "History Restored"
        p_type = self.record.get("type")
        dur = self.record.get("duration_minutes", 0)
        
        try:
            if p_type == "ban":
                await guild.ban(discord.Object(id=self.target_id), reason="Undo Revoked: Restoring Punishment")
                action_taken += " & User Banned"
            elif p_type == "timeout" and isinstance(target, discord.Member):
                if dur > 0:
                    await target.timeout(get_valid_duration(dur), reason="Undo Revoked: Restoring Punishment")
                    action_taken += " & User Timed Out"
        except Exception as e:
            action_taken += f" (Physical action failed: {e})"

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.orange()
        embed.add_field(name="Update", value=f"> **Undo Revoked** by {interaction.user.mention}\n> {action_taken}", inline=False)
        
        button.disabled = True
        button.label = "Undo Revoked"
        await interaction.edit_original_response(embed=embed, view=self)

async def show_punish_menu(interaction: discord.Interaction, user: discord.User, public=False, reaction_count=None):
    await interaction.response.defer(ephemeral=True)
    embed = build_punish_embed(user)
    view = PunishView(user, interaction.user, public=public, reaction_count=reaction_count)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

async def show_history_menu(
    interaction: discord.Interaction,
    user: discord.Member,
    *,
    mode: str = "history",
    selected_case_id: Optional[int] = None,
    initial_undo_reason: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    uid = str(user.id)
    history_data = bot.data_manager.punishments.get(uid, [])
    if not history_data:
        await interaction.followup.send(embed=build_no_history_embed(user, interaction.guild), ephemeral=True)
        return
    view = HistoryView(
        user,
        mode=mode,
        selected_case_id=selected_case_id,
        initial_undo_reason=initial_undo_reason,
    )
    message = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True, wait=True)
    view.message = message


async def show_case_panel(
    interaction: discord.Interaction,
    *,
    case_id: Optional[int] = None,
    user: Optional[discord.Member] = None,
):
    if not get_feature_flag(bot.data_manager.config, "advanced_case_panel", True):
        await respond_with_error(interaction, "The case panel is currently turned off in the feature settings.", scope=SCOPE_MODERATION)
        return

    await interaction.response.defer(ephemeral=True)

    target_user_id: Optional[str] = None
    target_user: Optional[Union[discord.Member, discord.User]] = user
    case_ids: List[int] = []

    if case_id:
        target_user_id, record = bot.data_manager.get_case(case_id)
        if not record or not target_user_id:
            await interaction.followup.send(
                embed=make_empty_state_embed(
                    "Case Not Found",
                    f"> No case with ID `{case_id}` was found.",
                    scope=SCOPE_MODERATION,
                    guild=interaction.guild,
                ),
                ephemeral=True,
            )
            return
        case_ids = [case_id]
        if not target_user:
            target_user = interaction.guild.get_member(int(target_user_id))

    elif user:
        target_user_id = str(user.id)
        case_ids = [record.get("case_id") for record in bot.data_manager.get_user_cases(user.id) if record.get("case_id")]
        if not case_ids:
            await interaction.followup.send(
                embed=make_empty_state_embed(
                    "No Cases Found",
                    f"> **{user.display_name}** has no recorded cases to manage.",
                    scope=SCOPE_MODERATION,
                    guild=interaction.guild,
                    thumbnail=user.display_avatar.url,
                ),
                ephemeral=True,
            )
            return
    else:
        await interaction.followup.send(
            embed=make_error_embed(
                "Case Panel Requires Context",
                "> Choose a `case_id` or a `user` so the bot knows which case to open.",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
            ),
            ephemeral=True,
        )
        return

    view = CasePanelView(target_user_id, case_ids, target_user=target_user)
    message = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True, wait=True)
    view.message = message

def _staff_check(interaction: discord.Interaction) -> bool:
    return is_staff(interaction)


async def _resolve_selected_member(interaction: discord.Interaction, selected_user: Union[discord.Member, discord.User]) -> Optional[discord.Member]:
    if isinstance(selected_user, discord.Member):
        return selected_user
    return await resolve_member(interaction.guild, selected_user.id)


class CaseIdModal(discord.ui.Modal, title="Open Case by ID"):
    case_id = discord.ui.TextInput(label="Case ID", placeholder="123", max_length=12)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            selected_case_id = int(self.case_id.value.strip())
        except ValueError:
            await respond_with_error(interaction, "Enter a valid numeric case ID.", scope=SCOPE_MODERATION)
            return
        await show_case_panel(interaction, case_id=selected_case_id)


class ModerationTargetSelect(discord.ui.UserSelect):
    def __init__(self, parent: "ModerationTargetPickerView"):
        super().__init__(
            placeholder="Choose a member...",
            min_values=1,
            max_values=1,
            row=0,
        )
        self._target_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._target_view.handle_user(interaction, self.values[0])


class ModerationTargetPickerView(discord.ui.View):
    def __init__(self, *, requester_id: int, action: str, public: bool = False, initial_undo_reason: Optional[str] = None):
        super().__init__(timeout=180)
        self.requester_id = requester_id
        self.action = action
        self.public = public
        self.initial_undo_reason = initial_undo_reason
        self.add_item(ModerationTargetSelect(self))
        if action == "case":
            self.add_item(CaseIdButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(embed=make_embed("Access Denied", "> This picker belongs to another moderator.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return False

    async def handle_user(self, interaction: discord.Interaction, selected_user: Union[discord.Member, discord.User]) -> None:
        if not is_staff(interaction):
            await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this panel.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
            return

        if self.action == "punish":
            await show_punish_menu(interaction, selected_user, public=self.public)
            return

        member = await _resolve_selected_member(interaction, selected_user)
        if member is None:
            await respond_with_error(interaction, "That user is not currently in this server.", scope=SCOPE_MODERATION)
            return

        if self.action == "history":
            await show_history_menu(interaction, member)
            return
        if self.action == "undo":
            await show_history_menu(interaction, member, mode="undo", initial_undo_reason=self.initial_undo_reason)
            return
        if self.action == "case":
            await show_case_panel(interaction, user=member)


class CaseIdButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Open by Case ID", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CaseIdModal())


async def send_target_picker(
    interaction: discord.Interaction,
    *,
    action: str,
    title: str,
    description: str,
    public: bool = False,
    initial_undo_reason: Optional[str] = None,
) -> None:
    embed = make_embed(
        title,
        description,
        kind="info",
        scope=SCOPE_MODERATION,
        guild=interaction.guild,
    )
    await interaction.response.send_message(
        embed=embed,
        view=ModerationTargetPickerView(
            requester_id=interaction.user.id,
            action=action,
            public=public,
            initial_undo_reason=initial_undo_reason,
        ),
        ephemeral=True,
    )


@tree.command(name="punish", description="Open the moderation action panel.")
@app_commands.describe(public="Send the result to this channel.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.check(_staff_check)
async def punish(interaction: discord.Interaction, user: Optional[discord.User] = None, public: bool = False):
    if user is None:
        await send_target_picker(
            interaction,
            action="punish",
            title="Choose a Target",
            description="> Select a member to open the moderation action panel.",
            public=public,
        )
        return
    await show_punish_menu(interaction, user, public=public)


@tree.command(name="history", description="View a user's moderation history.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.check(_staff_check)
async def history(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    if user is None:
        await send_target_picker(
            interaction,
            action="history",
            title="Choose a Member",
            description="> Select a member to view their moderation history.",
        )
        return
    await show_history_menu(interaction, user)


@tree.command(name="active", description="View active bans and timeouts.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.check(_staff_check)
async def active(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    now = discord.utils.utcnow()
    active_list = []
    for uid, records in bot.data_manager.punishments.items():
        for i, rec in enumerate(records):
            dur = rec.get("duration_minutes", 0)
            p_type = rec.get("type", "timeout")
            if p_type == "ban" and not rec.get("active", True):
                continue
            if dur == 0: continue
            ts_str = rec.get("timestamp")
            ts = iso_to_dt(ts_str)
            if not ts: continue

            if dur == -1:
                expiry = datetime.max.replace(tzinfo=timezone.utc)
            elif dur > 0:
                expiry = ts + timedelta(minutes=dur)

            if dur == -1 or expiry > now:
                member = interaction.guild.get_member(int(uid))
                name = member.display_name if member else uid
                active_list.append((uid, rec, expiry, i+1, name))
    if not active_list:
        await interaction.followup.send(embed=make_embed("No Active Punishments", "> No active punishments found.", kind="info", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return
    active_list.sort(key=lambda x: x[2])
    embed = build_active_punishments_embed(interaction.guild, active_list, now)
    view = ActiveView(active_list)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@tree.command(name="undo", description="Reverse a logged moderation action.")
@app_commands.describe(reason="Reason to prefill in the undo panel.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.check(_staff_check)
async def undo(interaction: discord.Interaction, user: Optional[discord.Member] = None, reason: Optional[str] = None):
    if user is None:
        await send_target_picker(
            interaction,
            action="undo",
            title="Choose a Member",
            description="> Select a member to open the undo panel.",
            initial_undo_reason=reason,
        )
        return
    await show_history_menu(interaction, user, mode="undo", initial_undo_reason=reason)


@tree.command(name="purge", description="Delete recent messages with optional filters.")
@app_commands.describe(amount="Messages to scan or delete. Max 999.", user="Only delete messages from this user.", keyword="Only delete messages containing this text.")
@app_commands.default_permissions(manage_messages=True)
@app_commands.check(_staff_check)
async def purge(interaction: discord.Interaction, amount: int, user: discord.Member = None, keyword: str = None):
    if amount < 1 or amount > 999:
        await interaction.response.send_message(embed=make_embed("Invalid Amount", "> Amount must be between 1 and 999.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if not user and not keyword:
        try:
            deleted = await interaction.channel.purge(limit=amount)
            await interaction.followup.send(embed=make_embed("Messages Cleared", f"> Cleared **{len(deleted)}** messages.", kind="success", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)

            log_embed = make_embed(
                "Messages Purged",
                "> A bulk message purge was executed in a channel.",
                kind="warning",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
            )
            log_embed.add_field(name="Actor", value=format_user_ref(interaction.user), inline=True)
            log_embed.add_field(name="Channel", value=f"{interaction.channel.mention} (`{interaction.channel.id}`)", inline=True)
            log_embed.add_field(name="Amount", value=str(len(deleted)), inline=True)
            await send_punishment_log(interaction.guild, log_embed)
        except discord.HTTPException as e:
            await interaction.followup.send(embed=make_embed("Failed to Purge", f"> Failed to purge: {e}", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return

    to_delete = []
    manual_delete = []
    deleted_count = 0

    now = discord.utils.utcnow()
    two_weeks_ago = now - timedelta(days=14)

    async for message in interaction.channel.history(limit=10000):
        if deleted_count + len(to_delete) + len(manual_delete) >= amount:
            break

        if user and message.author.id != user.id:
            continue
        if keyword and keyword.lower() not in message.content.lower():
            continue

        if message.created_at > two_weeks_ago:
            to_delete.append(message)
            if len(to_delete) >= 100:
                try:
                    await interaction.channel.delete_messages(to_delete)
                    deleted_count += len(to_delete)
                    to_delete = []
                except Exception: pass
        else:
            manual_delete.append(message)

    if to_delete:
        try:
            await interaction.channel.delete_messages(to_delete)
            deleted_count += len(to_delete)
        except Exception: pass

    for m in manual_delete:
        try:
            await m.delete()
            deleted_count += 1
            await asyncio.sleep(1.2)
        except Exception: pass

    if deleted_count == 0:
        await interaction.followup.send(embed=make_embed("No Messages Found", "> No matching messages found to purge.", kind="info", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return

    target_str = user.mention if user else "Anyone"
    await interaction.followup.send(embed=make_embed("Messages Cleared", f"> Cleared **{deleted_count}** messages from {target_str}.", kind="success", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)

    log_embed = make_embed(
        "Filtered Purge",
        "> A targeted purge removed messages using user or keyword filters.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=interaction.guild,
    )
    log_embed.add_field(name="Actor", value=format_user_ref(interaction.user), inline=True)
    log_embed.add_field(name="Target", value=f"{target_str}", inline=True)
    log_embed.add_field(name="Channel", value=f"{interaction.channel.mention} (`{interaction.channel.id}`)", inline=True)
    log_embed.add_field(name="Amount", value=str(deleted_count), inline=True)
    if keyword: log_embed.add_field(name="Keyword", value=keyword, inline=True)
    await send_punishment_log(interaction.guild, log_embed)


@tree.command(name="lock", description="Lock the current channel.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.check(_staff_check)
async def lock(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    default_role = interaction.guild.default_role
    overwrite = channel.overwrites_for(default_role)
    overwrite.send_messages = False
    try:
        await channel.set_permissions(default_role, overwrite=overwrite, reason=f"Locked by {interaction.user}")
        public_embed = make_embed(
            "Channel Locked",
            "> This channel is temporarily locked by the moderation team.",
            kind="danger",
            scope=SCOPE_MODERATION,
            guild=interaction.guild,
        )
        msg = await channel.send(embed=public_embed)
        if "locked_channels" not in bot.data_manager.config: bot.data_manager.config["locked_channels"] = {}
        bot.data_manager.config["locked_channels"][str(channel.id)] = msg.id
        await bot.data_manager.save_config()
        await interaction.followup.send(embed=make_embed("Channel Locked", "> Channel has been locked successfully.", kind="success", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(embed=make_embed("Error", f"> Error: {e}", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)


@tree.command(name="unlock", description="Unlock the current channel.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.check(_staff_check)
async def unlock(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    default_role = interaction.guild.default_role
    overwrite = channel.overwrites_for(default_role)
    overwrite.send_messages = None
    try:
        await channel.set_permissions(default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
        cid = str(channel.id)
        if "locked_channels" in bot.data_manager.config:
            if cid in bot.data_manager.config["locked_channels"]:
                try:
                    msg = await channel.fetch_message(bot.data_manager.config["locked_channels"][cid])
                    await msg.delete()
                except Exception: pass
                del bot.data_manager.config["locked_channels"][cid]
                await bot.data_manager.save_config()
        await interaction.followup.send(embed=make_embed("Channel Unlocked", "> Channel has been unlocked successfully.", kind="success", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(embed=make_embed("Error", f"> Error: {e}", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)


@tree.command(name="mod-guide", description="View the moderation command guide.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.check(_staff_check)
async def mod_help(interaction: discord.Interaction):
    embed = build_mod_help_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="case", description="Open a moderation case.")
@app_commands.describe(case_id="Case ID to open.", user="User whose latest case should open.")
@app_commands.check(_staff_check)
async def case(interaction: discord.Interaction, case_id: Optional[app_commands.Range[int, 1, 999999]] = None, user: Optional[discord.Member] = None):
    if case_id is None and user is None:
        await send_target_picker(
            interaction,
            action="case",
            title="Open a Case",
            description="> Select a member to open their latest case, or open a specific case by ID.",
        )
        return
    await show_case_panel(interaction, case_id=case_id, user=user)


@tree.context_menu(name="Punish")
@app_commands.default_permissions(moderate_members=True)
async def punish_context(interaction: discord.Interaction, user: discord.User):
    if not is_staff(interaction):
        await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this command.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return
    await show_punish_menu(interaction, user)


@tree.context_menu(name="Moderation History")
@app_commands.default_permissions(moderate_members=True)
async def history_context(interaction: discord.Interaction, user: discord.Member):
    if not is_staff(interaction):
        await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have permission to use this command.", kind="error", scope=SCOPE_MODERATION, guild=interaction.guild), ephemeral=True)
        return
    await show_history_menu(interaction, user)



class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
    bot.tree.add_command(punish)
    bot.tree.add_command(history)
    bot.tree.add_command(active)
    bot.tree.add_command(undo)
    bot.tree.add_command(purge)
    bot.tree.add_command(lock)
    bot.tree.add_command(unlock)
    bot.tree.add_command(mod_help)
    bot.tree.add_command(case)
    bot.tree.add_command(punish_context)
    bot.tree.add_command(history_context)
