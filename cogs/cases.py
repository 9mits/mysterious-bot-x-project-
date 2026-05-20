# modules/commands/cases.py
# Case management helpers, embed builders, and interactive Views.

import discord
from discord.ext import commands
import copy
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Union, Tuple, Any
from collections import Counter
import html
import re
import io

from core.constants import (
    BRAND_NAME,
    DEFAULT_ROLE_OWNER,
    DEFAULT_RULES,
    FEATURE_FLAG_LABELS,
    SCOPE_ANALYTICS,
    SCOPE_MODERATION,
    SCOPE_SYSTEM,
)
from core.models import CaseNote
from core.services import (
    export_case_payload,
    normalize_case_record,
    sanitize_evidence_links,
    sanitize_linked_cases,
    sanitize_tags,
)
from core.context import bot
from core.utils import iso_to_dt, now_iso, parse_duration_str
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
    brand_embed,
    make_empty_state_embed,
    make_confirmation_embed,
    join_lines,
    format_user_ref,
    format_user_id_ref,
    send_log,
    send_punishment_log,
    respond_with_error,
    resolve_member,
    create_progress_bar,
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


def build_active_punishments_embed(guild: discord.Guild, active_list: List[tuple], now: datetime) -> discord.Embed:
    display_limit = 22
    embed = make_embed(
        "Active Punishments",
        "> Timeouts and bans that are still in effect are listed below. Select one for full case details.",
        kind="danger",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(name="Open Cases", value=str(len(active_list)), inline=True)
    embed.add_field(name="Generated", value=discord.utils.format_dt(now, "R"), inline=True)

    type_counter = Counter(record.get("type", "unknown") for _, record, _, _, _ in active_list)
    breakdown = join_lines([
        f"Bans: {type_counter.get('ban', 0)}",
        f"Timeouts: {type_counter.get('timeout', 0)}",
    ])
    embed.add_field(name="Breakdown", value=breakdown, inline=True)

    for uid, record, expiry, _, name in active_list[:display_limit]:
        expiry_text = "Never" if record.get("duration_minutes") == -1 else discord.utils.format_dt(expiry, "R")
        embed.add_field(
            name=f"{get_case_label(record)} • {name}",
            value=join_lines([
                f"User: <@{uid}>",
                f"Action: {describe_punishment_record(record)}",
                f"Reason: {truncate_text(record.get('reason', 'Unknown'), 100)}",
                f"Expires: {expiry_text}",
            ]),
            inline=False,
        )

    if len(active_list) > display_limit:
        embed.set_footer(text=f"{BRAND_NAME} • {SCOPE_MODERATION} • Showing {display_limit} of {len(active_list)} active cases")
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
            "`/history` — Browse a user’s disciplinary record case-by-case.",
            "`/active` — View all active bans and timeouts.",
            "`/undopunish` — Reverse a punishment with a reason and case selector.",
        ]),
        inline=False,
    )
    embed.add_field(
        name="Actions",
        value="\n".join([
            "`/punish` — Open the sanction console with smart escalation.",
            "`/publicpunish` — Punish and post the result publicly in the channel.",
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


class FinalConfirmClear(discord.ui.View):
    def __init__(self, target, moderator, origin_message=None):
        super().__init__(timeout=60)
        self.target = target
        self.moderator = moderator
        self.origin_message = origin_message

    @discord.ui.button(label="YES, WIPE EVERYTHING", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        removed_records = await clear_user_history_records(self.target)
        if removed_records:
            attachment = build_history_archive_attachment(
                "history_clear",
                target_user_id=str(self.target.id),
                actor_id=self.moderator.id,
                payload={"action": "history_clear", "records": removed_records},
            )
            log_embed = build_history_cleared_log_embed(interaction.guild, self.moderator, self.target, removed_records)
            await send_punishment_log(interaction.guild, log_embed, attachments=[attachment])

            await interaction.response.edit_message(content="**History has been completely wiped.**", view=None)

            if self.origin_message:
                try:
                    from .roles import build_punish_embed
                    await self.origin_message.edit(embed=build_punish_embed(self.target))
                except Exception:
                    pass
        else:
            await interaction.response.edit_message(content="User has no history to clear.", view=None)

    @discord.ui.button(label="No, Stop", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Clear history canceled.", view=None)

class HistorySelect(discord.ui.Select):
    def __init__(self, page_items: List[dict], panel: "HistoryView"):
        self.panel = panel
        options = []
        for record in page_items:
            case_id = get_case_id(record)
            if case_id is None:
                continue
            reason = record.get("reason", "Unknown")
            dt = iso_to_dt(record.get("timestamp"))
            date_str = dt.strftime("%Y-%m-%d") if dt else "Unknown"
            label = f"{get_case_label(record)}: {truncate_text(reason, 70)}"
            desc = f"{date_str} • {describe_punishment_record(record)}"
            options.append(discord.SelectOption(label=label, description=desc, value=str(case_id)))

        if not options:
            options.append(discord.SelectOption(label="No cases found", value="0", description="There are no valid cases on this page."))

        super().__init__(placeholder="Select a case to view details...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "0":
            await respond_with_error(interaction, "There are no valid cases to open on this page.", scope=SCOPE_MODERATION)
            return

        self.panel.message = interaction.message
        self.panel.selected_case_id = int(self.values[0])
        self.panel.mode = "history"
        self.panel.update_components()
        await interaction.response.edit_message(embed=self.panel.build_embed(), view=self.panel)


class UndoCaseSelect(discord.ui.Select):
    def __init__(self, page_items: List[dict], panel: "HistoryView"):
        self.panel = panel
        options = []
        for record in page_items:
            case_id = get_case_id(record)
            if case_id is None:
                continue
            dt = iso_to_dt(record.get("timestamp"))
            date_str = dt.strftime("%Y-%m-%d") if dt else "Unknown"
            label = f"{get_case_label(record)} ({date_str})"
            desc = truncate_text(f"{describe_punishment_record(record)} • {record.get('reason', 'Unknown')}", 100)
            options.append(
                discord.SelectOption(
                    label=label,
                    description=desc,
                    value=str(case_id),
                    default=case_id == panel.selected_case_id,
                )
            )

        if not options:
            options.append(discord.SelectOption(label="No cases found", value="0", description="There are no valid cases on this page."))

        super().__init__(placeholder="Select punishment to undo...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "0":
            await respond_with_error(interaction, "There are no valid cases to undo on this page.", scope=SCOPE_MODERATION)
            return

        self.panel.message = interaction.message
        self.panel.selected_case_id = int(self.values[0])
        self.panel.update_components()
        await interaction.response.edit_message(embed=self.panel.build_embed(), view=self.panel)


class UndoReasonSelect(discord.ui.Select):
    def __init__(self, panel: "HistoryView"):
        self.panel = panel
        options = [
            discord.SelectOption(
                label=preset["label"],
                value=preset["value"],
                description=truncate_text(preset["description"], 100),
                default=(not panel.custom_undo_reason and preset["value"] == panel.undo_reason_value),
            )
            for preset in UNDO_REASON_PRESETS
        ]
        super().__init__(placeholder="Select an undo reason preset...", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.panel.message = interaction.message
        self.panel.undo_reason_value = self.values[0]
        self.panel.custom_undo_reason = None
        self.panel.update_components()
        await interaction.response.edit_message(embed=self.panel.build_embed(), view=self.panel)


class HistoryActionButton(discord.ui.Button):
    def __init__(self, label: str, style: discord.ButtonStyle, action: str, *, row: int, disabled: bool = False):
        super().__init__(label=label, style=style, row=row, disabled=disabled)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view: HistoryView = self.view
        await view.handle_action(interaction, self.action)

class HistoryNavButton(discord.ui.Button):
    def __init__(self, label: str, style: discord.ButtonStyle, direction: int, *, row: int, disabled: bool = False):
        super().__init__(label=label, style=style, row=row, disabled=disabled)
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: HistoryView = self.view
        view.message = interaction.message
        view.page = max(0, min(view.max_pages - 1, view.page + self.direction))
        if view.mode == "undo":
            page_items = view.get_page_items()
            if page_items:
                view.selected_case_id = get_case_id(page_items[0])
        view.update_components()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class UndoReasonModal(discord.ui.Modal, title="Custom Undo Reason"):
    reason = discord.ui.TextInput(
        label="Undo Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why this punishment is being undone.",
        max_length=500,
    )

    def __init__(self, panel: "HistoryView"):
        super().__init__()
        self.panel = panel
        if panel.custom_undo_reason:
            self.reason.default = panel.custom_undo_reason

    async def on_submit(self, interaction: discord.Interaction):
        custom_reason = self.reason.value.strip()
        if not custom_reason:
            await respond_with_error(interaction, "The undo reason cannot be empty.", scope=SCOPE_MODERATION)
            return

        self.panel.custom_undo_reason = custom_reason
        await self.panel.refresh_panel_message()
        await interaction.response.send_message(
            embed=make_confirmation_embed(
                "Undo Reason Saved",
                "> The custom undo reason was saved to the panel.",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
            ),
            ephemeral=True,
        )


class UndoConfirmView(discord.ui.View):
    def __init__(self, panel: "HistoryView"):
        super().__init__(timeout=120)
        self.panel = panel

    @discord.ui.button(label="Confirm Undo", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = self.panel.get_selected_record()
        undo_reason = self.panel.get_current_undo_reason_text()
        if not record or not undo_reason:
            await interaction.response.edit_message(content="The selected case is no longer available.", embed=None, view=None)
            return

        await interaction.response.edit_message(content="Processing undo...", embed=None, view=None)
        success, removed_record, action_result = await undo_case_record(
            interaction.guild,
            interaction.user,
            self.panel.user,
            get_case_id(record) or 0,
            undo_reason,
        )
        if not success or not removed_record:
            await interaction.edit_original_response(content=action_result, embed=None, view=None)
            return

        attachment = build_history_archive_attachment(
            "undo_case",
            target_user_id=str(self.panel.user.id),
            actor_id=interaction.user.id,
            payload={
                "action": "undo_case",
                "undo_reason": undo_reason,
                "record": removed_record,
            },
        )
        log_embed = build_punishment_undo_log_embed(interaction.guild, interaction.user, self.panel.user, removed_record, undo_reason, action_result)
        from .moderation import RevokeUndoView
        view = RevokeUndoView(self.panel.user.id, removed_record, interaction.user.id)
        await send_punishment_log(interaction.guild, log_embed, view=view, attachments=[attachment])

        await self.panel.refresh_panel_message()
        await interaction.edit_original_response(
            content=f"**{get_case_label(removed_record)}** was undone.\n{action_result}",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Undo canceled.", embed=None, view=None)


class HistoryClearConfirmView(discord.ui.View):
    def __init__(self, panel: "HistoryView"):
        super().__init__(timeout=120)
        self.panel = panel

    @discord.ui.button(label="Yes, Clear History", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Clearing history...", embed=None, view=None)
        removed_records = await clear_user_history_records(self.panel.user)
        if not removed_records:
            await self.panel.refresh_panel_message()
            await interaction.edit_original_response(content="User has no history to clear.", embed=None, view=None)
            return

        attachment = build_history_archive_attachment(
            "history_clear",
            target_user_id=str(self.panel.user.id),
            actor_id=interaction.user.id,
            payload={"action": "history_clear", "records": removed_records},
        )
        log_embed = build_history_cleared_log_embed(interaction.guild, interaction.user, self.panel.user, removed_records)
        await send_punishment_log(interaction.guild, log_embed, attachments=[attachment])

        await self.panel.refresh_panel_message()
        await interaction.edit_original_response(content="**History has been completely wiped.**", embed=None, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Clear history canceled.", embed=None, view=None)

class HistoryView(discord.ui.View):
    def __init__(self, user: discord.Member, *, mode: str = "history", selected_case_id: Optional[int] = None, initial_undo_reason: Optional[str] = None):
        super().__init__(timeout=300)
        self.user = user
        self.mode = mode if mode in {"history", "undo"} else "history"
        self.selected_case_id = selected_case_id
        self.custom_undo_reason = str(initial_undo_reason or "").strip() or None
        self.undo_reason_value = UNDO_REASON_PRESETS[0]["value"]
        self.message: Optional[discord.Message] = None
        self.page = 0
        self.items_per_page = 25
        self.history: List[dict] = []
        self.sorted_history: List[dict] = []
        self.max_pages = 1
        self.reload_history()
        if self.mode == "undo" and not self.selected_case_id and self.sorted_history:
            self.selected_case_id = get_case_id(self.sorted_history[0])
        self.ensure_page_for_selected_case()
        self.update_components()

    def reload_history(self):
        self.history = [record for record in bot.data_manager.punishments.get(str(self.user.id), []) if isinstance(record, dict)]
        self.sorted_history = sorted(
            self.history,
            key=lambda record: (get_case_id(record) or 0, record.get("timestamp", "")),
            reverse=True,
        )
        self.max_pages = max(1, (len(self.sorted_history) + self.items_per_page - 1) // self.items_per_page)
        self.page = max(0, min(self.page, self.max_pages - 1))
        if self.selected_case_id and not any(get_case_id(record) == self.selected_case_id for record in self.sorted_history):
            self.selected_case_id = get_case_id(self.sorted_history[0]) if self.mode == "undo" and self.sorted_history else None

    def ensure_page_for_selected_case(self):
        if not self.selected_case_id:
            self.page = max(0, min(self.page, self.max_pages - 1))
            return
        for index, record in enumerate(self.sorted_history):
            if get_case_id(record) == self.selected_case_id:
                self.page = index // self.items_per_page
                return
        self.page = max(0, min(self.page, self.max_pages - 1))

    def get_page_items(self) -> List[dict]:
        start = self.page * self.items_per_page
        end = start + self.items_per_page
        return self.sorted_history[start:end]

    def get_selected_record(self) -> Optional[dict]:
        if not self.selected_case_id:
            return None
        for record in self.sorted_history:
            if get_case_id(record) == self.selected_case_id:
                return record
        return None

    def get_current_undo_reason_mode(self) -> str:
        return get_undo_reason_details(self.undo_reason_value, self.custom_undo_reason)[0]

    def get_current_undo_reason_text(self) -> str:
        return get_undo_reason_details(self.undo_reason_value, self.custom_undo_reason)[1]

    def build_embed(self) -> discord.Embed:
        if not self.sorted_history:
            return build_no_history_embed(self.user, self.user.guild)
        if self.mode == "undo":
            return build_undo_panel_embed(
                self.user,
                self.history,
                self.get_selected_record(),
                reason_mode=self.get_current_undo_reason_mode(),
                undo_reason=self.get_current_undo_reason_text(),
            )
        selected_record = self.get_selected_record()
        if selected_record:
            return build_history_case_detail_embed(self.user, selected_record)
        return build_history_overview_embed(self.user, self.history)

    async def refresh_panel_message(self):
        self.reload_history()
        if self.mode == "undo" and not self.selected_case_id and self.sorted_history:
            self.selected_case_id = get_case_id(self.sorted_history[0])
        self.ensure_page_for_selected_case()
        if not self.sorted_history:
            self.stop()
            if self.message:
                await self.message.edit(embed=build_no_history_embed(self.user, self.user.guild), view=None)
            return
        self.update_components()
        if self.message:
            await self.message.edit(embed=self.build_embed(), view=self)

    def update_components(self):
        self.clear_items()
        if not self.sorted_history:
            return

        if self.mode == "undo":
            self.add_item(UndoCaseSelect(self.get_page_items(), self))
            self.add_item(UndoReasonSelect(self))
            if self.max_pages > 1:
                self.add_item(HistoryNavButton("Previous", discord.ButtonStyle.primary, -1, row=2, disabled=(self.page == 0)))
                self.add_item(discord.ui.Button(label=f"Page {self.page + 1}/{self.max_pages}", disabled=True, style=discord.ButtonStyle.secondary, row=2))
                self.add_item(HistoryNavButton("Next", discord.ButtonStyle.primary, 1, row=2, disabled=(self.page >= self.max_pages - 1)))
            self.add_item(HistoryActionButton("Back to History", discord.ButtonStyle.secondary, "back_to_history", row=3))
            self.add_item(HistoryActionButton("Custom Reason", discord.ButtonStyle.primary, "custom_reason", row=3))
            self.add_item(HistoryActionButton("Refresh", discord.ButtonStyle.secondary, "refresh", row=3))
            self.add_item(HistoryActionButton("Undo Selected", discord.ButtonStyle.danger, "undo_selected", row=3, disabled=(self.get_selected_record() is None)))
            return

        if self.selected_case_id:
            self.add_item(HistoryActionButton("Back to Overview", discord.ButtonStyle.secondary, "history_overview", row=0))
            self.add_item(HistoryActionButton("Undo This Case", discord.ButtonStyle.danger, "open_undo", row=0))
            self.add_item(HistoryActionButton("Refresh", discord.ButtonStyle.secondary, "refresh", row=0))
            self.add_item(HistoryActionButton("Clear History", discord.ButtonStyle.danger, "clear_history", row=1))
            return

        self.add_item(HistorySelect(self.get_page_items(), self))
        if self.max_pages > 1:
            self.add_item(HistoryNavButton("Previous", discord.ButtonStyle.primary, -1, row=1, disabled=(self.page == 0)))
            self.add_item(discord.ui.Button(label=f"Page {self.page + 1}/{self.max_pages}", disabled=True, style=discord.ButtonStyle.secondary, row=1))
            self.add_item(HistoryNavButton("Next", discord.ButtonStyle.primary, 1, row=1, disabled=(self.page >= self.max_pages - 1)))
        self.add_item(HistoryActionButton("Refresh", discord.ButtonStyle.secondary, "refresh", row=2))
        self.add_item(HistoryActionButton("Undo Punishment", discord.ButtonStyle.danger, "open_undo", row=2))
        self.add_item(HistoryActionButton("Clear History", discord.ButtonStyle.danger, "clear_history", row=2))

    async def handle_action(self, interaction: discord.Interaction, action: str):
        self.message = interaction.message
        if action == "refresh":
            await self.refresh_after_interaction(interaction)
            return

        if action == "history_overview":
            self.mode = "history"
            self.selected_case_id = None
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        if action == "back_to_history":
            self.mode = "history"
            self.ensure_page_for_selected_case()
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        if action == "open_undo":
            self.mode = "undo"
            if not self.selected_case_id:
                page_items = self.get_page_items()
                if page_items:
                    self.selected_case_id = get_case_id(page_items[0])
                elif self.sorted_history:
                    self.selected_case_id = get_case_id(self.sorted_history[0])
            self.ensure_page_for_selected_case()
            self.update_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        if action == "custom_reason":
            await interaction.response.send_modal(UndoReasonModal(self))
            return

        if action == "undo_selected":
            record = self.get_selected_record()
            if not record:
                await respond_with_error(interaction, "Select a case to undo first.", scope=SCOPE_MODERATION)
                return

            confirm_embed = make_embed(
                f"Undo {get_case_label(record)}",
                "> Confirm this reversal. The case will be removed from history and the bot will try to reverse any active punishment.",
                kind="danger",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
                thumbnail=self.user.display_avatar.url,
            )
            confirm_embed.add_field(name="Undo Reason", value=format_reason_value(self.get_current_undo_reason_text(), limit=500), inline=False)
            confirm_embed.add_field(name="Case Details", value=format_case_summary_block(record, include_original_reason=True), inline=False)
            await interaction.response.send_message(embed=confirm_embed, view=UndoConfirmView(self), ephemeral=True)
            return

        if action == "clear_history":
            await interaction.response.send_message(
                "**Are you sure you want to clear this user's punishment history?**",
                view=HistoryClearConfirmView(self),
                ephemeral=True,
            )
            return

    async def refresh_after_interaction(self, interaction: discord.Interaction):
        self.reload_history()
        if self.mode == "undo" and not self.selected_case_id and self.sorted_history:
            self.selected_case_id = get_case_id(self.sorted_history[0])
        self.ensure_page_for_selected_case()
        self.update_components()
        if not self.sorted_history:
            self.stop()
            await interaction.response.edit_message(embed=build_no_history_embed(self.user, interaction.guild), view=None)
            return
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


async def log_case_management_action(
    guild: discord.Guild,
    actor: discord.Member,
    target_user_id: str,
    record: dict,
    action: str,
    details: str,
):
    detail_lines = [line.strip() for line in str(details or "").splitlines() if line.strip()]
    embed = make_action_log_embed(
        f"{get_case_label(record)} Updated",
        "A case-management action modified the record metadata.",
        guild=guild,
        kind="info",
        scope=SCOPE_MODERATION,
        actor=format_user_ref(actor),
        target=f"<@{target_user_id}> (`{target_user_id}`)",
        reason=action,
        duration="Record Updated",
        expires="N/A",
        notes=detail_lines or [f"Result: {truncate_text(details, 500)}"],
    )
    if record.get("action_id"):
        embed.add_field(name="Action ID", value=f"`{record['action_id']}`", inline=True)
    await send_punishment_log(guild, embed)


def _split_case_input(value: str) -> List[str]:
    return [part.strip() for part in re.split(r"[\n,]+", value or "") if part.strip()]


class CaseNoteModal(discord.ui.Modal, title="Add Internal Case Note"):
    note = discord.ui.TextInput(
        label="Internal Note",
        style=discord.TextStyle.paragraph,
        placeholder="Staff-only note for future context.",
        max_length=1000,
    )

    def __init__(self, panel: "CasePanelView"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        target_user_id, record = bot.data_manager.get_case(self.panel.case_id)
        if not record or not target_user_id:
            await respond_with_error(interaction, "The selected case no longer exists.", scope=SCOPE_MODERATION)
            return

        notes = record.setdefault("internal_notes", [])
        notes.append(CaseNote(interaction.user.id, self.note.value.strip(), now_iso()).to_dict())
        normalize_case_record(record)
        await bot.data_manager.save_punishments()
        await log_case_management_action(interaction.guild, interaction.user, target_user_id, record, "Internal note added", self.note.value)
        await self.panel.refresh_panel()
        await interaction.response.send_message(
            embed=make_confirmation_embed(
                f"{get_case_label(record)} Saved",
                "> Internal note added to the case record.",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
            ),
            ephemeral=True,
        )


class CaseLinksModal(discord.ui.Modal, title="Update Evidence and Tags"):
    evidence_links = discord.ui.TextInput(
        label="Evidence Links",
        style=discord.TextStyle.paragraph,
        placeholder="Paste URLs separated by commas or new lines.",
        required=False,
        max_length=1000,
    )
    linked_cases = discord.ui.TextInput(
        label="Related Case IDs",
        placeholder="Example: 101, 118, 204",
        required=False,
        max_length=200,
    )
    tags = discord.ui.TextInput(
        label="Tags",
        placeholder="Example: scam, repeat-offender, escalated",
        required=False,
        max_length=200,
    )

    def __init__(self, panel: "CasePanelView"):
        super().__init__()
        self.panel = panel
        _, record = bot.data_manager.get_case(panel.case_id)
        if record:
            self.evidence_links.default = "\n".join(record.get("evidence_links", []))
            self.linked_cases.default = ", ".join(str(case_id) for case_id in record.get("linked_cases", []))
            self.tags.default = ", ".join(record.get("tags", []))

    async def on_submit(self, interaction: discord.Interaction):
        target_user_id, record = bot.data_manager.get_case(self.panel.case_id)
        if not record or not target_user_id:
            await respond_with_error(interaction, "The selected case no longer exists.", scope=SCOPE_MODERATION)
            return

        record["evidence_links"] = sanitize_evidence_links(_split_case_input(self.evidence_links.value))
        record["linked_cases"] = sanitize_linked_cases(_split_case_input(self.linked_cases.value), current_case_id=record.get("case_id"))
        record["tags"] = sanitize_tags(_split_case_input(self.tags.value))
        normalize_case_record(record)
        await bot.data_manager.save_punishments()
        await log_case_management_action(
            interaction.guild,
            interaction.user,
            target_user_id,
            record,
            "Links and tags updated",
            f"Tags: {', '.join(record['tags']) or 'None'} | Linked: {', '.join(str(case_id) for case_id in record['linked_cases']) or 'None'}",
        )
        await self.panel.refresh_panel()
        await interaction.response.send_message(
            embed=make_confirmation_embed(
                f"{get_case_label(record)} Saved",
                "> Evidence links, linked cases, and tags were updated.",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
            ),
            ephemeral=True,
        )


class CaseStateSelect(discord.ui.Select):
    def __init__(self, panel: "CasePanelView"):
        self.panel = panel
        _, record = bot.data_manager.get_case(panel.case_id)
        current = f"{record.get('status', 'open')}|{record.get('resolution_state', 'pending')}" if record else ""
        options = []
        for status, resolution, label, description in [
            ("open", "pending", "Open - Waiting", "New case that still needs review."),
            ("open", "active", "Open - In Progress", "Staff are actively handling this case."),
            ("review", "pending", "Under Review", "Waiting for staff review."),
            ("appealed", "pending", "Appeal Waiting", "The user appealed and staff still need to decide."),
            ("closed", "resolved", "Closed - Finished", "Handled and fully closed."),
            ("closed", "reversed", "Closed - Reversed", "The action was undone or reversed."),
            ("closed", "expired", "Closed - Expired", "The timed action ended on its own."),
        ]:
            options.append(
                discord.SelectOption(
                    label=label,
                    value=f"{status}|{resolution}",
                    description=description,
                    default=current == f"{status}|{resolution}",
                )
            )
        super().__init__(placeholder="Choose the case status...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        target_user_id, record = bot.data_manager.get_case(self.panel.case_id)
        if not record or not target_user_id:
            await respond_with_error(interaction, "The selected case no longer exists.", scope=SCOPE_MODERATION)
            return

        status, resolution = self.values[0].split("|", 1)
        record["status"] = status
        record["resolution_state"] = resolution
        normalize_case_record(record)
        await bot.data_manager.save_punishments()
        await log_case_management_action(
            interaction.guild,
            interaction.user,
            target_user_id,
            record,
            "Status updated",
            f"Status: {status} | Resolution: {resolution}",
        )
        await self.panel.refresh_panel()
        await interaction.response.edit_message(
            embed=make_confirmation_embed(
                f"{get_case_label(record)} Updated",
                "> Case status and resolution state were updated.",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
            ),
            view=None,
        )


class CaseStateView(discord.ui.View):
    def __init__(self, panel: "CasePanelView"):
        super().__init__(timeout=120)
        self.add_item(CaseStateSelect(panel))


class CaseSwitchSelect(discord.ui.Select):
    def __init__(self, panel: "CasePanelView"):
        self.panel = panel
        options = []
        for case_id in panel.case_ids[:25]:
            _, record = bot.data_manager.get_case(case_id)
            if not record:
                continue
            label = truncate_text(f"{get_case_label(record)} • {record.get('reason', 'Unknown')}", 100)
            description = truncate_text(f"{describe_punishment_record(record)} • {format_case_status(record)}", 100)
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(case_id),
                    default=case_id == panel.case_id,
                )
            )
        if not options:
            options.append(discord.SelectOption(label="No cases found", value="0"))
        super().__init__(placeholder="Open another case...", min_values=1, max_values=1, options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "0":
            await respond_with_error(interaction, "No valid cases are available.", scope=SCOPE_MODERATION)
            return
        self.panel.case_id = int(self.values[0])
        self.panel.sync_buttons()
        await interaction.response.edit_message(embed=self.panel.build_embed(), view=self.panel)


class CasePanelView(discord.ui.View):
    def __init__(self, target_user_id: str, case_ids: List[int], target_user: Optional[Union[discord.Member, discord.User]] = None):
        super().__init__(timeout=300)
        self.target_user_id = target_user_id
        self.case_ids = case_ids
        self.case_id = case_ids[0]
        self.target_user = target_user
        self.message: Optional[discord.Message] = None
        if len(self.case_ids) > 1:
            self.add_item(CaseSwitchSelect(self))
        self.sync_buttons()

    def current_record(self) -> Optional[dict]:
        _, record = bot.data_manager.get_case(self.case_id)
        return record

    def build_embed(self) -> discord.Embed:
        record = self.current_record()
        if not record:
            return make_empty_state_embed(
                "Case Not Found",
                "> The selected case could not be loaded.",
                scope=SCOPE_MODERATION,
                guild=self.target_user.guild if isinstance(self.target_user, discord.Member) else None,
            )
        guild = self.target_user.guild if isinstance(self.target_user, discord.Member) else (self.message.guild if self.message else None)
        return build_case_detail_embed(guild, self.target_user_id, record, target_user=self.target_user)

    def sync_buttons(self):
        record = self.current_record() or {}
        assigned = record.get("assigned_moderator")
        self.claim_case.label = "Unclaim Case" if assigned else "Claim Case"
        self.claim_case.style = discord.ButtonStyle.secondary if assigned else discord.ButtonStyle.success

    async def refresh_panel(self):
        self.sync_buttons()
        if self.message:
            await self.message.edit(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=0)
    async def refresh_case(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        self.sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Claim Case", style=discord.ButtonStyle.success, row=0)
    async def claim_case(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        record = self.current_record()
        if not record:
            await respond_with_error(interaction, "The selected case could not be loaded.", scope=SCOPE_MODERATION)
            return

        currently_assigned = record.get("assigned_moderator")
        record["assigned_moderator"] = None if currently_assigned == interaction.user.id else interaction.user.id
        normalize_case_record(record)
        await bot.data_manager.save_punishments()
        await log_case_management_action(
            interaction.guild,
            interaction.user,
            self.target_user_id,
            record,
            "Assignment updated",
            "Case claimed by moderator." if record.get("assigned_moderator") else "Case unclaimed.",
        )
        self.sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Add Note", style=discord.ButtonStyle.primary, row=0)
    async def add_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        await interaction.response.send_modal(CaseNoteModal(self))

    @discord.ui.button(label="Change Status", style=discord.ButtonStyle.primary, row=0)
    async def case_state(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        await interaction.response.send_message(
            embed=make_embed(
                "Case Status",
                "> Pick the status that best matches what is happening with this case right now.",
                kind="info",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
            ),
            view=CaseStateView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="Evidence & Tags", style=discord.ButtonStyle.primary, row=1)
    async def links_and_tags(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.message = interaction.message
        await interaction.response.send_modal(CaseLinksModal(self))

    @discord.ui.button(label="Download Case", style=discord.ButtonStyle.secondary, row=1)
    async def export_case(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = self.current_record()
        if not record:
            await respond_with_error(interaction, "The selected case could not be loaded.", scope=SCOPE_MODERATION)
            return

        payload = export_case_payload(self.target_user_id, record)
        buffer = io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
        file = discord.File(buffer, filename=f"case_{record.get('case_id', 'unknown')}.json")
        await interaction.response.send_message(
            embed=make_confirmation_embed(
                f"{get_case_label(record)} Download Ready",
                "> A case file was generated for this case.",
                scope=SCOPE_MODERATION,
                guild=interaction.guild,
            ),
            file=file,
            ephemeral=True,
        )


class FirstConfirmClear(discord.ui.View):
    def __init__(self, target, moderator, origin_message=None):
        super().__init__(timeout=60)
        self.target = target
        self.moderator = moderator
        self.origin_message = origin_message

    @discord.ui.button(label="Yes, Clear History", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"**WAIT!** Are you **REALLY** sure?\nThis will wipe ALL past violations for {self.target.mention}.\nThey will be treated as a new user for future punishments.",
            view=FinalConfirmClear(self.target, self.moderator, self.origin_message)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Clear history canceled.", view=None)

class PunishView(discord.ui.View):
    def __init__(self, target, moderator, public=False, reaction_count=None):
        super().__init__(timeout=60)
        self.target = target
        self.moderator = moderator
        from .moderation import PunishSelect
        self.add_item(PunishSelect(target, moderator, public=public, reaction_count=reaction_count))

    @discord.ui.button(label="Clear History", style=discord.ButtonStyle.danger, row=1)
    async def clear_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "**Are you sure you want to clear this user's punishment history?**", 
            view=FirstConfirmClear(self.target, self.moderator, interaction.message), 
            ephemeral=True
        )

    @discord.ui.button(label="View History", style=discord.ButtonStyle.secondary, row=1)
    async def view_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = self.target if isinstance(self.target, discord.Member) else await resolve_member(interaction.guild, self.target.id)
        if not member:
            await interaction.response.send_message("This user is no longer in the server, so the interactive history panel is unavailable.", ephemeral=True)
            return

        uid = str(member.id)
        history_data = bot.data_manager.punishments.get(uid, [])
        
        if not history_data:
            await interaction.response.send_message(f"**{member.display_name}** has a clean record (No history found).", ephemeral=True)
            return

        view = HistoryView(member)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

class RuleEditModal(discord.ui.Modal, title="Add/Edit Punishment Rule"):
    rule_name = discord.ui.TextInput(label="Rule Name", placeholder="e.g. Spamming", max_length=50)
    base_dur = discord.ui.TextInput(label="Base Duration (mins)", placeholder="0=Warn, -1=Ban", max_length=10)
    esc_dur = discord.ui.TextInput(label="Escalated Duration (mins)", placeholder="Repeat offense duration", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.rule_name.value.strip()
        if not name:
            await interaction.response.send_message("Rule name cannot be empty.", ephemeral=True)
            return
            
        # Use parse_duration_str to allow "ban", "1d", "30m" etc.
        base = parse_duration_str(self.base_dur.value.strip())
        esc = parse_duration_str(self.esc_dur.value.strip())
            
        rules = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        rules[name] = {"base": base, "escalated": esc}
        bot.data_manager.config["punishment_rules"] = rules
        await bot.data_manager.save_config()
        
        # Log
        log_embed = make_embed(
            "Punishment Rule Updated",
            "> An escalation rule was created or overwritten from the rules dashboard.",
            kind="info",
            scope=SCOPE_SYSTEM,
            guild=interaction.guild,
        )
        log_embed.add_field(name="Actor", value=format_user_ref(interaction.user), inline=True)
        log_embed.add_field(name="Rule", value=name, inline=True)
        log_embed.add_field(name="Values", value=f"> Base: {base}m\n> Escalated: {esc}m", inline=True)
        await send_log(interaction.guild, log_embed)
        
        await interaction.response.send_message(f"Rule **{name}** saved successfully.", ephemeral=True)

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

class ActiveView(discord.ui.View):
    def __init__(self, active_list):
        super().__init__(timeout=180)
        self.add_item(ActiveSelect(active_list))

class AccessView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select a role to toggle access...", min_values=1, max_values=1)
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        rid = role.id
        mod_roles = bot.data_manager.config.get("mod_roles", [])
        
        if rid in mod_roles:
            mod_roles.remove(rid)
            action = "removed from"
        else:
            mod_roles.append(rid)
            action = "added to"
            
        bot.data_manager.config["mod_roles"] = mod_roles
        await bot.data_manager.save_config()
        
        # Log
        log_embed = make_embed(
            "Moderator Access Updated",
            "> The list of roles with moderation access was changed.",
            kind="info",
            scope=SCOPE_SYSTEM,
            guild=interaction.guild,
        )
        log_embed.add_field(name="Actor", value=format_user_ref(interaction.user), inline=True)
        log_embed.add_field(name="Role", value=f"{role.mention} (`{role.id}`)", inline=True)
        log_embed.add_field(name="Action", value=action.capitalize(), inline=True)
        await send_log(interaction.guild, log_embed)
        
        mentions = [f"<@&{r}>" for r in mod_roles]
        desc = "**Allowed Mod Roles:**\n" + ", ".join(mentions) if mentions else "No specific roles configured (Admins & Mods allowed)."
        
        if interaction.message:
            embed = interaction.message.embeds[0]
            embed.description = f"> {desc}"
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.edit_message(view=self)
            
        await interaction.followup.send(f"Role {role.mention} {action} mod access.", ephemeral=True)

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

class RuleDeleteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(RuleDeleteSelect())

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

class RuleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(RuleSelectForEdit())

def generate_transcript_html(messages, user):
    style = """
    body { background-color: #313338; color: #dbdee1; font-family: "gg sans", "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 0; padding: 20px; }
    .chat-container { max-width: 100%; display: flex; flex-direction: column; }
    .message { display: flex; margin-top: 1rem; padding: 5px; }
    .message:hover { background-color: #2e3035; }
    .message.deleted { background-color: rgba(242, 63, 66, 0.1); border-left: 3px solid #f23f42; }
    .avatar { width: 40px; height: 40px; border-radius: 50%; margin-right: 16px; margin-top: 2px; }
    .content { display: flex; flex-direction: column; width: 100%; }
    .header { display: flex; align-items: center; margin-bottom: 2px; }
    .username { font-weight: 500; color: #f2f3f5; margin-right: 0.25rem; font-size: 1rem; }
    .timestamp { font-size: 0.75rem; color: #949ba4; margin-left: 0.25rem; }
    .msg-content { font-size: 1rem; line-height: 1.375rem; white-space: pre-wrap; color: #dbdee1; }
    .attachment-container { margin-top: 5px; }
    .attachment-img { max-width: 400px; max-height: 300px; border-radius: 8px; cursor: pointer; }
    .deleted-tag { font-size: 0.625rem; color: #f23f42; margin-left: 4px; border: 1px solid #f23f42; border-radius: 3px; padding: 0 4px; vertical-align: middle; }
    .edited-tag { font-size: 0.625rem; color: #949ba4; margin-left: 4px; vertical-align: middle; }
    .channel-ref { font-size: 0.75rem; color: #949ba4; font-weight: bold; margin-bottom: 2px; }
    a { color: #00a8fc; text-decoration: none; }
    a:hover { text-decoration: underline; }
    """
    
    safe_display_name = html.escape(user.display_name)
    html_parts = [
        f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>History - {safe_display_name}</title><style>{style}</style></head><body>',
        f'<div class="chat-container"><h2 style="color:white; border-bottom: 1px solid #4e5058; padding-bottom: 10px;">Chat History: {safe_display_name} ({user.id})</h2>'
    ]

    # messages is Newest -> Oldest. Reverse to show Oldest -> Newest in HTML.
    for m in reversed(messages):
        ts = m["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        content = html.escape(m.get("content", ""))
        if not content: content = "<em>[No Text Content]</em>"
        author_name = html.escape(m.get("author_name", user.display_name))
        author_avatar_url = html.escape(m.get("author_avatar_url", user.display_avatar.url if getattr(user, "display_avatar", None) else ""))

        # Status tags
        tags = ""
        if m.get("deleted"): tags += '<span class="deleted-tag">DELETED</span>'
        if m.get("edited"): tags += '<span class="edited-tag">(edited)</span>'

        # Attachments
        att_html = ""
        if m.get("attachments"):
            att_html += '<div class="attachment-container">'
            for a in m["attachments"]:
                safe_url = html.escape(a["url"])
                safe_filename = html.escape(a["filename"])
                ext = a["filename"].split('.')[-1].lower()
                if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
                    att_html += f'<a href="{safe_url}" target="_blank"><img src="{safe_url}" class="attachment-img" alt="{safe_filename}"></a><br>'
                else:
                    att_html += f'<a href="{safe_url}" target="_blank">📎 {safe_filename}</a><br>'
            att_html += '</div>'

        # Stickers
        if m.get("stickers"):
            att_html += f'<div style="color:#949ba4; font-size:0.8rem;">Stickers: {html.escape(", ".join(m["stickers"]))}</div>'

        div_class = "message deleted" if m.get("deleted") else "message"
        row = f"""
        <div class="{div_class}">
            <img class="avatar" src="{author_avatar_url}" alt="Avatar">
            <div class="content">
                <div class="channel-ref">#{html.escape(str(m['channel_id']))}</div>
                <div class="header">
                    <span class="username">{author_name}</span>
                    <span class="timestamp">{ts}</span>
                    {tags}
                </div>
                <div class="msg-content">{content}</div>
                {att_html}
            </div>
        </div>
        """
        html_parts.append(row)
        
    html_parts.append('</div></body></html>')
    return "\n".join(html_parts)

class RulesDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="List Rules", style=discord.ButtonStyle.primary)
    async def list_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        rules = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        lines = []
        for name, data in rules.items():
            b = format_duration(data['base'])
            e = format_duration(data['escalated'])
            lines.append(f"**{name}**: {b} -> {e}")

        embed = make_embed(
            "Punishment Rules",
            "> Current automated escalation baselines used by the moderation console.",
            kind="info",
            scope=SCOPE_MODERATION,
            guild=interaction.guild,
        )
        embed.add_field(name="Configured Rules", value=truncate_text("\n".join(lines) or "No rules configured.", 4000), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Add Rule", style=discord.ButtonStyle.success)
    async def add_rule(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RuleEditModal()
        modal.title = "Add New Rule"
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Edit Rule", style=discord.ButtonStyle.secondary)
    async def edit_rule(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select rule to edit:", view=RuleSelectView(), ephemeral=True)

    @discord.ui.button(label="Delete Rule", style=discord.ButtonStyle.danger)
    async def delete_rule(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select rule to delete:", view=RuleDeleteView(), ephemeral=True)

def get_mod_cases(mod_id: str) -> list:
    cases = []
    for uid, records in bot.data_manager.punishments.items():
        for r in records:
            if str(r.get("moderator")) == mod_id:
                cases.append((uid, r))
    return cases

def get_staff_stats_embed(target: discord.Member, cases: list, reversals: int) -> discord.Embed:
    total = len(cases)
    
    # Sort cases by timestamp (newest first) for calculations
    sorted_cases = sorted(cases, key=lambda x: x[1].get("timestamp", ""), reverse=True)
    
    action_counter = Counter()
    reasons = Counter()
    timestamps = []

    for uid, r in sorted_cases:
        reasons[r.get("reason", "Unknown")] += 1
        ts_str = r.get("timestamp")
        if ts_str:
            dt = iso_to_dt(ts_str)
            if dt: timestamps.append(dt)

        action_type = r.get("type")
        if not action_type:
            dur = r.get("duration_minutes", 0)
            if dur == -1:
                action_type = "ban"
            elif dur == 0:
                action_type = "warn"
            else:
                action_type = "timeout"
        action_counter[action_type] += 1

    embed = make_embed(
        f"Staff Profile: {target.display_name}",
        "> Moderation performance snapshot based on logged actions and reversals.",
        kind="info",
        scope=SCOPE_ANALYTICS,
        guild=target.guild,
        thumbnail=target.display_avatar.url,
    )
    if target.color != discord.Color.default():
        embed.color = target.color

    joined = discord.utils.format_dt(target.joined_at, "d") if target.joined_at else "Unknown"
    roles_str = truncate_text(", ".join([r.mention for r in target.roles if not r.is_default()][-5:]) or "None", 1024)
    embed.add_field(name="Member", value=format_user_ref(target), inline=True)
    embed.add_field(name="Joined Server", value=joined, inline=True)
    embed.add_field(name="Roles", value=roles_str, inline=False)

    # Activity Overview
    first_action = timestamps[-1] if timestamps else None
    last_action = timestamps[0] if timestamps else None
    
    days_active = (last_action - first_action).days if (first_action and last_action) else 0
    days_active = max(1, days_active)
    
    avg_daily = round(total / days_active, 2) if total > 0 else 0
    reversal_rate = round((reversals / total) * 100, 1) if total > 0 else 0
    
    overview = (
        f"**Total Actions:** `{total}`\n"
        f"**Reversals:** `{reversals}` ({reversal_rate}%)\n"
        f"**Avg Actions/Day:** `{avg_daily}`\n"
        f"**First Action:** {discord.utils.format_dt(first_action, 'd') if first_action else 'N/A'}\n"
        f"**Last Action:** {discord.utils.format_dt(last_action, 'R') if last_action else 'N/A'}"
    )
    now = discord.utils.utcnow()
    embed.add_field(name="Performance Overview", value=f">>> {overview}", inline=False)

    # Recent Activity
    last_24h = sum(1 for t in timestamps if (now - t).days < 1)
    last_7d = sum(1 for t in timestamps if (now - t).days < 7)
    last_30d = sum(1 for t in timestamps if (now - t).days < 30)
    
    recent = (
        f"**24 Hours:** `{last_24h}`\n"
        f"**7 Days:** `{last_7d}`\n"
        f"**30 Days:** `{last_30d}`"
    )
    embed.add_field(name="Recent Activity", value=f">>> {recent}", inline=True)

    # Action Distribution (Visual)
    if total > 0:
        bans = action_counter.get("ban", 0)
        timeouts = action_counter.get("timeout", 0)
        warns = action_counter.get("warn", 0)
        p_bans = bans / total
        p_to = timeouts / total
        p_warn = warns / total
        
        dist_desc = (
            f"**Bans** ({bans})\n`{create_progress_bar(p_bans)}` {round(p_bans*100)}%\n"
            f"**Timeouts** ({timeouts})\n`{create_progress_bar(p_to)}` {round(p_to*100)}%\n"
            f"**Warnings** ({warns})\n`{create_progress_bar(p_warn)}` {round(p_warn*100)}%"
        )
        embed.add_field(name="Action Distribution", value=f">>> {dist_desc}", inline=False)
    else:
        embed.add_field(name="Action Distribution", value="> No data available.", inline=False)

    # Top Reasons
    if reasons:
        top = reasons.most_common(5)
        reason_lines = []
        for r, c in top:
            pct = (c / total) * 100
            reason_lines.append(f"**{truncate_text(r, 60)}**: {c} ({round(pct)}%)")
        embed.add_field(name="Most Common Violations", value=">>> " + "\n".join(reason_lines), inline=False)

    return embed

class ModCasesSelect(discord.ui.Select):
    def __init__(self, cases, guild):
        self.cases = cases
        # Sort by timestamp desc
        self.cases.sort(key=lambda x: x[1].get("timestamp", ""), reverse=True)
        
        options = []
        for i, (uid, rec) in enumerate(self.cases[:25]):
            ts = iso_to_dt(rec.get("timestamp"))
            date_str = ts.strftime("%Y-%m-%d") if ts else "?"
            reason = truncate_text(rec.get("reason", "Unknown"), 60)
            action = rec.get("type") or ("ban" if rec.get("duration_minutes", 0) == -1 else ("warn" if rec.get("duration_minutes", 0) == 0 else "timeout"))

            label = truncate_text(f"{get_case_label(rec, i + 1)} • {action.title()}", 100)
            member = guild.get_member(int(uid)) if guild else None
            user_display = member.name if member else uid
            desc = truncate_text(f"{date_str} • {user_display} • {reason}", 100)
            options.append(discord.SelectOption(label=label, description=desc, value=str(i)))
            
        if not options:
            options.append(discord.SelectOption(label="No cases found", value="-1"))
            
        super().__init__(placeholder="Select a case to view details...", min_values=1, max_values=1, options=options, disabled=not options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "-1":
            return
            
        idx = int(self.values[0])
        uid, rec = self.cases[idx]

        case_label = get_case_label(rec, idx + 1)
        embed = make_embed(
            f"{case_label} Details",
            "> Full case metadata for this moderator-issued action.",
            kind="warning",
            scope=SCOPE_ANALYTICS,
            guild=interaction.guild,
        )

        # User Info
        user_obj = interaction.guild.get_member(int(uid))
        user_name = user_obj.name if user_obj else "Unknown (Left Server)"
        user_field = f"**Name:** {user_name}\n**Mention:** <@{uid}>\n**ID:** `{uid}`"
        embed.add_field(name="User", value=f"> {user_field.replace(chr(10), chr(10)+'> ')}", inline=True)
        
        # Moderator Info
        mod_id = rec.get("moderator")
        mod_field = f"**Mention:** <@{mod_id}>\n**ID:** `{mod_id}`"
        embed.add_field(name="Moderator", value=f"> {mod_field.replace(chr(10), chr(10)+'> ')}", inline=True)
        
        # Action Info
        mins = rec.get("duration_minutes", 0)
        if mins == -1:
            type_str = "Ban"
            dur_str = "Ban"
        elif mins == 0:
            type_str = "Warning"
            dur_str = "N/A"
        else:
            type_str = "Timeout"
            dur_str = format_duration(mins)
            
        action_field = f"**Type:** {type_str}\n**Duration:** {dur_str}"
        embed.add_field(name="Action", value=f"> {action_field.replace(chr(10), chr(10)+'> ')}", inline=True)
        embed.add_field(name="Status", value="> Active" if is_record_active(rec) else "> Closed", inline=True)
        
        # Timestamps
        ts = iso_to_dt(rec.get("timestamp"))
        if ts:
            ts_field = f"**Issued:** {discord.utils.format_dt(ts, 'F')} ({discord.utils.format_dt(ts, 'R')})"
            if mins > 0:
                expiry = ts + timedelta(minutes=mins)
                ts_field += f"\n**Expired:** {discord.utils.format_dt(expiry, 'F')}"
            embed.add_field(name="Timeline", value=f"> {ts_field.replace(chr(10), chr(10)+'> ')}", inline=False)
            
        # Reason & Notes
        embed.add_field(name="Violation Reason", value=f"> {truncate_text(rec.get('reason', 'Unknown'), 1024)}", inline=False)
        
        note = truncate_text(str(rec.get("note") or "").strip(), 1000)
        if note:
            embed.add_field(name="Internal Note", value=format_log_quote(note, limit=1000), inline=False)
        
        user_msg = rec.get("user_msg")
        if user_msg:
            embed.add_field(name="Message to User", value=format_log_quote(user_msg, limit=1000), inline=False)
            
        is_esc = rec.get("escalated", False)
        if is_esc:
            embed.add_field(name="Escalated", value="Yes", inline=True)
        
        # Keep the view (which has this select) so they can pick another case
        await interaction.response.edit_message(embed=embed, view=self.view)

class StaffProfileView(discord.ui.View):
    def __init__(self, target, cases, staff_members, directory_embed, stats_embed, guild):
        super().__init__(timeout=180)
        self.target = target
        self.cases = cases
        self.staff_members = staff_members
        self.directory_embed = directory_embed
        self.stats_embed = stats_embed
        
        self.add_item(ModCasesSelect(cases, guild))
        
        if not staff_members or not directory_embed:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.label == "Back to Directory":
                    self.remove_item(child)
                    break

    @discord.ui.button(label="Back to Stats", style=discord.ButtonStyle.secondary, row=1)
    async def back_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.stats_embed, view=self)

    @discord.ui.button(label="Back to Directory", style=discord.ButtonStyle.primary, row=1)
    async def back_dir(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = StaffView(self.staff_members)
        await interaction.response.edit_message(embed=self.directory_embed, view=view)

class StaffSelect(discord.ui.Select):
    def __init__(self, staff_members):
        self.staff_members = staff_members
        options = []
        for m in staff_members[:25]:
            options.append(discord.SelectOption(label=m.display_name, value=str(m.id)))
        super().__init__(placeholder="Select a staff member to view stats...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        target = interaction.guild.get_member(target_id)
        if target:
            uid = str(target.id)
            cases = get_mod_cases(uid)
            reversals = bot.data_manager.mod_stats.get("reversals", {}).get(uid, 0)
            
            stats_embed = get_staff_stats_embed(target, cases, reversals)
            directory_embed = interaction.message.embeds[0]
            
            view = StaffProfileView(target, cases, self.staff_members, directory_embed, stats_embed, interaction.guild)
            await interaction.response.edit_message(embed=stats_embed, view=view)
        else:
            await interaction.response.send_message("User not found.", ephemeral=True)

class StaffView(discord.ui.View):
    def __init__(self, staff_members):
        super().__init__(timeout=180)
        self.add_item(StaffSelect(staff_members))

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
        if "debug" not in bot.data_manager.config: bot.data_manager.config["debug"] = {}
        current = bot.data_manager.config["debug"].get("bypass_boost", False)
        bot.data_manager.config["debug"]["bypass_boost"] = not current
        await bot.data_manager.save_config()
        embed = build_test_env_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Toggle Cooldown Bypass", style=discord.ButtonStyle.primary)
    async def toggle_cooldown(self, interaction: discord.Interaction, button: discord.ui.Button):
        if "debug" not in bot.data_manager.config: bot.data_manager.config["debug"] = {}
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



class CasesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(CasesCog(bot))
