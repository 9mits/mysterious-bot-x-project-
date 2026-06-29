"""Case management: embed builders, interactive views, undo/appeal flows."""

import discord
from discord.ext import commands
import copy
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Union, Tuple, Any
from collections import Counter
import re

from core.constants import (
    FEATURE_FLAG_LABELS,
    SCOPE_MODERATION,
)
from core.context import bot
from core.utils import iso_to_dt, now_iso
from .shared import (
    UNDO_REASON_PRESETS,
    UNDO_REASON_PRESET_MAP,
    truncate_text,
    format_duration,
    format_log_quote,
    format_plain_log_block,
    format_reason_value,
    format_log_notes,
    make_action_log_embed,
    make_embed,
    join_lines,
    format_user_ref,
    format_user_id_ref,
    resolve_member,
)

def get_case_id(record: dict) -> Optional[int]:
    case_id = record.get("case_id")
    if isinstance(case_id, int) and case_id > 0:
        return case_id
    return None


def get_case_label(record: dict, fallback: Optional[int] = None) -> str:
    case_id = get_case_id(record)
    if case_id is not None:
        return f"Case #{case_id}"
    if fallback is not None:
        return f"Case #{fallback}"
    return "Case"


def get_record_expiry(record: dict) -> Optional[datetime]:
    duration = record.get("duration_minutes", 0)
    if duration in (0, None):
        return None
    if duration == -1:
        return None
    issued_at = iso_to_dt(record.get("timestamp"))
    if not issued_at:
        return None
    return issued_at + timedelta(minutes=duration)


def format_case_status(record: dict) -> str:
    status = str(record.get("status", "open")).replace("_", " ").title()
    resolution = str(record.get("resolution_state", "pending")).replace("_", " ").title()
    return f"{status} • {resolution}"


def get_feature_flag_name(key: str) -> str:
    return FEATURE_FLAG_LABELS.get(key, key.replace("_", " ").title())


def is_record_active(record: dict, now: Optional[datetime] = None) -> bool:
    now = now or discord.utils.utcnow()
    punishment_type = record.get("type")
    duration = record.get("duration_minutes", 0)

    if punishment_type == "ban":
        if duration == -1:
            return record.get("active", True)
        expiry = get_record_expiry(record)
        return bool(record.get("active", True) and expiry and expiry > now)

    if punishment_type == "timeout" and duration > 0:
        expiry = get_record_expiry(record)
        return bool(expiry and expiry > now)

    return False


def describe_punishment_record(record: dict) -> str:
    punishment_type = record.get("type", "warn")
    duration = record.get("duration_minutes", 0)

    if punishment_type == "ban":
        return "Permanent Ban" if duration == -1 else f"Tempban • {format_duration(duration)}"
    if punishment_type == "timeout":
        return f"Timeout • {format_duration(duration)}"
    if punishment_type == "kick":
        return "Kick"
    if punishment_type == "softban":
        return "Softban"
    return "Warning"


def get_punishment_duration_and_expiry(record: dict) -> Tuple[Optional[str], Optional[str]]:
    punishment_type = str(record.get("type", "warn") or "warn").lower()
    duration = int(record.get("duration_minutes", 0) or 0)
    expires_at = get_record_expiry(record)

    if punishment_type == "timeout" and duration > 0:
        return format_duration(duration), discord.utils.format_dt(expires_at, "F") if expires_at else None
    if punishment_type == "ban":
        if duration == -1:
            return "Ban", "Never"
        if duration > 0:
            return format_duration(duration), discord.utils.format_dt(expires_at, "F") if expires_at else None
        return "Ban", None
    if punishment_type == "kick":
        return "Kick", None
    if punishment_type == "softban":
        return "Softban", None
    return None, None


def get_undo_reason_details(preset_value: Optional[str], custom_reason: Optional[str] = None) -> Tuple[str, str]:
    if custom_reason and str(custom_reason).strip():
        text = str(custom_reason).strip()
        return "Custom", text
    preset = UNDO_REASON_PRESET_MAP.get(str(preset_value or "").strip(), UNDO_REASON_PRESETS[0])
    return preset["label"], preset["label"]


