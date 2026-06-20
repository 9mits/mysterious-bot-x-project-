"""Event listeners and native AutoMod bridge — split from system.py."""

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
from core.context import abuse_system, bot
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
    make_error_embed,
    get_user_display_name,
    format_user_ref,
    format_user_id_ref,
    get_primary_guild,
    send_punishment_log,
    respond_with_error,
    resolve_member,
    get_valid_duration,
    get_punishment_log_channel_ids,
    prepare_modmail_relay_attachments,
    maybe_send_dm_modmail_panel,
    punish_rogue_mod,
    extract_snowflake_id,
)
from .cases import (
    get_case_label,
    build_punishment_execution_log_embed,
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

async def _find_role_grant_actor(guild: discord.Guild, target_id: int, role: discord.Role):
    """Return the member who granted `role` to `target_id`, matched precisely
    against the audit log rather than blindly trusting the newest entry."""
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
            if not entry.target or entry.target.id != target_id:
                continue
            added_roles = getattr(entry.after, "roles", None) or []
            if role in added_roles:
                return entry.user
    except discord.Forbidden:
        return None
    except Exception as exc:
        logger.warning("Anti-Nuke: failed to read audit log for role grant: %s", exc)
        return None
    return None


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Only react when roles were added
    if len(before.roles) >= len(after.roles):
        return

    added_roles = [r for r in after.roles if r not in before.roles]
    immunity_list = bot.data_manager.config.get("immunity_list", [])

    for role in added_roles:
        if not has_dangerous_perm(role.permissions):
            continue

        actor = await _find_role_grant_actor(after.guild, after.id, role)
        if actor is None:
            continue  # couldn't attribute this grant — leave it for manual review
        if actor.id == bot.user.id:
            continue  # the bot granted it (e.g. its own automation)
        if str(actor.id) in immunity_list:
            continue  # actor is explicitly trusted

        restore_data = {"type": "member_role", "target_id": after.id, "extra_id": role.id}

        # REVERT (remove the dangerous role from the target)
        try:
            await after.remove_roles(role, reason=f"Anti-Nuke: Reverting unauthorized role grant by {actor}")
        except Exception as exc:
            logger.warning("Anti-Nuke: failed to revert role %s on %s: %s", role.id, after.id, exc)

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

        await punish_rogue_mod(after.guild, actor, f"Granted dangerous role **{role.name}** to {after.mention}", embed=embed, restore_data=restore_data)

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

async def on_ready():
    bot.start_time = time.time()
    logger.info(f"[READY] Logged in as {bot.user} (ID: {bot.user.id}). System operational.")




class EventsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        await on_guild_role_update(before, after)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        await on_member_update(before, after)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await on_message(message)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await on_ready()

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await on_app_command_error(interaction, error)

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction) -> None:
        await on_automod_action(execution)

    @commands.Cog.listener()
    async def on_socket_raw_receive(self, message) -> None:
        await on_socket_raw_receive(message)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload) -> None:
        await on_raw_reaction_add(payload)


async def setup(bot) -> None:
    cog = EventsCog(bot)
    await bot.add_cog(cog)
    bot.tree.on_error = on_app_command_error