def build_case_summary_lines(record: dict, *, include_original_reason: bool = False) -> List[str]:
    lines = [f"Action: {describe_punishment_record(record)}", f"Status: {format_case_status(record)}"]
    issued_at = iso_to_dt(record.get("timestamp"))
    if issued_at:
        lines.append(f"Issued: {discord.utils.format_dt(issued_at, 'R')}")

    duration = int(record.get("duration_minutes", 0) or 0)
    expires_at = get_record_expiry(record)
    if record.get("type") == "ban" and duration == -1:
        lines.append("Expires: Never")
    elif expires_at:
        lines.append(f"Expires: {discord.utils.format_dt(expires_at, 'R')}")

    if record.get("escalated"):
        lines.append("Escalated: Yes")
    if include_original_reason:
        lines.append(f"Original Reason: {truncate_text(record.get('reason', 'Unknown'), 140)}")
    if record.get("action_id"):
        lines.append(f"Action ID: {record['action_id']}")
    return lines


def format_case_summary_block(record: dict, *, include_original_reason: bool = False, limit: int = 1000) -> str:
    return format_log_notes(*build_case_summary_lines(record, include_original_reason=include_original_reason), limit=limit)


def add_punishment_record_log_fields(
    embed: discord.Embed,
    record: dict,
    *,
    include_original_reason: bool = False,
):
    issued_at = iso_to_dt(record.get("timestamp"))
    _, expires_at = get_punishment_duration_and_expiry(record)

    embed.add_field(name="Punishment", value=describe_punishment_record(record), inline=True)
    embed.add_field(name="Status", value=format_case_status(record), inline=True)
    embed.add_field(
        name="Issued",
        value=discord.utils.format_dt(issued_at, "F") if issued_at else "Unknown",
        inline=True,
    )
    if expires_at:
        embed.add_field(name="Expires", value=expires_at, inline=True)
    if record.get("escalated"):
        embed.add_field(name="Escalated", value="Yes", inline=True)
    if record.get("action_id"):
        embed.add_field(name="Action ID", value=f"`{record['action_id']}`", inline=True)
    if include_original_reason:
        embed.add_field(
            name="Case Violation",
            value=format_plain_log_block(record.get("reason", "Unknown"), limit=1000),
            inline=False,
        )


def build_history_archive_attachment(
    prefix: str,
    *,
    target_user_id: str,
    actor_id: int,
    payload: Dict[str, Any],
) -> Tuple[str, bytes]:
    stamp = discord.utils.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{target_user_id}_{stamp}.json"
    content = json.dumps(
        {
            "target_user_id": target_user_id,
            "actor_id": actor_id,
            "generated_at": now_iso(),
            **payload,
        },
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")
    return filename, content


async def record_case_reversal_stats(records: List[dict]):
    reversals = bot.data_manager.mod_stats.setdefault("reversals", {})
    changed = False
    for record in records:
        moderator_id = str(record.get("moderator") or "").strip()
        if not moderator_id.isdigit():
            continue
        reversals[moderator_id] = reversals.get(moderator_id, 0) + 1
        changed = True

    if changed:
        await bot.data_manager.save_mod_stats()


def pop_case_record(user_id: str, case_id: int) -> Optional[dict]:
    records = bot.data_manager.punishments.get(user_id, [])
    for index, record in enumerate(records):
        if get_case_id(record) == case_id:
            removed_record = records.pop(index)
            if not records:
                bot.data_manager.punishments.pop(user_id, None)
            return removed_record
    return None


async def reverse_punishment_effect(
    guild: discord.Guild,
    user_id: int,
    record: dict,
    *,
    undo_reason: str,
    actor: Union[discord.Member, discord.User],
) -> str:
    punishment_type = str(record.get("type", "warn") or "warn").lower()
    duration = int(record.get("duration_minutes", 0) or 0)
    audit_reason = truncate_text(f"Punishment undone by {actor} | {undo_reason}", 500)

    if punishment_type == "ban":
        should_unban = duration == -1 or bool(record.get("active"))
        if not should_unban:
            return "Removed the inactive ban record from history."
        try:
            await guild.unban(discord.Object(id=user_id), reason=audit_reason)
            return "Removed the case and unbanned the user."
        except discord.NotFound:
            return "Removed the case; the user was not currently banned."
        except discord.Forbidden:
            return "Removed the case, but the bot could not unban the user."
        except Exception as exc:
            return f"Removed the case, but unbanning failed: {exc}"

    if punishment_type == "timeout":
        member = await resolve_member(guild, user_id)
        if not is_record_active(record):
            return "Removed the inactive timeout record from history."
        if not member:
            return "Removed the timeout record; the user is no longer in the server."
        try:
            if member.is_timed_out():
                await member.timeout(None, reason=audit_reason)
                return "Removed the case and cleared the active timeout."
            return "Removed the case; the user was no longer timed out."
        except discord.Forbidden:
            return "Removed the case, but the bot could not clear the timeout."
        except Exception as exc:
            return f"Removed the case, but clearing the timeout failed: {exc}"

    if punishment_type == "kick":
        return "Removed the kick record from history."
    if punishment_type == "softban":
        return "Removed the softban record from history."
    return "Removed the warning record from history."


async def undo_case_record(
    guild: discord.Guild,
    actor: Union[discord.Member, discord.User],
    target: Union[discord.Member, discord.User],
    case_id: int,
    undo_reason: str,
) -> Tuple[bool, Optional[dict], str]:
    target_user_id, record = bot.data_manager.get_case(case_id)
    if not record or target_user_id != str(target.id):
        return False, None, "The selected case could not be found."

    removed_record = pop_case_record(target_user_id, case_id)
    if not removed_record:
        return False, None, "The selected case could not be removed from history."

    await record_case_reversal_stats([removed_record])
    await bot.data_manager.save_punishments()

    action_result = await reverse_punishment_effect(
        guild,
        target.id,
        removed_record,
        undo_reason=undo_reason,
        actor=actor,
    )
    return True, removed_record, action_result


async def clear_user_history_records(target: Union[discord.Member, discord.User]) -> List[dict]:
    user_id = str(target.id)
    history = bot.data_manager.punishments.get(user_id, [])
    if not history:
        return []

    removed_records = [copy.deepcopy(record) for record in history if isinstance(record, dict)]
    await record_case_reversal_stats(removed_records)

    bot.data_manager.punishments.pop(user_id, None)
    bot.data_manager.config.setdefault("stats", {})["cases_cleared"] = bot.data_manager.config.get("stats", {}).get("cases_cleared", 0) + len(removed_records)
    await bot.data_manager.save_config()
    await bot.data_manager.save_punishments()
    return removed_records


def build_history_clear_summary(records: List[dict]) -> str:
    if not records:
        return "Records: 0"

    sorted_records = sorted(records, key=lambda record: get_case_id(record) or 0)
    case_labels = [get_case_label(record) for record in sorted_records]
    type_counter = Counter(str(record.get("type", "warn") or "warn").lower() for record in sorted_records)
    breakdown = ", ".join(
        label
        for label in [
            f"Warnings {type_counter.get('warn', 0)}" if type_counter.get("warn") else "",
            f"Timeouts {type_counter.get('timeout', 0)}" if type_counter.get("timeout") else "",
            f"Bans {type_counter.get('ban', 0)}" if type_counter.get("ban") else "",
            f"Kicks {type_counter.get('kick', 0)}" if type_counter.get("kick") else "",
            f"Softbans {type_counter.get('softban', 0)}" if type_counter.get("softban") else "",
        ]
        if label
    ) or "No breakdown available"

    latest_record = sorted_records[-1]
    earliest_record = sorted_records[0]
    case_preview = ", ".join(case_labels[:10])
    if len(case_labels) > 10:
        case_preview = f"{case_preview}, +{len(case_labels) - 10} more"

    return format_plain_log_block(
        f"Cases: {case_preview}",
        f"Breakdown: {breakdown}",
        f"Latest: {get_case_label(latest_record)} • {truncate_text(latest_record.get('reason', 'Unknown'), 90)}",
        f"Earliest: {get_case_label(earliest_record)} • {truncate_text(earliest_record.get('reason', 'Unknown'), 90)}",
    )


def build_punishment_execution_log_embed(
    *,
    guild: discord.Guild,
    case_label: str,
    actor: str,
    target: str,
    record: dict,
    thumbnail: Optional[str] = None,
    native_log_url: Optional[str] = None,
) -> discord.Embed:
    embed = make_action_log_embed(
        f"[{case_label}] Punishment Executed",
        "A moderation action has been applied and logged successfully.",
        guild=guild,
        kind="danger",
        scope=SCOPE_MODERATION,
        actor=actor,
        target=target,
        reason=record.get("reason", "Unknown"),
        thumbnail=thumbnail,
    )
    add_punishment_record_log_fields(embed, record)

    note = truncate_text(str(record.get("note") or "").strip(), 1000)
    if note:
        embed.add_field(name="Staff Note", value=format_plain_log_block(note, limit=1000), inline=False)

    user_msg = truncate_text(str(record.get("user_msg") or "").strip(), 1000)
    if user_msg:
        embed.add_field(name="User Message", value=format_plain_log_block(user_msg, limit=1000), inline=False)

    if native_log_url:
        embed.add_field(name="Discord AutoMod Log", value=f"[Open Native Log]({native_log_url})", inline=False)

    return embed


def calculate_member_risk(history: list) -> tuple[int, str]:
    score = 0
    now = discord.utils.utcnow()

    for record in history:
        issued_at = iso_to_dt(record.get("timestamp"))
        if not issued_at or (now - issued_at).days > 90:
            continue

        punishment_type = record.get("type", "warn")
        duration = record.get("duration_minutes", 0)

        if punishment_type == "ban":
            score += 8 if duration == -1 else 6
        elif punishment_type == "timeout":
            score += 3
            if duration >= 1440:
                score += 2
        elif punishment_type in {"kick", "softban"}:
            score += 4
        else:
            score += 1

        if record.get("escalated"):
            score += 1
        if is_record_active(record, now):
            score += 2

    if score == 0:
        return score, "Clean"
    if score < 4:
        return score, "Low"
    if score < 9:
        return score, "Elevated"
    if score < 15:
        return score, "High"
    return score, "Critical"


def get_active_records_for_user(user_id: int) -> List[dict]:
    history = bot.data_manager.punishments.get(str(user_id), [])
    now = discord.utils.utcnow()
    active = [record for record in history if is_record_active(record, now)]
    active.sort(key=lambda record: get_record_expiry(record) or datetime.max.replace(tzinfo=timezone.utc))
    return active


def build_history_overview_embed(user: discord.Member, history: List[dict]) -> discord.Embed:
    embed = make_embed(
        f"History: {user.display_name}",
        "> Browse cases below, then use the panel buttons to undo a case or wipe the full record.",
        kind="info",
        scope=SCOPE_MODERATION,
        guild=user.guild,
        thumbnail=user.display_avatar.url,
    )

    active_count = len(get_active_records_for_user(user.id))
    last_record = history[-1] if history else None
    last_dt = iso_to_dt(last_record.get("timestamp")) if last_record else None
    risk_score, risk_label = calculate_member_risk(history)

    embed.add_field(name="User", value=format_user_ref(user), inline=False)
    embed.add_field(name="Total Cases", value=str(len(history)), inline=True)
    embed.add_field(name="Active", value=str(active_count), inline=True)
    embed.add_field(name="Risk", value=f"{risk_label} ({risk_score})", inline=True)
    if last_record:
        embed.add_field(name="Last Case", value=get_case_label(last_record), inline=True)
        embed.add_field(name="Last Action", value=discord.utils.format_dt(last_dt, "R") if last_dt else "Unknown", inline=True)
    return embed


def build_no_history_embed(user: Union[discord.Member, discord.User], guild: discord.Guild) -> discord.Embed:
    return make_embed(
        "No Punishment History",
        f"> **{user.display_name}** has a clean record.",
        kind="success",
        scope=SCOPE_MODERATION,
        guild=guild,
        thumbnail=user.display_avatar.url,
    )


def build_history_case_detail_embed(user: discord.Member, record: dict) -> discord.Embed:
    embed = make_embed(
        f"{get_case_label(record)} Details",
        "> Staff-only case record with punishment, issuer, timeline, and notes.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=user.guild,
        thumbnail=user.display_avatar.url,
        author_name=f"History for {user.display_name}",
        author_icon=user.display_avatar.url,
    )

    mod_id = record.get("moderator")
    embed.add_field(name="User", value=format_user_ref(user), inline=True)
    embed.add_field(name="Moderator", value=f"<@{mod_id}> (`{mod_id}`)", inline=True)
    embed.add_field(name="Status", value=format_case_status(record), inline=True)
    embed.add_field(name="Violation", value=format_reason_value(record.get("reason", "Unknown"), limit=250), inline=False)
    embed.add_field(name="Punishment", value=describe_punishment_record(record), inline=True)

    issued_at = iso_to_dt(record.get("timestamp"))
    expiry = get_record_expiry(record)
    embed.add_field(name="Issued", value=discord.utils.format_dt(issued_at, "F") if issued_at else "Unknown", inline=True)
    if record.get("duration_minutes") not in (0, None):
        embed.add_field(
            name="Expires",
            value="Never" if record.get("duration_minutes") == -1 else (discord.utils.format_dt(expiry, "F") if expiry else "Unknown"),
            inline=True,
        )

    note = truncate_text(str(record.get("note") or "").strip(), 1000)
    if note:
        embed.add_field(name="Internal Note", value=format_log_quote(note, limit=1000), inline=False)

    user_msg = record.get("user_msg")
    if user_msg:
        embed.add_field(name="Message to User", value=format_log_quote(user_msg, limit=1000), inline=False)

    return embed


def build_undo_panel_embed(
    user: discord.Member,
    history: List[dict],
    record: Optional[dict],
    *,
    reason_mode: str,
    undo_reason: str,
) -> discord.Embed:
    embed = make_embed(
        f"Undo Punishment: {user.display_name}",
        "> Select a case, choose an undo reason, then confirm the reversal.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=user.guild,
        thumbnail=user.display_avatar.url,
    )
    embed.add_field(name="User", value=format_user_ref(user), inline=False)
    embed.add_field(name="Total Cases", value=str(len(history)), inline=True)
    embed.add_field(name="Active Cases", value=str(len(get_active_records_for_user(user.id))), inline=True)
    embed.add_field(name="Reason Mode", value=reason_mode, inline=True)
    embed.add_field(name="Undo Reason", value=format_reason_value(undo_reason, limit=500), inline=False)

    if record:
        issued_at = iso_to_dt(record.get("timestamp"))
        embed.add_field(name="Selected Case", value=get_case_label(record), inline=True)
        embed.add_field(name="Action", value=describe_punishment_record(record), inline=True)
        embed.add_field(name="Issued", value=discord.utils.format_dt(issued_at, "R") if issued_at else "Unknown", inline=True)
        embed.add_field(name="Case Details", value=format_case_summary_block(record, include_original_reason=True), inline=False)
    else:
        embed.add_field(name="Selected Case", value="No cases available.", inline=False)

    return embed


def build_punishment_undo_log_embed(
    guild: discord.Guild,
    actor: Union[discord.Member, discord.User],
    target: Union[discord.Member, discord.User],
    record: dict,
    undo_reason: str,
    action_result: str,
) -> discord.Embed:
    embed = make_action_log_embed(
        f"{get_case_label(record)} Undone",
        "A punishment record was removed and the bot attempted to reverse the active sanction.",
        guild=guild,
        kind="success",
        scope=SCOPE_MODERATION,
        actor=format_user_ref(actor),
        target=format_user_ref(target),
        reason=undo_reason,
        thumbnail=target.display_avatar.url,
    )
    add_punishment_record_log_fields(embed, record, include_original_reason=True)
    embed.add_field(name="Outcome", value=format_plain_log_block(action_result), inline=False)
    return embed


def build_history_cleared_log_embed(
    guild: discord.Guild,
    actor: Union[discord.Member, discord.User],
    target: Union[discord.Member, discord.User],
    removed_records: List[dict],
) -> discord.Embed:
    embed = make_action_log_embed(
        "History Cleared",
        "A user's moderation record history has been wiped by staff.",
        guild=guild,
        kind="danger",
        scope=SCOPE_MODERATION,
        actor=format_user_ref(actor),
        target=format_user_ref(target),
        reason="Manual history wipe",
        thumbnail=target.display_avatar.url,
    )
    embed.add_field(name="Cleared Records", value=str(len(removed_records)), inline=True)
    embed.add_field(name="Summary", value=build_history_clear_summary(removed_records), inline=False)
    return embed


def build_case_detail_embed(
    guild: discord.Guild,
    target_user_id: str,
    record: dict,
    *,
    target_user: Optional[Union[discord.Member, discord.User]] = None,
) -> discord.Embed:
    target_line = format_user_ref(target_user) if target_user else format_user_id_ref(target_user_id, fallback_name=record.get("target_name"))
    moderator_id = record.get("moderator")
    issued_at = iso_to_dt(record.get("timestamp"))
    expires_at = get_record_expiry(record)
    notes = record.get("internal_notes", [])
    note_lines = []
    for note in notes[-3:]:
        if not isinstance(note, dict):
            continue
        created_at = iso_to_dt(note.get("created_at"))
        note_lines.append(
            f"<@{note.get('author_id', 0)}> • {discord.utils.format_dt(created_at, 'R') if created_at else 'Unknown'}\n{truncate_text(note.get('note', ''), 140)}"
        )

    evidence_links = record.get("evidence_links", [])
    linked_cases = record.get("linked_cases", [])
    tags = record.get("tags", [])
    assigned = record.get("assigned_moderator")

    embed = make_embed(
        f"{get_case_label(record)} Control Panel",
        "> Review and manage everything for this case from one panel.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
        thumbnail=target_user.display_avatar.url if target_user else None,
    )
    embed.add_field(name="Actor", value=f"<@{moderator_id}> (`{moderator_id}`)" if moderator_id else "Unknown", inline=True)
    embed.add_field(name="Target", value=target_line, inline=True)
    embed.add_field(name="Status", value=format_case_status(record), inline=True)
    embed.add_field(name="Reason", value=format_reason_value(record.get("reason", "Unknown"), limit=1024), inline=False)
    embed.add_field(name="Duration", value=describe_punishment_record(record), inline=True)
    if record.get("duration_minutes") not in (0, None):
        embed.add_field(name="Expires", value=("Never" if record.get("duration_minutes") == -1 else (discord.utils.format_dt(expires_at, "R") if expires_at else "Unknown")), inline=True)
    embed.add_field(name="Notes", value=join_lines(note_lines, "No internal notes."), inline=False)
    embed.add_field(name="Evidence", value=join_lines([truncate_text(url, 80) for url in evidence_links], "No evidence links."), inline=False)
    embed.add_field(name="Tags", value=", ".join(f"`{tag}`" for tag in tags) if tags else "No tags.", inline=True)
    embed.add_field(name="Assigned Moderator", value=f"<@{assigned}> (`{assigned}`)" if assigned else "Unassigned", inline=True)
    embed.add_field(name="Linked Cases", value=", ".join(f"`#{case_id}`" for case_id in linked_cases) if linked_cases else "None", inline=True)
    if issued_at:
        embed.add_field(name="Issued", value=discord.utils.format_dt(issued_at, "F"), inline=True)
    if record.get("action_id"):
        embed.add_field(name="Action ID", value=f"`{record.get('action_id')}`", inline=True)
    return embed


def build_all_cases_embed(
    guild: discord.Guild,
    page_items: List[Tuple[str, dict]],
    *,
    page: int,
    max_pages: int,
    total: int,
    counts: Dict[str, int],
) -> discord.Embed:
    """Server-wide case browser: every case (bans, timeouts, warns, kicks,
    softbans) in case order, paginated."""
    embed = make_embed(
        "Server Cases",
        "> Every moderation case on record, newest first. Select one below to open its full panel.",
        kind="info",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    if total == 0:
        embed.description = "> No moderation cases have been recorded yet."
        return embed

    embed.add_field(name="Total Cases", value=str(total), inline=True)
    breakdown = join_lines([
        f"Bans: {counts.get('ban', 0)}",
        f"Timeouts: {counts.get('timeout', 0)}",
        f"Warns: {counts.get('warn', 0)}",
        f"Kicks: {counts.get('kick', 0)}",
        f"Softbans: {counts.get('softban', 0)}",
    ])
    embed.add_field(name="Breakdown", value=breakdown, inline=True)
    embed.add_field(name="Page", value=f"{page + 1}/{max_pages}", inline=True)

    lines = []
    for user_id, record in page_items:
        lines.append(
            f"**{get_case_label(record)}** • <@{user_id}> — {describe_punishment_record(record)} — {format_case_status(record)}\n"
            f"> {truncate_text(record.get('reason', 'Unknown'), 80)}"
        )
    if lines:
        embed.description = f"{embed.description}\n\n" + truncate_text("\n".join(lines), 3800)
    return embed


def build_mod_help_embed(guild: discord.Guild) -> discord.Embed:
    embed = make_embed(
        "Moderation Command Guide",
        "> Core moderation workflows, context tools, and channel controls.",
        kind="info",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(
        name="Case Management",
        value="\n".join([
            "`/case` — Open a case panel for notes, status, evidence, and assignment.",
            "`/cases` — Browse every case on the server in case order.",
            "`/history` — Browse a user’s disciplinary record case-by-case.",
            "`/undo` — Reverse a punishment with a reason and case selector.",
        ]),
        inline=False,
    )
    embed.add_field(
        name="Actions",
        value="\n".join([
            "`/punish` — Open the sanction console with smart escalation and optional public posting.",
            "`/purge` — Bulk-delete messages with user or keyword filtering.",
        ]),
        inline=False,
    )
    embed.add_field(
        name="Channel Controls",
        value="\n".join([
            "`/lock` — Restrict messaging in the current channel.",
            "`/unlock` — Restore messaging in the current channel.",
        ]),
        inline=False,
    )
    return embed



def _split_case_input(value: str) -> List[str]:
    return [part.strip() for part in re.split(r"[\n,]+", value or "") if part.strip()]


class CasesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(CasesCog(bot))
