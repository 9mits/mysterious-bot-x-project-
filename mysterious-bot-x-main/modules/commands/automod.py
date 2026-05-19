# modules/commands/automod.py
# Native and smart AutoMod logic, policy views, report views, and /automod command.

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
    DEFAULT_SPAM_ROLE_ID,
    EMBED_PALETTE,
    SCOPE_ANALYTICS,
    SCOPE_MODERATION,
    SCOPE_SUPPORT,
    SCOPE_SYSTEM,
    TOKEN_ENV_VARS,
)
from modules.models import CaseNote
from modules.services import (
    DEFAULT_CANNED_REPLIES,
    DEFAULT_NATIVE_AUTOMOD_SETTINGS,
    get_feature_flag,
    get_escalation_steps,
    get_native_automod_settings,
    has_capability,
    normalize_case_record,
    resolve_escalation_duration,
    resolve_native_automod_policy,
    sanitize_evidence_links,
    validate_guild_configuration,
)
from modules.context import abuse_system, bot, tree
from modules.utils import iso_to_dt, now_iso, parse_duration_str
from .shared import (
    logger,
    DB_DIR,
    PUNISHMENTS_FILE,
    truncate_text,
    format_duration,
    format_log_quote,
    format_plain_log_block,
    format_reason_value,
    format_log_notes,
    make_action_log_embed,
    normalize_log_embed,
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
    send_punishment_log,
    send_automod_log,
    has_permission_capability,
    respond_with_error,
    is_staff_member,
    is_staff,
    resolve_member,
    get_valid_duration,
    build_automod_dashboard_embed,
    build_feature_flags_embed,
    build_escalation_matrix_embed,
    build_canned_replies_embed,
    build_setup_validation_embed,
    get_punishment_log_channel_id,
    build_modmail_panel_embed,
    build_setup_dashboard_embed,
    build_modmail_settings_embed,
    build_config_dashboard_embed,
    build_rules_dashboard_embed,
    build_role_landing_embed,
    build_status_embed,
    handle_abuse,
    punish_rogue_mod,
    get_feature_flag_name,
)
from .cases import (
    get_case_id,
    get_case_label,
    get_record_expiry,
    is_record_active,
    describe_punishment_record,
    build_punishment_execution_log_embed,
    record_case_reversal_stats,
    get_active_records_for_user,
)

from .roles import AppealView

AUTOMOD_PUNISHMENT_OPTIONS = [
    ("warn", "Warn Only"),
    ("timeout", "Timeout"),
    ("kick", "Kick"),
    ("ban", "Ban"),
]
AUTOMOD_THRESHOLD_PRESETS = [1, 2, 3, 4, 5, 6, 8, 10, 12]
AUTOMOD_WINDOW_PRESETS = [15, 60, 120, 360, 720, 1440, 2880, 4320, 10080]
AUTOMOD_TIMEOUT_PRESETS = [10, 30, 60, 120, 180, 720, 1440, 2880, 10080, 40320]
SMART_DUPLICATE_THRESHOLD_PRESETS = [2, 3, 4, 5, 6, 8, 10]
SMART_DUPLICATE_WINDOW_PRESETS = [10, 15, 20, 30, 45, 60, 120]
SMART_CAPS_PERCENT_PRESETS = [50, 60, 70, 75, 80, 90]
SMART_CAPS_LENGTH_PRESETS = [5, 8, 12, 16, 24, 32]
AUTOMOD_REPORT_RESPONSE_PRESETS = {
    "fixed": {
        "label": "We fixed the AutoMod",
        "description": "Tell the user the AutoMod setup was corrected.",
        "message": "We reviewed your report and fixed the AutoMod setup for that warning. Thanks for reporting it.",
        "status": "Resolved - AutoMod Updated",
        "kind": "success",
    },
    "justified": {
        "label": "Warn was justified",
        "description": "Tell the user the AutoMod warning will stand.",
        "message": "We reviewed your report and the AutoMod warning was justified, so it will remain as-is.",
        "status": "Reviewed - Warning Stands",
        "kind": "warning",
    },
    "removed": {
        "label": "Warn was removed",
        "description": "Tell the user the warning was treated as a false positive.",
        "message": "We reviewed your report and treated this as a false positive. The warning has been cleared on our side.",
        "status": "Resolved - False Positive",
        "kind": "success",
    },
    "custom": {
        "label": "Custom response",
        "description": "Write and send a custom staff response.",
        "message": "",
        "status": "Staff Replied",
        "kind": "info",
    },
}
SMART_AUTOMOD_DEFAULTS = {
    "duplicate_window_seconds": 20,
    "duplicate_threshold": 4,
    "max_caps_ratio": 0.75,
    "caps_min_length": 12,
    "blocked_patterns": [],
    "exempt_channels": [],
    "exempt_roles": [],
}


def format_minutes_interval(minutes: int) -> str:
    minutes = max(1, int(minutes or 1))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if minutes < 1440:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = minutes // 1440
    return f"{days} day{'s' if days != 1 else ''}"


def format_seconds_interval(seconds: int) -> str:
    seconds = max(1, int(seconds or 1))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    return format_minutes_interval(minutes)


def format_compact_minutes_input(minutes: int) -> str:
    minutes = max(1, int(minutes or 1))
    if minutes % 10080 == 0:
        return f"{minutes // 10080}w"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def parse_positive_integer_input(raw_value: str, *, field_name: str, minimum: int = 1, maximum: int = 999) -> int:
    text = str(raw_value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    if not text.isdigit():
        raise ValueError(f"{field_name} must be a whole number.")
    value = int(text)
    if value < minimum or value > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")
    return value


def parse_minutes_input(raw_value: str, *, field_name: str, minimum: int = 1, maximum: int = 40320) -> int:
    text = str(raw_value or "").strip().lower()
    if not text:
        raise ValueError(f"{field_name} is required.")

    match = re.fullmatch(r"(\d+)\s*([a-z]+)?", text)
    if not match:
        raise ValueError(f"{field_name} must look like 30m, 12h, 2d, or 1w.")

    amount = int(match.group(1))
    unit = (match.group(2) or "m").lower()

    if unit in {"m", "min", "mins", "minute", "minutes"}:
        minutes = amount
    elif unit in {"h", "hr", "hrs", "hour", "hours"}:
        minutes = amount * 60
    elif unit in {"d", "day", "days"}:
        minutes = amount * 1440
    elif unit in {"w", "wk", "wks", "week", "weeks"}:
        minutes = amount * 10080
    else:
        raise ValueError(f"{field_name} must use m, h, d, or w.")

    if minutes < minimum or minutes > maximum:
        raise ValueError(f"{field_name} must be between {format_minutes_interval(minimum)} and {format_minutes_interval(maximum)}.")
    return minutes


def parse_automod_punishment_input(raw_value: str, *, field_name: str = "Action") -> str:
    text = str(raw_value or "").strip().lower()
    mapping = {
        "warn": "warn",
        "warning": "warn",
        "timeout": "timeout",
        "mute": "timeout",
        "kick": "kick",
        "ban": "ban",
    }
    punishment_type = mapping.get(text)
    if punishment_type is None:
        raise ValueError(f"{field_name} must be one of: warn, timeout, kick, or ban.")
    return punishment_type


def build_numeric_select_options(current: int, presets: List[int], formatter) -> List[discord.SelectOption]:
    values = []
    for value in presets:
        if value not in values:
            values.append(value)
    if current not in values:
        values.append(current)
    return [
        discord.SelectOption(label=truncate_text(formatter(value), 100), value=str(value), default=value == current)
        for value in values[:25]
    ]


def get_smart_automod_settings() -> dict:
    current = bot.data_manager.config.get("smart_automod", {})
    normalized = {
        "duplicate_window_seconds": max(5, int(current.get("duplicate_window_seconds", SMART_AUTOMOD_DEFAULTS["duplicate_window_seconds"]) or SMART_AUTOMOD_DEFAULTS["duplicate_window_seconds"])),
        "duplicate_threshold": max(2, int(current.get("duplicate_threshold", SMART_AUTOMOD_DEFAULTS["duplicate_threshold"]) or SMART_AUTOMOD_DEFAULTS["duplicate_threshold"])),
        "max_caps_ratio": max(0.1, min(1.0, float(current.get("max_caps_ratio", SMART_AUTOMOD_DEFAULTS["max_caps_ratio"]) or SMART_AUTOMOD_DEFAULTS["max_caps_ratio"]))),
        "caps_min_length": max(3, int(current.get("caps_min_length", SMART_AUTOMOD_DEFAULTS["caps_min_length"]) or SMART_AUTOMOD_DEFAULTS["caps_min_length"])),
        "blocked_patterns": [str(item).strip()[:80] for item in current.get("blocked_patterns", []) if str(item).strip()][:50],
        "exempt_channels": [int(item) for item in current.get("exempt_channels", []) if isinstance(item, int) or str(item).isdigit()],
        "exempt_roles": [int(item) for item in current.get("exempt_roles", []) if isinstance(item, int) or str(item).isdigit()],
    }
    return normalized


def store_native_automod_settings(settings: dict) -> dict:
    normalized = get_native_automod_settings({"native_automod": settings})
    bot.data_manager.config["native_automod"] = normalized
    return normalized


def store_smart_automod_settings(settings: dict) -> dict:
    normalized = get_smart_automod_settings()
    normalized.update({
        "duplicate_window_seconds": max(5, int(settings.get("duplicate_window_seconds", normalized["duplicate_window_seconds"]) or normalized["duplicate_window_seconds"])),
        "duplicate_threshold": max(2, int(settings.get("duplicate_threshold", normalized["duplicate_threshold"]) or normalized["duplicate_threshold"])),
        "max_caps_ratio": max(0.1, min(1.0, float(settings.get("max_caps_ratio", normalized["max_caps_ratio"]) or normalized["max_caps_ratio"]))),
        "caps_min_length": max(3, int(settings.get("caps_min_length", normalized["caps_min_length"]) or normalized["caps_min_length"])),
        "blocked_patterns": [str(item).strip()[:80] for item in settings.get("blocked_patterns", normalized["blocked_patterns"]) if str(item).strip()][:50],
        "exempt_channels": [int(item) for item in settings.get("exempt_channels", normalized["exempt_channels"]) if isinstance(item, int) or str(item).isdigit()],
        "exempt_roles": [int(item) for item in settings.get("exempt_roles", normalized["exempt_roles"]) if isinstance(item, int) or str(item).isdigit()],
    })
    bot.data_manager.config["smart_automod"] = normalized
    return normalized


def format_automod_punishment_label(policy: dict) -> str:
    punishment_type = str(policy.get("punishment_type", "warn") or "warn").lower()
    if punishment_type == "timeout":
        return f"Timeout ({format_duration(int(policy.get('duration_minutes', 60) or 60))})"
    if punishment_type == "ban":
        return "Ban"
    if punishment_type == "kick":
        return "Kick"
    return "Warn Only"


def get_automod_report_preset(key: str) -> dict:
    return AUTOMOD_REPORT_RESPONSE_PRESETS.get(key, AUTOMOD_REPORT_RESPONSE_PRESETS["custom"])


def build_default_native_automod_policy() -> dict:
    return {
        "enabled": False,
        "reason_template": str(DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"]["reason_template"]),
        "steps": [],
    }


def get_native_automod_policy_steps(policy: Optional[dict]) -> List[dict]:
    if not isinstance(policy, dict):
        return []
    steps = []
    for payload in policy.get("steps", []):
        if not isinstance(payload, dict):
            continue
        punishment_type = str(payload.get("punishment_type", "warn") or "warn").lower()
        threshold = max(1, int(payload.get("threshold", 1) or 1))
        window_minutes = max(1, int(payload.get("window_minutes", 1440) or 1440))
        duration_minutes = int(payload.get("duration_minutes", 0) or 0)
        if punishment_type == "timeout":
            duration_minutes = max(1, min(40320, duration_minutes or 60))
        elif punishment_type == "ban":
            duration_minutes = -1
        else:
            duration_minutes = 0
        steps.append({
            "threshold": threshold,
            "window_minutes": window_minutes,
            "duration_minutes": duration_minutes,
            "punishment_type": punishment_type,
        })
    steps.sort(key=lambda step: (int(step.get("threshold", 1)), int(step.get("window_minutes", 1)), str(step.get("punishment_type", "warn"))))
    return steps[:5]


def build_default_native_automod_step(existing_steps: Optional[List[dict]] = None) -> dict:
    steps = get_native_automod_policy_steps({"steps": existing_steps or []})
    if steps:
        last_step = steps[-1]
        threshold = min(25, max(1, int(last_step.get("threshold", 3) or 3) + 1))
        window_minutes = int(last_step.get("window_minutes", 1440) or 1440)
    else:
        threshold = 3
        window_minutes = 1440
    return {
        "threshold": threshold,
        "window_minutes": window_minutes,
        "duration_minutes": 60,
        "punishment_type": "timeout",
    }


def format_native_automod_step_summary(step: dict) -> str:
    threshold = int(step.get("threshold", 1) or 1)
    return f"{threshold} warning{'s' if threshold != 1 else ''} in {format_minutes_interval(int(step.get('window_minutes', 1440) or 1440))} -> {format_automod_punishment_label(step)}"


def get_native_rule_override(settings: dict, rule: discord.AutoModRule) -> Tuple[str, dict, bool]:
    overrides = settings.get("rule_overrides", {})
    for candidate in (str(rule.id), rule.name):
        if candidate in overrides:
            return candidate, overrides[candidate], True
    return str(rule.id), build_default_native_automod_policy(), False


def render_id_mentions(ids: List[int], *, prefix: str, limit: int = 6) -> str:
    cleaned = [int(value) for value in ids if isinstance(value, int) or str(value).isdigit()]
    if not cleaned:
        return "None"
    rendered = [f"<{prefix}{value}>" for value in cleaned[:limit]]
    if len(cleaned) > limit:
        rendered.append(f"+{len(cleaned) - limit} more")
    return ", ".join(rendered)


def build_automod_bridge_embed(guild: discord.Guild) -> discord.Embed:
    settings = get_native_automod_settings(bot.data_manager.config)
    embed = make_embed(
        "AutoMod Bot Response",
        "> Control what the bot does after Discord AutoMod triggers.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(name="Bot Response", value="On" if settings.get("enabled", True) else "Off", inline=True)
    embed.add_field(name="User DMs", value="On" if settings.get("warning_dm_enabled", True) else "Off", inline=True)
    embed.add_field(name="False-Positive Report", value="On" if settings.get("report_button_enabled", True) else "Off", inline=True)
    embed.add_field(
        name="What Happens",
        value=join_lines([
            "Discord AutoMod blocks or flags a message.",
            "The bot can DM the user and log the event.",
            "Any automatic punishment must be turned on per rule.",
            "The report button lets the user ask staff to review the warning.",
        ]),
        inline=False,
    )
    return embed


def build_automod_policy_embed(
    guild: discord.Guild,
    policy: dict,
    *,
    title: str,
    description: str,
    rule: Optional[discord.AutoModRule] = None,
    using_override: bool = False,
    selected_step_index: Optional[int] = None,
) -> discord.Embed:
    steps = get_native_automod_policy_steps(policy)
    embed = make_embed(title, description, kind="warning", scope=SCOPE_MODERATION, guild=guild)
    if rule is not None:
        embed.add_field(name="Rule", value=rule.name, inline=True)
        embed.add_field(name="Discord Actions", value=describe_automod_rule_actions(rule), inline=True)
    embed.add_field(name="Auto Punish", value="On" if policy.get("enabled") and steps else "Off", inline=True)
    embed.add_field(name="Steps", value=str(len(steps)), inline=True)
    if steps:
        step_lines = [f"{index + 1}. {format_native_automod_step_summary(step)}" for index, step in enumerate(steps[:5])]
        embed.add_field(name="Escalation Ladder", value=join_lines(step_lines, fallback="No punishment steps set yet."), inline=False)
    else:
        embed.add_field(name="Escalation Ladder", value="No punishment steps set yet.", inline=False)
    if steps and selected_step_index is not None and 0 <= selected_step_index < len(steps):
        selected_step = steps[selected_step_index]
        selected_lines = [
            f"Step: {selected_step_index + 1}",
            f"Warnings: {selected_step.get('threshold', 1)}",
            f"Window: {format_minutes_interval(int(selected_step.get('window_minutes', 1440) or 1440))}",
            f"Action: {format_automod_punishment_label(selected_step)}",
        ]
        if str(selected_step.get("punishment_type", "warn")).lower() == "timeout":
            selected_lines.append(f"Timeout: {format_minutes_interval(int(selected_step.get('duration_minutes', 60) or 60))}")
        embed.add_field(name="Selected Step", value=join_lines(selected_lines), inline=False)
    embed.add_field(name="Reason Template", value=format_log_quote(policy.get("reason_template", "Repeated native AutoMod violations"), limit=500), inline=False)
    return embed


def build_automod_immunity_embed(guild: discord.Guild) -> discord.Embed:
    settings = get_native_automod_settings(bot.data_manager.config)
    embed = make_embed(
        "AutoMod Immunity",
        "> Choose who should be ignored by the native AutoMod bridge follow-up.",
        kind="info",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(name="Users", value=render_id_mentions(settings.get("immunity_users", []), prefix="@"), inline=False)
    embed.add_field(name="Roles", value=render_id_mentions(settings.get("immunity_roles", []), prefix="@&"), inline=False)
    embed.add_field(name="Channels", value=render_id_mentions(settings.get("immunity_channels", []), prefix="#"), inline=False)
    return embed


def build_automod_routing_embed(guild: discord.Guild) -> discord.Embed:
    embed = make_embed(
        "AutoMod Log Channels",
        "> Use the selectors below to set or clear where the bot sends AutoMod logs and user reports.",
        kind="info",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(
        name="Log Channel",
        value=f"<#{bot.data_manager.config.get('automod_log_channel_id', 0)}>" if bot.data_manager.config.get("automod_log_channel_id") else "Uses punishment logs or the native alert channel fallback",
        inline=False,
    )
    embed.add_field(
        name="Report Channel",
        value=f"<#{bot.data_manager.config.get('automod_report_channel_id', 0)}>" if bot.data_manager.config.get("automod_report_channel_id") else "Uses the appeal log channel or punishment logs",
        inline=False,
    )
    return embed


def build_smart_automod_embed(guild: discord.Guild) -> discord.Embed:
    settings = get_smart_automod_settings()
    embed = make_embed(
        "Smart AutoMod Filters",
        "> Configure the bot's own duplicate, caps, and blocked-pattern checks.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(name="Duplicate Window", value=f"{settings.get('duplicate_threshold', 4)} messages in {settings.get('duplicate_window_seconds', 20)} seconds", inline=True)
    embed.add_field(name="Caps Rule", value=f"{round(float(settings.get('max_caps_ratio', 0.75)) * 100)}% after {settings.get('caps_min_length', 12)} chars", inline=True)
    embed.add_field(name="Blocked Patterns", value=str(len(settings.get("blocked_patterns", []))), inline=True)
    embed.add_field(
        name="Current Pattern Preview",
        value=join_lines([f"- `{pattern}`" for pattern in settings.get("blocked_patterns", [])[:8]], fallback="No patterns configured."),
        inline=False,
    )
    embed.add_field(name="Exempt Roles", value=render_id_mentions(settings.get("exempt_roles", []), prefix="@&"), inline=False)
    embed.add_field(name="Exempt Channels", value=render_id_mentions(settings.get("exempt_channels", []), prefix="#"), inline=False)
    return embed


def build_automod_rule_browser_embed(guild: discord.Guild, rules: List[discord.AutoModRule]) -> discord.Embed:
    settings = get_native_automod_settings(bot.data_manager.config)
    configured_rules = sum(1 for payload in settings.get("rule_overrides", {}).values() if get_native_automod_policy_steps(payload))
    embed = make_embed(
        "Native AutoMod Rules",
        "> Pick one Discord AutoMod rule below to set up that rule's automatic punishment steps.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    if not rules:
        embed.add_field(name="Rules", value="No native Discord AutoMod rules were found in this server.", inline=False)
        return embed
    embed.add_field(name="Native Rules", value=str(len(rules)), inline=True)
    embed.add_field(name="Rules Configured", value=str(configured_rules), inline=True)
    for rule in rules[:6]:
        _, policy, using_override = get_native_rule_override(settings, rule)
        steps = get_native_automod_policy_steps(policy)
        embed.add_field(
            name=f"{'On' if rule.enabled else 'Off'} • {rule.name}",
            value=join_lines([
                f"Discord: {describe_automod_rule_actions(rule)}",
                f"Auto Punish: {'On' if policy.get('enabled') and steps else 'Off'}",
                f"Steps: {len(steps)}",
                (f"Last Step: {format_automod_punishment_label(steps[-1])}" if steps else "No steps set"),
            ]),
            inline=False,
        )
    return embed


def describe_automod_rule_trigger(rule: discord.AutoModRule) -> str:
    trigger = rule.trigger
    if trigger.type == discord.AutoModRuleTriggerType.keyword:
        keywords = ", ".join(f"`{truncate_text(value, 20)}`" for value in trigger.keyword_filter[:4]) or "No keywords"
        regexes = ", ".join(f"`{truncate_text(value, 20)}`" for value in trigger.regex_patterns[:2])
        details = [f"Keywords: {keywords}"]
        if regexes:
            details.append(f"Regex: {regexes}")
        return join_lines(details)
    if trigger.type == discord.AutoModRuleTriggerType.keyword_preset:
        presets = []
        if trigger.presets.profanity:
            presets.append("Profanity")
        if trigger.presets.sexual_content:
            presets.append("Sexual Content")
        if trigger.presets.slurs:
            presets.append("Slurs")
        return ", ".join(presets) or "Preset Rule"
    if trigger.type == discord.AutoModRuleTriggerType.mention_spam:
        raid = "On" if trigger.mention_raid_protection else "Off"
        return f"Mention Limit: {trigger.mention_limit or 0} • Raid Protection: {raid}"
    if trigger.type == discord.AutoModRuleTriggerType.spam:
        return "Spam detection"
    return trigger.type.name.replace('_', ' ').title()


def describe_automod_rule_actions(rule: discord.AutoModRule) -> str:
    parts = []
    for action in rule.actions:
        if action.type == discord.AutoModRuleActionType.block_message:
            parts.append(f"Block message{' + custom notice' if action.custom_message else ''}")
        elif action.type == discord.AutoModRuleActionType.send_alert_message:
            parts.append(f"Send alert to <#{action.channel_id}>")
        elif action.type == discord.AutoModRuleActionType.timeout:
            minutes = int(action.duration.total_seconds() // 60) if action.duration else 0
            parts.append(f"Timeout for {format_duration(minutes)}")
        elif action.type == discord.AutoModRuleActionType.block_member_interactions:
            parts.append("Block member interactions")
    return ", ".join(parts) or "No actions"


def serialize_automod_rule(rule: discord.AutoModRule) -> dict:
    trigger = rule.trigger
    presets = []
    if trigger.presets.profanity:
        presets.append("profanity")
    if trigger.presets.sexual_content:
        presets.append("sexual_content")
    if trigger.presets.slurs:
        presets.append("slurs")

    payload = {
        "name": rule.name,
        "enabled": rule.enabled,
        "trigger_type": rule.trigger.type.name,
        "keyword_filter": trigger.keyword_filter,
        "regex_patterns": trigger.regex_patterns,
        "allow_list": trigger.allow_list,
        "mention_limit": trigger.mention_limit,
        "mention_raid_protection": trigger.mention_raid_protection,
        "presets": presets,
        "actions": [],
        "exempt_roles": list(rule.exempt_role_ids),
        "exempt_channels": list(rule.exempt_channel_ids),
    }
    for action in rule.actions:
        action_payload = {"type": action.type.name}
        if action.custom_message:
            action_payload["custom_message"] = action.custom_message
        if action.channel_id:
            action_payload["channel_id"] = action.channel_id
        if action.duration:
            action_payload["duration_minutes"] = int(action.duration.total_seconds() // 60)
        payload["actions"].append(action_payload)
    return payload


def build_automod_trigger_from_payload(payload: dict, existing_type: Optional[discord.AutoModRuleTriggerType] = None) -> discord.AutoModTrigger:
    trigger_name = str(payload.get("trigger_type") or (existing_type.name if existing_type else "keyword")).lower()
    trigger_type = discord.AutoModRuleTriggerType[trigger_name]
    if trigger_type == discord.AutoModRuleTriggerType.keyword:
        return discord.AutoModTrigger(
            type=trigger_type,
            keyword_filter=[str(v) for v in payload.get("keyword_filter", []) if str(v).strip()],
            regex_patterns=[str(v) for v in payload.get("regex_patterns", []) if str(v).strip()],
            allow_list=[str(v) for v in payload.get("allow_list", []) if str(v).strip()],
        )
    if trigger_type == discord.AutoModRuleTriggerType.keyword_preset:
        presets = discord.AutoModPresets.none()
        for name in payload.get("presets", []):
            if name == "profanity":
                presets.profanity = True
            elif name == "sexual_content":
                presets.sexual_content = True
            elif name == "slurs":
                presets.slurs = True
        return discord.AutoModTrigger(type=trigger_type, presets=presets, allow_list=[str(v) for v in payload.get("allow_list", []) if str(v).strip()])
    if trigger_type == discord.AutoModRuleTriggerType.mention_spam:
        return discord.AutoModTrigger(
            type=trigger_type,
            mention_limit=max(1, min(50, int(payload.get("mention_limit", 5) or 5))),
            mention_raid_protection=bool(payload.get("mention_raid_protection", False)),
        )
    return discord.AutoModTrigger(type=trigger_type)


def build_automod_actions_from_payload(payload: dict, guild: discord.Guild) -> List[discord.AutoModRuleAction]:
    actions: List[discord.AutoModRuleAction] = []
    for action_payload in payload.get("actions", []):
        if not isinstance(action_payload, dict):
            continue
        action_type = str(action_payload.get("type", "block_message")).lower()
        if action_type == "send_alert_message":
            channel_id = action_payload.get("channel_id") or bot.data_manager.config.get("automod_log_channel_id") or get_punishment_log_channel_id()
            if channel_id:
                actions.append(discord.AutoModRuleAction(channel_id=int(channel_id)))
        elif action_type == "timeout":
            duration_minutes = max(1, min(40320, int(action_payload.get("duration_minutes", 60) or 60)))
            actions.append(discord.AutoModRuleAction(duration=timedelta(minutes=duration_minutes)))
        elif action_type == "block_member_interactions":
            actions.append(discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_member_interactions))
        else:
            actions.append(discord.AutoModRuleAction(custom_message=str(action_payload.get("custom_message") or "This message was blocked by server AutoMod.")))
    if not actions:
        actions.append(discord.AutoModRuleAction(custom_message="This message was blocked by server AutoMod."))
        alert_channel_id = bot.data_manager.config.get("automod_log_channel_id") or get_punishment_log_channel_id()
        if alert_channel_id:
            actions.append(discord.AutoModRuleAction(channel_id=int(alert_channel_id)))
    return actions


async def fetch_native_automod_rules(guild: discord.Guild) -> List[discord.AutoModRule]:
    return await guild.fetch_automod_rules()


def build_native_automod_rules_embed(guild: discord.Guild, rules: List[discord.AutoModRule]) -> discord.Embed:
    embed = make_embed(
        "Native AutoMod Rules",
        "> Discord's built-in AutoMod rules currently configured for this server.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    if not rules:
        embed.add_field(name="Rules", value="No native AutoMod rules are configured yet.", inline=False)
        return embed
    embed.add_field(name="Total Rules", value=str(len(rules)), inline=True)
    embed.add_field(name="Enabled", value=str(sum(1 for rule in rules if rule.enabled)), inline=True)
    for rule in rules[:10]:
        embed.add_field(
            name=f"{'On' if rule.enabled else 'Off'} • {rule.name}",
            value=join_lines([
                f"Trigger: {describe_automod_rule_trigger(rule)}",
                f"Actions: {describe_automod_rule_actions(rule)}",
                f"Exempt Roles: {len(rule.exempt_role_ids)} • Exempt Channels: {len(rule.exempt_channel_ids)}",
            ]),
            inline=False,
        )
    return embed


def build_native_automod_rule_detail_embed(guild: discord.Guild, rule: discord.AutoModRule) -> discord.Embed:
    embed = make_embed(
        f"AutoMod Rule: {rule.name}",
        "> Detailed view of one Discord native AutoMod rule.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(name="Target", value=rule.name, inline=True)
    embed.add_field(name="Reason", value=format_reason_value(rule.trigger.type.name.replace('_', ' ').title(), limit=300), inline=False)
    embed.add_field(name="Trigger", value=describe_automod_rule_trigger(rule), inline=False)
    embed.add_field(name="Actions", value=describe_automod_rule_actions(rule), inline=False)
    embed.add_field(name="Enabled", value="Yes" if rule.enabled else "No", inline=True)
    embed.add_field(name="Rule ID", value=str(rule.id), inline=True)
    embed.add_field(name="Exempt Roles", value=", ".join(f"<@&{rid}>" for rid in rule.exempt_role_ids) or "None", inline=False)
    embed.add_field(name="Exempt Channels", value=", ".join(f"<#{cid}>" for cid in rule.exempt_channel_ids) or "None", inline=False)
    return embed


def get_native_automod_stats_bucket(user_id: int) -> dict:
    store = bot.data_manager.mod_stats.setdefault("native_automod", {})
    if not isinstance(store, dict):
        store = {}
        bot.data_manager.mod_stats["native_automod"] = store
    bucket = store.setdefault(str(user_id), {"events": [], "applied_steps": []})
    if not isinstance(bucket, dict):
        bucket = {"events": [], "applied_steps": []}
        store[str(user_id)] = bucket
    events = bucket.setdefault("events", [])
    if not isinstance(events, list):
        bucket["events"] = []
    applied_steps = bucket.setdefault("applied_steps", [])
    if not isinstance(applied_steps, list):
        bucket["applied_steps"] = []
    return bucket


def prune_native_automod_bucket(bucket: dict, *, now_value: Optional[datetime] = None) -> None:
    now_value = now_value or discord.utils.utcnow()

    fresh_events = []
    for event in bucket.get("events", []):
        dt = iso_to_dt(event.get("timestamp")) if isinstance(event, dict) else None
        if dt and now_value - dt <= timedelta(days=30):
            fresh_events.append(event)
    bucket["events"] = fresh_events[-100:]

    fresh_steps = []
    for record in bucket.get("applied_steps", []):
        dt = iso_to_dt(record.get("timestamp")) if isinstance(record, dict) else None
        if dt and now_value - dt <= timedelta(days=30):
            fresh_steps.append(record)
    bucket["applied_steps"] = fresh_steps[-100:]


def record_native_automod_event(*, user_id: int, rule_id: int, rule_name: str, content: str, matched_keyword: Optional[str]) -> None:
    bucket = get_native_automod_stats_bucket(user_id)
    now_value = discord.utils.utcnow()
    prune_native_automod_bucket(bucket, now_value=now_value)
    events = list(bucket.get("events", []))
    events.append({
        "timestamp": now_iso(),
        "rule_id": int(rule_id),
        "rule_name": rule_name,
        "content": truncate_text(content, 500),
        "matched_keyword": matched_keyword,
    })
    bucket["events"] = events[-100:]


def count_recent_native_automod_hits(*, user_id: int, rule_id: int, rule_name: str, window_minutes: int) -> int:
    bucket = get_native_automod_stats_bucket(user_id)
    prune_native_automod_bucket(bucket)
    cutoff = discord.utils.utcnow() - timedelta(minutes=max(1, window_minutes))
    count = 0
    for event in bucket.get("events", []):
        if not isinstance(event, dict):
            continue
        dt = iso_to_dt(event.get("timestamp"))
        if not dt or dt < cutoff:
            continue
        event_rule_id = event.get("rule_id")
        event_rule_name = str(event.get("rule_name", ""))
        if str(event_rule_id) == str(rule_id) or event_rule_name == rule_name:
            count += 1
    return count


def has_recent_native_automod_step_application(
    *,
    user_id: int,
    rule_id: int,
    rule_name: str,
    threshold: int,
    window_minutes: int,
) -> bool:
    bucket = get_native_automod_stats_bucket(user_id)
    prune_native_automod_bucket(bucket)
    cutoff = discord.utils.utcnow() - timedelta(minutes=max(1, window_minutes))
    for record in bucket.get("applied_steps", []):
        if not isinstance(record, dict):
            continue
        dt = iso_to_dt(record.get("timestamp"))
        if not dt or dt < cutoff:
            continue
        record_rule_id = record.get("rule_id")
        record_rule_name = str(record.get("rule_name", ""))
        if str(record_rule_id) != str(rule_id) and record_rule_name != rule_name:
            continue
        if int(record.get("threshold", 0) or 0) != int(threshold):
            continue
        if int(record.get("window_minutes", 0) or 0) != int(window_minutes):
            continue
        return True
    return False


def record_native_automod_step_application(
    *,
    user_id: int,
    rule_id: int,
    rule_name: str,
    step: dict,
) -> None:
    bucket = get_native_automod_stats_bucket(user_id)
    now_value = discord.utils.utcnow()
    prune_native_automod_bucket(bucket, now_value=now_value)
    applied_steps = list(bucket.get("applied_steps", []))
    applied_steps.append({
        "timestamp": now_iso(),
        "rule_id": int(rule_id),
        "rule_name": str(rule_name),
        "threshold": int(step.get("threshold", 1) or 1),
        "window_minutes": int(step.get("window_minutes", 1440) or 1440),
        "punishment_type": str(step.get("punishment_type", "warn") or "warn"),
        "duration_minutes": int(step.get("duration_minutes", 0) or 0),
    })
    bucket["applied_steps"] = applied_steps[-100:]


def get_triggered_native_automod_step(*, user_id: int, rule_id: int, rule_name: str, policy: dict) -> Tuple[Optional[dict], int]:
    if not bool(policy.get("enabled", False)):
        return None, 0

    for step in get_native_automod_policy_steps(policy):
        threshold = int(step.get("threshold", 1) or 1)
        window_minutes = int(step.get("window_minutes", 1440) or 1440)
        hit_count = count_recent_native_automod_hits(
            user_id=user_id,
            rule_id=rule_id,
            rule_name=rule_name,
            window_minutes=window_minutes,
        )
        if hit_count < threshold:
            continue
        if has_recent_native_automod_step_application(
            user_id=user_id,
            rule_id=rule_id,
            rule_name=rule_name,
            threshold=threshold,
            window_minutes=window_minutes,
        ):
            continue
        return step, hit_count
    return None, 0


def build_native_automod_dedupe_key(execution: discord.AutoModAction) -> Tuple[int, int, int, str, str]:
    return (
        int(execution.guild_id or 0),
        int(execution.user_id or 0),
        int(execution.rule_id or 0),
        str(execution.channel_id or 0),
        truncate_text(execution.matched_keyword or execution.matched_content or execution.content or "", 120),
    )


def claim_native_automod_execution(execution: discord.AutoModAction, *, ttl_seconds: int = 15) -> bool:
    now_ts = time.time()
    cache = bot.native_automod_event_cache
    for cache_key, seen_at in list(cache.items()):
        if now_ts - seen_at > ttl_seconds:
            cache.pop(cache_key, None)

    dedupe_key = build_native_automod_dedupe_key(execution)
    previous = cache.get(dedupe_key)
    if previous and now_ts - previous <= ttl_seconds:
        return False

    cache[dedupe_key] = now_ts
    return True


def get_native_automod_action_label(execution: discord.AutoModAction) -> str:
    return execution.action.type.name.replace("_", " ").title()


def native_automod_rule_has_enforcement(rule: Optional[discord.AutoModRule], execution: discord.AutoModAction) -> bool:
    enforcement_types = {
        discord.AutoModRuleActionType.block_message,
        discord.AutoModRuleActionType.timeout,
        discord.AutoModRuleActionType.block_member_interactions,
    }
    if execution.action.type in enforcement_types:
        return True
    if rule is None:
        return False
    return any(getattr(action, "type", None) in enforcement_types for action in getattr(rule, "actions", []))


def is_native_automod_exempt(member: discord.Member, channel_id: Optional[int], settings: dict) -> bool:
    if str(member.id) in bot.data_manager.config.get("immunity_list", []):
        return True

    immunity_users = {int(value) for value in settings.get("immunity_users", []) if isinstance(value, int) or str(value).isdigit()}
    immunity_roles = {int(value) for value in settings.get("immunity_roles", []) if isinstance(value, int) or str(value).isdigit()}
    immunity_channels = {int(value) for value in settings.get("immunity_channels", []) if isinstance(value, int) or str(value).isdigit()}

    if member.id in immunity_users:
        return True
    if channel_id and channel_id in immunity_channels:
        return True
    return any(role.id in immunity_roles for role in member.roles)


async def apply_native_automod_escalation(
    guild: discord.Guild,
    member: discord.Member,
    *,
    rule_id: int,
    rule_name: str,
    content: str,
    matched_keyword: Optional[str],
    warning_count: int,
    policy: dict,
    step: dict,
) -> Tuple[bool, str, Optional[dict]]:
    punishment_type = str(step.get("punishment_type", "warn") or "warn").lower()
    duration_minutes = int(step.get("duration_minutes", 0) or 0)
    threshold = int(step.get("threshold", 1) or 1)
    window_minutes = int(step.get("window_minutes", 1440) or 1440)
    reason_template = str(policy.get("reason_template", "Repeated native AutoMod violations") or "Repeated native AutoMod violations")
    reason = f"{reason_template} [{rule_name}]"
    if punishment_type == "ban":
        action_label = "Banned"
    elif punishment_type == "timeout":
        action_label = "Timed Out"
    elif punishment_type == "kick":
        action_label = "Kicked"
    else:
        action_label = "Warned"
    user_message_text = f"You have been **{action_label}** in **{guild.name}**."
    note = truncate_text(
        "\n".join([
            "Discord AutoMod escalation triggered.",
            f"Rule: {rule_name}",
            f"Hit Count: {warning_count} warning(s) in {format_minutes_interval(window_minutes)}",
            f"Triggered Step: {threshold} warning(s)",
            f"Matched Keyword: {matched_keyword or 'Unknown'}",
            f"Blocked Message: {content or '[Unavailable]'}",
        ]),
        1000,
    )
    timestamp_iso = now_iso()
    case_record = None

    if punishment_type == "timeout" and duration_minutes <= 0:
        duration_minutes = 60
    if punishment_type == "ban":
        duration_minutes = -1

    try:
        if punishment_type == "timeout":
            await member.timeout(get_valid_duration(duration_minutes), reason=f"{reason} (By {bot.user})")
        elif punishment_type == "ban":
            await guild.ban(member, reason=f"{reason} (By {bot.user})", delete_message_days=0)
        elif punishment_type == "kick":
            await guild.kick(member, reason=f"{reason} (By {bot.user})")
    except discord.Forbidden:
        return False, "The bot does not have permission to apply the configured escalation.", None
    except Exception as exc:
        return False, f"Failed to apply escalation: {exc}", None

    record = {
        "reason": reason,
        "moderator": bot.user.id,
        "duration_minutes": duration_minutes if punishment_type != "kick" else 0,
        "timestamp": timestamp_iso,
        "escalated": True,
        "note": note,
        "user_msg": user_message_text,
        "target_name": get_user_display_name(member),
        "type": punishment_type if punishment_type in {"warn", "timeout", "ban", "kick"} else "warn",
        "active": punishment_type in {"ban", "timeout"},
    }
    case_record = await bot.data_manager.add_punishment(str(member.id), record, persist=False)
    bot.data_manager.config.setdefault("stats", {})["total_issued"] = bot.data_manager.config.get("stats", {}).get("total_issued", 0) + 1
    bot.data_manager.mark_config_dirty()
    await bot.data_manager.save_all()

    try:
        dm_embed = make_embed(
            "Moderation Action Issued",
            f"> {user_message_text}",
            kind="danger",
            scope=SCOPE_MODERATION,
            guild=guild,
            thumbnail=guild.icon.url if guild.icon else None,
        )
        dm_embed.add_field(name="Reason", value=format_reason_value(reason, limit=1000), inline=False)
        if punishment_type == "timeout" and duration_minutes > 0:
            dm_embed.add_field(name="Duration", value=format_duration(duration_minutes), inline=True)
            expires = discord.utils.format_dt(discord.utils.utcnow() + get_valid_duration(duration_minutes), "R")
            dm_embed.add_field(name="Expires", value=expires, inline=True)
        elif punishment_type == "ban":
            dm_embed.add_field(name="Duration", value="Ban" if duration_minutes == -1 else format_duration(duration_minutes), inline=True)
            if duration_minutes > 0:
                expires = discord.utils.format_dt(discord.utils.utcnow() + get_valid_duration(duration_minutes), "R")
                dm_embed.add_field(name="Expires", value=expires, inline=True)
        appeal_view = AppealView(guild.id, member.id, bot.user.id, duration_minutes if punishment_type != 'kick' else 0, timestamp_iso, reason)
        await member.send(embed=dm_embed, view=appeal_view)
    except Exception:
        pass

    status = punishment_type.title()
    if punishment_type == "warn":
        status = "Warning"
    elif punishment_type == "timeout":
        status = f"Timeout ({format_duration(duration_minutes)})"
    elif punishment_type == "ban":
        status = "Ban"

    return True, f"Applied {status} automatically at {warning_count} warnings in {format_minutes_interval(window_minutes)}.", case_record


async def run_smart_automod(message: discord.Message) -> bool:
    if not message.guild or isinstance(message.channel, discord.Thread):
        return False
    if not get_feature_flag(bot.data_manager.config, "smart_automod", False):
        return False
    if not isinstance(message.author, discord.Member) or message.author.bot:
        return False

    settings = bot.data_manager.config.get("smart_automod", {})
    exempt_channels = {int(cid) for cid in settings.get("exempt_channels", []) if str(cid).isdigit()}
    exempt_roles = {int(rid) for rid in settings.get("exempt_roles", []) if str(rid).isdigit()}

    if message.channel.id in exempt_channels:
        return False
    if any(role.id in exempt_roles for role in message.author.roles):
        return False
    if is_staff_member(message.author):
        return False

    content = (message.content or "").strip()
    if not content:
        return False

    now = time.time()
    window_seconds = max(5, int(settings.get("duplicate_window_seconds", 20)))
    duplicate_threshold = max(2, int(settings.get("duplicate_threshold", 4)))
    tracker = abuse_system.smart_automod_tracker[message.author.id]
    normalized = re.sub(r"\s+", " ", content.lower())
    tracker.append((now, normalized))
    while tracker and now - tracker[0][0] > window_seconds:
        tracker.popleft()

    duplicate_count = sum(1 for _, entry in tracker if entry == normalized)
    alpha_chars = [char for char in content if char.isalpha()]
    max_caps_ratio = float(settings.get("max_caps_ratio", 0.75))
    caps_min_length = max(5, int(settings.get("caps_min_length", 12)))
    caps_ratio = (
        sum(1 for char in alpha_chars if char.isupper()) / len(alpha_chars)
        if len(alpha_chars) >= caps_min_length
        else 0.0
    )

    blocked_pattern = None
    for pattern in settings.get("blocked_patterns", []):
        try:
            if re.search(pattern, content, re.IGNORECASE):
                blocked_pattern = pattern
                break
        except re.error:
            continue

    trigger_reason = None
    if blocked_pattern:
        trigger_reason = f"Blocked pattern matched: `{blocked_pattern}`"
    elif duplicate_count >= duplicate_threshold:
        trigger_reason = f"Duplicate spam detected ({duplicate_count} matching messages in {window_seconds}s)"
    elif caps_ratio >= max_caps_ratio:
        trigger_reason = f"Excessive caps ratio detected ({round(caps_ratio * 100)}%)"

    if not trigger_reason:
        return False

    try:
        await message.delete()
    except Exception:
        pass

    notice = None
    try:
        notice = await message.channel.send(
            f"{message.author.mention} your message was removed by smart automod.",
            delete_after=10,
        )
    except Exception:
        notice = None

    embed = make_action_log_embed(
        "Smart AutoMod Triggered",
        "A message was removed by the bot's smart filter layer.",
        guild=message.guild,
        kind="warning",
        scope=SCOPE_MODERATION,
        actor=format_user_ref(message.author),
        target=f"{message.channel.mention} (`{message.channel.id}`)",
        reason=trigger_reason,
        duration="Message Removed",
        expires="N/A",
        message=content,
        notes=[
            f"Duplicate Hits: {duplicate_count}",
            f"Caps Ratio: {round(caps_ratio * 100)}%",
            f"Blocked Pattern: {blocked_pattern or 'None'}",
        ],
        thumbnail=message.author.display_avatar.url,
    )
    await send_automod_log(message.guild, embed)
    return True


def ensure_native_rule_override_policy(settings: dict, rule: discord.AutoModRule) -> Tuple[str, dict]:
    override_key, current_policy, _ = get_native_rule_override(settings, rule)
    policy = {
        "enabled": bool(current_policy.get("enabled", False)),
        "reason_template": str(current_policy.get("reason_template", DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"]["reason_template"]) or DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"]["reason_template"])[:200],
        "steps": get_native_automod_policy_steps(current_policy),
    }
    settings.setdefault("rule_overrides", {})[override_key] = policy
    return override_key, policy


class AutoModPolicyReasonModal(discord.ui.Modal, title="Edit AutoMod Reason Template"):
    reason_template = discord.ui.TextInput(
        label="Reason Template",
        style=discord.TextStyle.paragraph,
        max_length=200,
        placeholder="Repeated native AutoMod violations",
    )

    def __init__(self, *, rule: Optional[discord.AutoModRule] = None, rules: Optional[List[discord.AutoModRule]] = None):
        super().__init__()
        self.rule = rule
        self.rules = rules or []
        settings = get_native_automod_settings(bot.data_manager.config)
        if rule is None:
            policy = build_default_native_automod_policy()
        else:
            _, policy, _ = get_native_rule_override(settings, rule)
        self.reason_template.default = str(policy.get("reason_template", DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"]["reason_template"]))

    async def on_submit(self, interaction: discord.Interaction):
        settings = get_native_automod_settings(bot.data_manager.config)
        if self.rule is None:
            await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())
            return
        _, policy = ensure_native_rule_override_policy(settings, self.rule)
        policy["reason_template"] = self.reason_template.value.strip()[:200] or DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"]["reason_template"]
        store_native_automod_settings(settings)
        await bot.data_manager.save_config()

        view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules)
        await interaction.response.send_message(embed=view.build_embed(interaction.guild), view=view, ephemeral=True)


class AutoModStepValuesModal(discord.ui.Modal, title="Edit AutoMod Step"):
    punishment_type = discord.ui.TextInput(
        label="Action",
        placeholder="warn, timeout, kick, or ban",
        max_length=10,
    )
    warning_count = discord.ui.TextInput(
        label="Warnings",
        placeholder="3",
        max_length=4,
    )
    warning_window = discord.ui.TextInput(
        label="Window",
        placeholder="6h, 2d, or 1w",
        max_length=12,
    )
    timeout_length = discord.ui.TextInput(
        label="Timeout Length",
        placeholder="1h or 12h",
        required=False,
        max_length=12,
    )

    def __init__(self, *, parent_view):
        super().__init__()
        self.parent_view = parent_view
        current_step = parent_view.get_current_step()
        self.punishment_type.default = str(current_step.get("punishment_type", "warn")).lower()
        self.warning_count.default = str(current_step.get("threshold", 1))
        self.warning_window.default = format_compact_minutes_input(int(current_step.get("window_minutes", 1440) or 1440))
        if str(current_step.get("punishment_type", "warn")).lower() == "timeout":
            self.timeout_length.default = format_compact_minutes_input(int(current_step.get("duration_minutes", 60) or 60))
        else:
            self.timeout_length.default = ""

    async def on_submit(self, interaction: discord.Interaction):
        policy = self.parent_view.get_current_policy()
        steps = self.parent_view.get_current_steps()
        if not steps:
            overview = AutoModPolicyEditorView(rule=self.parent_view.rule, rules=self.parent_view.rules)
            await interaction.response.send_message(embed=overview.build_embed(interaction.guild), view=overview, ephemeral=True)
            return

        current_step = dict(steps[self.parent_view.step_index])

        try:
            punishment_type = parse_automod_punishment_input(self.punishment_type.value, field_name="Action")
            current_step["punishment_type"] = punishment_type
            current_step["threshold"] = parse_positive_integer_input(self.warning_count.value, field_name="Warning count")
            current_step["window_minutes"] = parse_minutes_input(self.warning_window.value, field_name="Warning window", maximum=43200)
            if punishment_type == "timeout":
                timeout_raw = self.timeout_length.value.strip() or format_compact_minutes_input(int(current_step.get("duration_minutes", 60) or 60))
                current_step["duration_minutes"] = parse_minutes_input(timeout_raw, field_name="Timeout length", maximum=40320)
            elif punishment_type == "ban":
                current_step["duration_minutes"] = -1
            else:
                current_step["duration_minutes"] = 0
        except ValueError as exc:
            await respond_with_error(interaction, str(exc), scope=SCOPE_MODERATION)
            return

        steps[self.parent_view.step_index] = current_step
        policy["steps"] = steps
        await self.parent_view.persist_policy(policy)

        view = AutoModPolicyEditorView(rule=self.parent_view.rule, rules=self.parent_view.rules, step_index=self.parent_view.step_index)
        if getattr(interaction, "message", None) is not None:
            await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)
            return
        await interaction.response.send_message(embed=view.build_embed(interaction.guild), view=view, ephemeral=True)


class AutoModStepSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = []
        for index, step in enumerate(self.parent_view.get_current_steps()):
            options.append(
                discord.SelectOption(
                    label=f"Step {index + 1}",
                    value=str(index),
                    description=truncate_text(format_native_automod_step_summary(step), 100),
                    default=index == getattr(self.parent_view, "step_index", 0),
                )
            )
        super().__init__(placeholder="Choose which step to edit...", min_values=1, max_values=1, options=options[:25], row=0)

    async def callback(self, interaction: discord.Interaction):
        step_index = int(self.values[0])
        view = AutoModPolicyEditorView(rule=self.parent_view.rule, rules=self.parent_view.rules, step_index=step_index)
        await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)


class AutoModStepPunishmentTypeSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        current_type = self.parent_view.get_current_step().get("punishment_type", "warn")
        options = [
            discord.SelectOption(label=label, value=value, default=value == current_type)
            for value, label in AUTOMOD_PUNISHMENT_OPTIONS
        ]
        super().__init__(placeholder="Choose the punishment for this step...", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.set_step_punishment_type(interaction, self.values[0])


class AutoModStepThresholdSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        current = int(self.parent_view.get_current_step().get("threshold", 3) or 3)
        super().__init__(
            placeholder="Trigger this step after this many warnings...",
            min_values=1,
            max_values=1,
            options=build_numeric_select_options(current, AUTOMOD_THRESHOLD_PRESETS, lambda value: f"{value} hit{'s' if value != 1 else ''}"),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.set_step_value(interaction, "threshold", int(self.values[0]))


class AutoModStepWindowSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        current = int(self.parent_view.get_current_step().get("window_minutes", 1440) or 1440)
        super().__init__(
            placeholder="Only count warnings inside this time window...",
            min_values=1,
            max_values=1,
            options=build_numeric_select_options(current, AUTOMOD_WINDOW_PRESETS, format_minutes_interval),
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.set_step_value(interaction, "window_minutes", int(self.values[0]))


class AutoModStepTimeoutDurationSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        current_step = self.parent_view.get_current_step()
        current = int(current_step.get("duration_minutes", 60) or 60)
        super().__init__(
            placeholder="Timeout length when action is timeout...",
            min_values=1,
            max_values=1,
            options=build_numeric_select_options(current, AUTOMOD_TIMEOUT_PRESETS, format_minutes_interval),
            row=3,
        )
        self.disabled = str(current_step.get("punishment_type", "warn")).lower() != "timeout"

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.set_step_value(interaction, "duration_minutes", int(self.values[0]))


class AutoModRuleSelect(discord.ui.Select):
    def __init__(self, parent_view, rules: List[discord.AutoModRule]):
        self.parent_view = parent_view
        self.rules = rules[:25]
        options = []
        settings = get_native_automod_settings(bot.data_manager.config)
        for rule in self.rules:
            _, policy, using_override = get_native_rule_override(settings, rule)
            steps = get_native_automod_policy_steps(policy)
            summary_label = f"{len(steps)} step{'s' if len(steps) != 1 else ''}" if steps else "No steps"
            options.append(
                discord.SelectOption(
                    label=truncate_text(rule.name, 100),
                    value=str(rule.id),
                    description=truncate_text(
                        f"{'On' if policy.get('enabled') and steps else 'Off'} • {summary_label}",
                        100,
                    ),
                )
            )
        super().__init__(placeholder="Choose a native AutoMod rule...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        selected = next((rule for rule in self.rules if str(rule.id) == self.values[0]), None)
        if selected is None:
            await respond_with_error(interaction, "That AutoMod rule could not be found anymore.", scope=SCOPE_MODERATION)
            return
        view = AutoModPolicyEditorView(rule=selected, rules=self.parent_view.rules)
        await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)


class AutoModBridgeSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.sync_buttons()

    def sync_buttons(self):
        settings = get_native_automod_settings(bot.data_manager.config)
        self.toggle_bridge.label = f"Bot Response: {'On' if settings.get('enabled', True) else 'Off'}"
        self.toggle_bridge.style = discord.ButtonStyle.success if settings.get("enabled", True) else discord.ButtonStyle.secondary
        self.toggle_dm.label = f"User DMs: {'On' if settings.get('warning_dm_enabled', True) else 'Off'}"
        self.toggle_dm.style = discord.ButtonStyle.success if settings.get("warning_dm_enabled", True) else discord.ButtonStyle.secondary
        self.toggle_report.label = f"Report Button: {'On' if settings.get('report_button_enabled', True) else 'Off'}"
        self.toggle_report.style = discord.ButtonStyle.success if settings.get("report_button_enabled", True) else discord.ButtonStyle.secondary

    async def _save_and_refresh(self, interaction: discord.Interaction, settings: dict):
        store_native_automod_settings(settings)
        await bot.data_manager.save_config()
        self.sync_buttons()
        await interaction.response.edit_message(embed=build_automod_bridge_embed(interaction.guild), view=self)

    @discord.ui.button(label="Bot Response", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_bridge(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = get_native_automod_settings(bot.data_manager.config)
        settings["enabled"] = not settings.get("enabled", True)
        await self._save_and_refresh(interaction, settings)

    @discord.ui.button(label="User DMs", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = get_native_automod_settings(bot.data_manager.config)
        settings["warning_dm_enabled"] = not settings.get("warning_dm_enabled", True)
        await self._save_and_refresh(interaction, settings)

    @discord.ui.button(label="Report Button", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = get_native_automod_settings(bot.data_manager.config)
        settings["report_button_enabled"] = not settings.get("report_button_enabled", True)
        await self._save_and_refresh(interaction, settings)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())


class AutoModRuleBrowserView(discord.ui.View):
    def __init__(self, rules: List[discord.AutoModRule]):
        super().__init__(timeout=180)
        self.rules = rules[:25]
        if self.rules:
            self.add_item(AutoModRuleSelect(self, self.rules))

    @discord.ui.button(label="Refresh Rules", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        rules = await fetch_native_automod_rules(interaction.guild)
        view = AutoModRuleBrowserView(rules)
        await interaction.response.edit_message(embed=build_automod_rule_browser_embed(interaction.guild, rules), view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())


class AutoModPolicyEditorView(discord.ui.View):
    def __init__(self, *, rule: Optional[discord.AutoModRule] = None, rules: Optional[List[discord.AutoModRule]] = None, step_index: int = 0):
        super().__init__(timeout=180)
        self.rule = rule
        self.rules = rules or []
        self.step_index = step_index
        steps = self.get_current_steps() if self.rule is not None else []
        if steps:
            self.step_index = max(0, min(step_index, len(steps) - 1))
            self.add_item(AutoModStepSelect(self))
        self.sync_buttons()

    def get_current_policy(self) -> dict:
        settings = get_native_automod_settings(bot.data_manager.config)
        if self.rule is None:
            return build_default_native_automod_policy()
        _, policy, _ = get_native_rule_override(settings, self.rule)
        return {
            "enabled": bool(policy.get("enabled", False)),
            "reason_template": str(policy.get("reason_template", DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"]["reason_template"]) or DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"]["reason_template"])[:200],
            "steps": get_native_automod_policy_steps(policy),
        }

    def get_current_steps(self) -> List[dict]:
        return get_native_automod_policy_steps(self.get_current_policy())

    def get_current_step(self) -> dict:
        steps = self.get_current_steps()
        if not steps:
            self.step_index = 0
            return build_default_native_automod_step()
        self.step_index = max(0, min(self.step_index, len(steps) - 1))
        return dict(steps[self.step_index])

    def build_embed(self, guild: discord.Guild) -> discord.Embed:
        if self.rule is None:
            return build_automod_policy_embed(
                guild,
                build_default_native_automod_policy(),
                title="AutoMod Rule Punishment",
                description="> Pick a Discord AutoMod rule first, then edit that rule's punishment settings.",
            )
        settings = get_native_automod_settings(bot.data_manager.config)
        _, policy, using_override = get_native_rule_override(settings, self.rule)
        return build_automod_policy_embed(
            guild,
            policy,
            title=f"Rule Punishment: {self.rule.name}",
            description="> Pick a step from the dropdown, then use the buttons below to edit that step or the rule.",
            rule=self.rule,
            using_override=using_override,
            selected_step_index=self.step_index if self.get_current_steps() else None,
        )

    def sync_buttons(self):
        settings = get_native_automod_settings(bot.data_manager.config)
        enabled = False
        using_override = False
        steps = self.get_current_steps() if self.rule is not None else []
        if self.rule is not None:
            _, policy, using_override = get_native_rule_override(settings, self.rule)
            enabled = bool(policy.get("enabled", False) and steps)
        self.toggle_enabled.label = f"Auto Punish: {'On' if enabled else 'Off'}"
        self.toggle_enabled.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        self.toggle_enabled.disabled = not bool(steps)
        self.add_step.disabled = self.rule is None or len(steps) >= 5
        self.custom_amounts.disabled = not bool(steps)
        self.remove_step.disabled = not bool(steps)
        self.remove_step.style = discord.ButtonStyle.secondary if self.remove_step.disabled else discord.ButtonStyle.danger
        self.clear_override.disabled = self.rule is None or not using_override
        self.clear_override.style = discord.ButtonStyle.secondary if self.clear_override.disabled else discord.ButtonStyle.danger

    async def persist_policy(self, policy: dict):
        settings = get_native_automod_settings(bot.data_manager.config)
        if self.rule is None:
            return
        override_key, _ = ensure_native_rule_override_policy(settings, self.rule)
        policy["steps"] = get_native_automod_policy_steps(policy)
        if not policy["steps"]:
            policy["enabled"] = False
            self.step_index = 0
        else:
            self.step_index = max(0, min(self.step_index, len(policy["steps"]) - 1))
        settings.setdefault("rule_overrides", {})[override_key] = policy
        store_native_automod_settings(settings)
        await bot.data_manager.save_config()

    async def save_policy(self, interaction: discord.Interaction, policy: dict):
        if self.rule is None:
            await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())
            return
        await self.persist_policy(policy)

    async def set_step_value(self, interaction: discord.Interaction, key: str, value: int):
        policy = self.get_current_policy()
        steps = self.get_current_steps()
        if not steps:
            view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=self.step_index)
            await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)
            return
        steps[self.step_index][key] = value
        policy["steps"] = steps
        await self.save_policy(interaction, policy)
        view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=self.step_index)
        await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)

    async def set_step_punishment_type(self, interaction: discord.Interaction, punishment_type: str):
        policy = self.get_current_policy()
        steps = self.get_current_steps()
        if not steps:
            view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=self.step_index)
            await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)
            return
        steps[self.step_index]["punishment_type"] = punishment_type
        if punishment_type == "timeout" and int(steps[self.step_index].get("duration_minutes", 0) or 0) <= 0:
            steps[self.step_index]["duration_minutes"] = 60
        elif punishment_type == "ban":
            steps[self.step_index]["duration_minutes"] = -1
        else:
            steps[self.step_index]["duration_minutes"] = 0
        policy["steps"] = steps
        await self.save_policy(interaction, policy)
        view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=self.step_index)
        await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)

    @discord.ui.button(label="Auto Punish", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_enabled(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = get_native_automod_settings(bot.data_manager.config)
        if self.rule is None:
            await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())
            return
        _, policy = ensure_native_rule_override_policy(settings, self.rule)
        if not policy.get("steps"):
            view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=self.step_index)
            await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)
            return
        policy["enabled"] = not bool(policy.get("enabled", False))
        await self.save_policy(interaction, policy)
        view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=self.step_index)
        await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)

    @discord.ui.button(label="Add Step", style=discord.ButtonStyle.primary, row=1)
    async def add_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = get_native_automod_settings(bot.data_manager.config)
        if self.rule is None:
            await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())
            return
        _, policy = ensure_native_rule_override_policy(settings, self.rule)
        steps = get_native_automod_policy_steps(policy)
        if len(steps) >= 5:
            await interaction.response.edit_message(embed=self.build_embed(interaction.guild), view=self)
            return
        steps.append(build_default_native_automod_step(steps))
        policy["steps"] = steps
        policy["enabled"] = True
        await self.save_policy(interaction, policy)
        view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=len(steps) - 1)
        await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)

    @discord.ui.button(label="Edit Selected Step", style=discord.ButtonStyle.primary, row=1)
    async def custom_amounts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoModStepValuesModal(parent_view=self))

    @discord.ui.button(label="Edit Reason", style=discord.ButtonStyle.secondary, row=2)
    async def edit_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoModPolicyReasonModal(rule=self.rule, rules=self.rules))

    @discord.ui.button(label="Remove Selected", style=discord.ButtonStyle.danger, row=2)
    async def remove_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        policy = self.get_current_policy()
        steps = self.get_current_steps()
        if not steps:
            view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=self.step_index)
            await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)
            return
        steps.pop(self.step_index)
        policy["steps"] = steps
        if not steps:
            policy["enabled"] = False
        await self.save_policy(interaction, policy)
        next_index = min(self.step_index, max(0, len(steps) - 1))
        view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=next_index)
        await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)

    @discord.ui.button(label="Reset Rule", style=discord.ButtonStyle.danger, row=2)
    async def clear_override(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.rule is None:
            await interaction.response.defer()
            return
        settings = get_native_automod_settings(bot.data_manager.config)
        override_key, _, using_override = get_native_rule_override(settings, self.rule)
        if using_override:
            settings.setdefault("rule_overrides", {}).pop(override_key, None)
            settings.setdefault("rule_overrides", {}).pop(self.rule.name, None)
            settings.setdefault("rule_overrides", {}).pop(str(self.rule.id), None)
            store_native_automod_settings(settings)
            await bot.data_manager.save_config()
        view = AutoModPolicyEditorView(rule=self.rule, rules=self.rules, step_index=0)
        await interaction.response.edit_message(embed=view.build_embed(interaction.guild), view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.rule is None:
            await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())
            return
        rules = self.rules or await fetch_native_automod_rules(interaction.guild)
        await interaction.response.edit_message(embed=build_automod_rule_browser_embed(interaction.guild, rules), view=AutoModRuleBrowserView(rules))


class AutoModChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, config_key: str, label: str):
        super().__init__(
            placeholder=f"Select {label}...",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.config_key = config_key
        self.label = label

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        channel = interaction.guild.get_channel(selected.id) or await interaction.guild.fetch_channel(selected.id)
        bot.data_manager.config[self.config_key] = channel.id
        await bot.data_manager.save_config()
        view = AutoModChannelSettingsView()
        await interaction.response.edit_message(embed=build_automod_routing_embed(interaction.guild), view=view)


class AutoModChannelSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(AutoModChannelSelect("automod_log_channel_id", "AutoMod Log Channel"))
        self.add_item(AutoModChannelSelect("automod_report_channel_id", "AutoMod Report Channel"))
        self.add_item(AutoModChannelActionSelect())


class AutoModChannelActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Back to Dashboard", value="back", description="Return to the main AutoMod control panel."),
            discord.SelectOption(label="Clear Log Channel", value="clear_log", description="Clear the dedicated AutoMod log channel."),
            discord.SelectOption(label="Clear Report Channel", value="clear_report", description="Clear the dedicated AutoMod report channel."),
        ]
        super().__init__(
            placeholder="More log channel actions...",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "back":
            await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())
            return
        if action == "clear_log":
            bot.data_manager.config["automod_log_channel_id"] = 0
            await bot.data_manager.save_config()
            await interaction.response.edit_message(embed=build_automod_routing_embed(interaction.guild), view=AutoModChannelSettingsView())
            return
        if action == "clear_report":
            bot.data_manager.config["automod_report_channel_id"] = 0
            await bot.data_manager.save_config()
            await interaction.response.edit_message(embed=build_automod_routing_embed(interaction.guild), view=AutoModChannelSettingsView())


class AutoModStoredValueRemoveSelect(discord.ui.Select):
    def __init__(self, *, label: str, config_scope: str, config_key: str, options: List[discord.SelectOption]):
        self.config_scope = config_scope
        self.config_key = config_key
        super().__init__(
            placeholder=f"Remove {label}...",
            min_values=1,
            max_values=min(len(options), 10),
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        selected_ids = {int(value) for value in self.values}
        if self.config_scope == "native":
            settings = get_native_automod_settings(bot.data_manager.config)
            settings[self.config_key] = [value for value in settings.get(self.config_key, []) if int(value) not in selected_ids]
            store_native_automod_settings(settings)
        else:
            settings = get_smart_automod_settings()
            settings[self.config_key] = [value for value in settings.get(self.config_key, []) if int(value) not in selected_ids]
            store_smart_automod_settings(settings)
        await bot.data_manager.save_config()
        await interaction.response.edit_message(content="Removed the selected entries.", view=None)


class AutoModStoredValueRemoveView(discord.ui.View):
    def __init__(self, *, label: str, config_scope: str, config_key: str, options: List[discord.SelectOption]):
        super().__init__(timeout=180)
        self.add_item(AutoModStoredValueRemoveSelect(label=label, config_scope=config_scope, config_key=config_key, options=options))


class AutoModImmunityUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Add immune users...", min_values=1, max_values=10, row=0)

    async def callback(self, interaction: discord.Interaction):
        settings = get_native_automod_settings(bot.data_manager.config)
        current = {int(value) for value in settings.get("immunity_users", [])}
        current.update(int(user.id) for user in self.values)
        settings["immunity_users"] = sorted(current)
        store_native_automod_settings(settings)
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_automod_immunity_embed(interaction.guild), view=AutoModImmunityView())


class AutoModImmunityRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Add immune roles...", min_values=1, max_values=10, row=1)

    async def callback(self, interaction: discord.Interaction):
        settings = get_native_automod_settings(bot.data_manager.config)
        current = {int(value) for value in settings.get("immunity_roles", [])}
        current.update(int(role.id) for role in self.values)
        settings["immunity_roles"] = sorted(current)
        store_native_automod_settings(settings)
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_automod_immunity_embed(interaction.guild), view=AutoModImmunityView())


class AutoModImmunityChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Add immune channels...", min_values=1, max_values=10, channel_types=[discord.ChannelType.text], row=2)

    async def callback(self, interaction: discord.Interaction):
        settings = get_native_automod_settings(bot.data_manager.config)
        current = {int(value) for value in settings.get("immunity_channels", [])}
        current.update(int(channel.id) for channel in self.values)
        settings["immunity_channels"] = sorted(current)
        store_native_automod_settings(settings)
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_automod_immunity_embed(interaction.guild), view=AutoModImmunityView())


class AutoModImmunityView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(AutoModImmunityUserSelect())
        self.add_item(AutoModImmunityRoleSelect())
        self.add_item(AutoModImmunityChannelSelect())

    async def _send_remove_picker(self, interaction: discord.Interaction, *, label: str, config_key: str):
        settings = get_native_automod_settings(bot.data_manager.config)
        values = settings.get(config_key, [])
        if not values:
            await interaction.response.send_message(f"No {label.lower()} are configured.", ephemeral=True)
            return
        options = []
        for value in values[:25]:
            if config_key == "immunity_users":
                member = interaction.guild.get_member(int(value))
                option_label = member.display_name if member else f"User {value}"
            elif config_key == "immunity_roles":
                role = interaction.guild.get_role(int(value))
                option_label = role.name if role else f"Role {value}"
            else:
                channel = interaction.guild.get_channel(int(value)) or interaction.guild.get_channel_or_thread(int(value))
                option_label = f"#{channel.name}" if channel else f"Channel {value}"
            options.append(discord.SelectOption(label=truncate_text(option_label, 100), value=str(value)))
        await interaction.response.send_message(
            f"Choose which {label.lower()} to remove:",
            view=AutoModStoredValueRemoveView(label=label, config_scope="native", config_key=config_key, options=options),
            ephemeral=True,
        )

    @discord.ui.button(label="Remove Users", style=discord.ButtonStyle.secondary, row=3)
    async def remove_users(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_remove_picker(interaction, label="Users", config_key="immunity_users")

    @discord.ui.button(label="Remove Roles", style=discord.ButtonStyle.secondary, row=3)
    async def remove_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_remove_picker(interaction, label="Roles", config_key="immunity_roles")

    @discord.ui.button(label="Remove Channels", style=discord.ButtonStyle.secondary, row=3)
    async def remove_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_remove_picker(interaction, label="Channels", config_key="immunity_channels")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())


class SmartAutoModThresholdModal(discord.ui.Modal, title="Edit Smart Filter Thresholds"):
    duplicate_window_seconds = discord.ui.TextInput(label="Duplicate window seconds", placeholder="20", max_length=4)
    duplicate_threshold = discord.ui.TextInput(label="Duplicate message count", placeholder="4", max_length=4)
    caps_min_length = discord.ui.TextInput(label="Minimum length before caps check", placeholder="12", max_length=4)
    max_caps_ratio = discord.ui.TextInput(label="Caps percent before block", placeholder="75", max_length=5)

    def __init__(self):
        super().__init__()
        settings = get_smart_automod_settings()
        self.duplicate_window_seconds.default = str(settings.get("duplicate_window_seconds", 20))
        self.duplicate_threshold.default = str(settings.get("duplicate_threshold", 4))
        self.caps_min_length.default = str(settings.get("caps_min_length", 12))
        self.max_caps_ratio.default = str(int(round(float(settings.get("max_caps_ratio", 0.75)) * 100)))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            ratio_value = float(self.max_caps_ratio.value)
            if ratio_value > 1:
                ratio_value = ratio_value / 100
            settings = get_smart_automod_settings()
            settings["duplicate_window_seconds"] = max(5, int(self.duplicate_window_seconds.value))
            settings["duplicate_threshold"] = max(2, int(self.duplicate_threshold.value))
            settings["caps_min_length"] = max(3, int(self.caps_min_length.value))
            settings["max_caps_ratio"] = max(0.1, min(1.0, ratio_value))
        except ValueError:
            await respond_with_error(interaction, "Smart AutoMod thresholds must be valid numbers.", scope=SCOPE_MODERATION)
            return

        store_smart_automod_settings(settings)
        await bot.data_manager.save_config()
        view = SmartAutoModSettingsView()
        await interaction.response.send_message(embed=build_smart_automod_embed(interaction.guild), view=view, ephemeral=True)


class SmartAutoModPatternModal(discord.ui.Modal, title="Edit Blocked Patterns"):
    blocked_patterns = discord.ui.TextInput(
        label="One pattern per line",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000,
        placeholder="slur here\nanother blocked phrase",
    )

    def __init__(self):
        super().__init__()
        self.blocked_patterns.default = "\n".join(get_smart_automod_settings().get("blocked_patterns", []))

    async def on_submit(self, interaction: discord.Interaction):
        lines = [line.strip() for line in self.blocked_patterns.value.splitlines() if line.strip()]
        settings = get_smart_automod_settings()
        settings["blocked_patterns"] = lines[:50]
        store_smart_automod_settings(settings)
        await bot.data_manager.save_config()
        view = SmartAutoModSettingsView()
        await interaction.response.send_message(embed=build_smart_automod_embed(interaction.guild), view=view, ephemeral=True)


class SmartAutoModExemptRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Add smart-filter exempt roles...", min_values=1, max_values=10, row=0)

    async def callback(self, interaction: discord.Interaction):
        settings = get_smart_automod_settings()
        current = {int(value) for value in settings.get("exempt_roles", [])}
        current.update(int(role.id) for role in self.values)
        settings["exempt_roles"] = sorted(current)
        store_smart_automod_settings(settings)
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_smart_automod_embed(interaction.guild), view=SmartAutoModSettingsView())


class SmartAutoModExemptChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Add smart-filter exempt channels...", min_values=1, max_values=10, channel_types=[discord.ChannelType.text], row=1)

    async def callback(self, interaction: discord.Interaction):
        settings = get_smart_automod_settings()
        current = {int(value) for value in settings.get("exempt_channels", [])}
        current.update(int(channel.id) for channel in self.values)
        settings["exempt_channels"] = sorted(current)
        store_smart_automod_settings(settings)
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_smart_automod_embed(interaction.guild), view=SmartAutoModSettingsView())


class SmartAutoModSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SmartAutoModExemptRoleSelect())
        self.add_item(SmartAutoModExemptChannelSelect())
        enabled = get_feature_flag(bot.data_manager.config, "smart_automod", False)
        self.toggle_feature.label = f"Smart Filters: {'On' if enabled else 'Off'}"
        self.toggle_feature.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary

    async def _send_remove_picker(self, interaction: discord.Interaction, *, label: str, config_key: str):
        settings = get_smart_automod_settings()
        values = settings.get(config_key, [])
        if not values:
            await interaction.response.send_message(f"No {label.lower()} are configured.", ephemeral=True)
            return
        options = []
        for value in values[:25]:
            if config_key == "exempt_roles":
                role = interaction.guild.get_role(int(value))
                option_label = role.name if role else f"Role {value}"
            else:
                channel = interaction.guild.get_channel(int(value)) or interaction.guild.get_channel_or_thread(int(value))
                option_label = f"#{channel.name}" if channel else f"Channel {value}"
            options.append(discord.SelectOption(label=truncate_text(option_label, 100), value=str(value)))
        await interaction.response.send_message(
            f"Choose which {label.lower()} to remove:",
            view=AutoModStoredValueRemoveView(label=label, config_scope="smart", config_key=config_key, options=options),
            ephemeral=True,
        )

    @discord.ui.button(label="Smart Filters", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_feature(self, interaction: discord.Interaction, button: discord.ui.Button):
        flags = bot.data_manager.config.setdefault("feature_flags", {})
        flags["smart_automod"] = not bool(flags.get("smart_automod", False))
        await bot.data_manager.save_config()
        view = SmartAutoModSettingsView()
        await interaction.response.edit_message(embed=build_smart_automod_embed(interaction.guild), view=view)

    @discord.ui.button(label="Edit Thresholds", style=discord.ButtonStyle.primary, row=2)
    async def edit_thresholds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SmartAutoModThresholdModal())

    @discord.ui.button(label="Edit Pattern List", style=discord.ButtonStyle.primary, row=2)
    async def edit_patterns(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SmartAutoModPatternModal())

    @discord.ui.button(label="Remove Exempt Roles", style=discord.ButtonStyle.secondary, row=3)
    async def remove_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_remove_picker(interaction, label="Roles", config_key="exempt_roles")

    @discord.ui.button(label="Remove Exempt Channels", style=discord.ButtonStyle.secondary, row=3)
    async def remove_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_remove_picker(interaction, label="Channels", config_key="exempt_channels")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())


class AutoModDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView())

    @discord.ui.button(label="Bot Response", style=discord.ButtonStyle.primary, row=0)
    async def bridge(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_automod_bridge_embed(interaction.guild), view=AutoModBridgeSettingsView())

    @discord.ui.button(label="Rule Punishments", style=discord.ButtonStyle.primary, row=0)
    async def native_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        rules = await fetch_native_automod_rules(interaction.guild)
        view = AutoModRuleBrowserView(rules)
        await interaction.edit_original_response(embed=build_automod_rule_browser_embed(interaction.guild, rules), view=view)

    @discord.ui.button(label="Log Channels", style=discord.ButtonStyle.success, row=1)
    async def routing(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_automod_routing_embed(interaction.guild), view=AutoModChannelSettingsView())

    @discord.ui.button(label="Immunity", style=discord.ButtonStyle.success, row=1)
    async def immunity(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_automod_immunity_embed(interaction.guild), view=AutoModImmunityView())


async def resolve_user_for_automod_report(guild: Optional[discord.Guild], user_id: int) -> Optional[Union[discord.Member, discord.User]]:
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
    cached = bot.get_user(user_id)
    if cached is not None:
        return cached
    try:
        return await bot.fetch_user(user_id)
    except Exception:
        return None


async def apply_automod_report_response(
    interaction: discord.Interaction,
    *,
    guild_id: int,
    reporter_id: int,
    warning_id: str,
    rule_name: str,
    response_key: str,
    response_text: str,
    source_message: Optional[discord.Message],
) -> bool:
    if not is_staff(interaction):
        await respond_with_error(interaction, "Access denied.", scope=SCOPE_MODERATION)
        return False

    guild = bot.get_guild(guild_id) or interaction.guild or get_primary_guild()
    if guild is None:
        await respond_with_error(interaction, "The server for this AutoMod report could not be resolved.", scope=SCOPE_MODERATION)
        return False

    if source_message is not None and source_message.embeds:
        for field in source_message.embeds[0].fields:
            if str(field.name).strip().lower() == "report status":
                await respond_with_error(interaction, "This AutoMod report already has a staff response.", scope=SCOPE_MODERATION)
                return False

    target_user = await resolve_user_for_automod_report(guild, reporter_id)
    if target_user is None:
        await respond_with_error(interaction, "The user for this AutoMod report could not be found.", scope=SCOPE_MODERATION)
        return False

    preset = get_automod_report_preset(response_key)
    dm_embed = make_embed(
        "AutoMod Report Update",
        f"> {response_text}",
        kind=preset.get("kind", "info"),
        scope=SCOPE_MODERATION,
        guild=guild,
        thumbnail=guild.icon.url if guild and guild.icon else None,
    )
    dm_embed.add_field(name="Reason", value=format_reason_value(rule_name, limit=300), inline=False)
    dm_embed.add_field(name="Responder", value=format_user_ref(interaction.user), inline=False)

    try:
        await target_user.send(embed=dm_embed)
    except discord.Forbidden:
        await respond_with_error(interaction, "The user has DMs closed, so the response could not be delivered.", scope=SCOPE_MODERATION)
        return False
    except Exception as exc:
        await respond_with_error(interaction, f"Failed to send the AutoMod report response: {exc}", scope=SCOPE_MODERATION)
        return False

    report_message = source_message
    if report_message is None:
        report_channel_id = (
            bot.data_manager.config.get("automod_report_channel_id")
            or bot.data_manager.config.get("appeal_channel_id")
            or get_punishment_log_channel_id()
        )
        report_channel = guild.get_channel_or_thread(int(report_channel_id)) if report_channel_id else None
        if report_channel is not None and interaction.message is not None:
            report_message = interaction.message

    if report_message is not None and report_message.embeds:
        updated_embed = discord.Embed.from_dict(report_message.embeds[0].to_dict())
        updated_embed.color = EMBED_PALETTE.get(preset.get("kind", "info"), EMBED_PALETTE["info"])
        upsert_embed_field(updated_embed, "Report Status", preset.get("status", "Staff Replied"), inline=True)
        upsert_embed_field(updated_embed, "Responder", format_user_ref(interaction.user), inline=True)
        upsert_embed_field(updated_embed, "Responded", discord.utils.format_dt(discord.utils.utcnow(), "F"), inline=True)
        upsert_embed_field(updated_embed, "Staff Response", format_log_quote(response_text, limit=800), inline=False)
        brand_embed(updated_embed, guild=guild, scope=SCOPE_MODERATION)
        try:
            await report_message.edit(embed=updated_embed, view=None)
        except Exception:
            pass
    return True


class AutoModCustomReportResponseModal(discord.ui.Modal, title="Custom AutoMod Report Response"):
    response_text = discord.ui.TextInput(
        label="Response",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="Write the response that should be sent to the user.",
    )

    def __init__(self, *, guild_id: int, reporter_id: int, warning_id: str, rule_name: str, source_message: Optional[discord.Message]):
        super().__init__()
        self.guild_id = guild_id
        self.reporter_id = reporter_id
        self.warning_id = warning_id
        self.rule_name = rule_name
        self.source_message = source_message

    async def on_submit(self, interaction: discord.Interaction):
        success = await apply_automod_report_response(
            interaction,
            guild_id=self.guild_id,
            reporter_id=self.reporter_id,
            warning_id=self.warning_id,
            rule_name=self.rule_name,
            response_key="custom",
            response_text=self.response_text.value.strip()[:1000],
            source_message=self.source_message,
        )
        if success and not interaction.response.is_done():
            await interaction.response.send_message("Response sent.", ephemeral=True)
        elif success:
            await interaction.followup.send("Response sent.", ephemeral=True)


class AutoModReportResponseSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=preset["label"],
                value=key,
                description=truncate_text(preset["description"], 100),
            )
            for key, preset in AUTOMOD_REPORT_RESPONSE_PRESETS.items()
        ]
        super().__init__(
            placeholder="Respond to this report...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "custom":
            await interaction.response.send_modal(
                AutoModCustomReportResponseModal(
                    guild_id=self.parent_view.guild_id,
                    reporter_id=self.parent_view.reporter_id,
                    warning_id=self.parent_view.warning_id,
                    rule_name=self.parent_view.rule_name,
                    source_message=interaction.message,
                )
            )
            return

        preset = get_automod_report_preset(selected)
        await interaction.response.defer(ephemeral=True)
        success = await apply_automod_report_response(
            interaction,
            guild_id=self.parent_view.guild_id,
            reporter_id=self.parent_view.reporter_id,
            warning_id=self.parent_view.warning_id,
            rule_name=self.parent_view.rule_name,
            response_key=selected,
            response_text=preset["message"],
            source_message=interaction.message,
        )
        if success:
            await interaction.followup.send(
                embed=make_confirmation_embed(
                    "Report Response Sent",
                    f"> {preset['label']} was sent to the user.",
                    scope=SCOPE_MODERATION,
                    guild=interaction.guild,
                ),
                ephemeral=True,
            )


class AutoModReportResponseView(discord.ui.View):
    def __init__(self, *, guild_id: int, reporter_id: int, warning_id: str, rule_name: str):
        super().__init__(timeout=604800)
        self.guild_id = guild_id
        self.reporter_id = reporter_id
        self.warning_id = warning_id
        self.rule_name = rule_name
        self.add_item(AutoModReportResponseSelect(self))


class AutoModReportModal(discord.ui.Modal, title="Report AutoMod Warning"):
    why_incorrect = discord.ui.TextInput(
        label="What was wrong?",
        style=discord.TextStyle.paragraph,
        max_length=600,
        placeholder="Explain why you think the filter was wrong.",
    )
    extra_context = discord.ui.TextInput(
        label="Anything else staff should know?",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=600,
        placeholder="Context, screenshots, or what you were trying to say.",
    )

    def __init__(self, *, guild_id: int, warning_id: str, rule_id: int, rule_name: str, content: str, matched_keyword: Optional[str]):
        super().__init__()
        self.guild_id = guild_id
        self.warning_id = warning_id
        self.rule_id = rule_id
        self.rule_name = rule_name
        self.content = content
        self.matched_keyword = matched_keyword

    async def on_submit(self, interaction: discord.Interaction):
        guild = bot.get_guild(self.guild_id) or get_primary_guild()
        if guild is None:
            await interaction.response.send_message("The server for this report could not be resolved.", ephemeral=True)
            return

        channel_id = (
            bot.data_manager.config.get("automod_report_channel_id")
            or bot.data_manager.config.get("appeal_channel_id")
            or get_punishment_log_channel_id()
        )
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            await interaction.response.send_message("No AutoMod report channel is configured yet. Please contact staff directly.", ephemeral=True)
            return

        embed = make_action_log_embed(
            "AutoMod Report Submitted",
            "A user reported that a native AutoMod warning may have been incorrect.",
            guild=guild,
            kind="warning",
            scope=SCOPE_MODERATION,
            actor=format_user_ref(interaction.user),
            target=self.rule_name,
            reason="User reported a possible false positive.",
            message=self.content or '[Unavailable]',
            notes=[
                f"Rule ID: {self.rule_id}",
                f"Matched Keyword: {self.matched_keyword or 'Unknown'}",
                f"User Report: {truncate_text(self.why_incorrect.value, 500)}",
                f"Extra Context: {truncate_text(self.extra_context.value, 500) if self.extra_context.value else 'None'}",
            ],
            thumbnail=interaction.user.display_avatar.url,
            author_name=f"{interaction.user.display_name} ({interaction.user.id})",
            author_icon=interaction.user.display_avatar.url,
        )
        await channel.send(
            embed=normalize_log_embed(embed, guild=guild),
            view=AutoModReportResponseView(
                guild_id=guild.id,
                reporter_id=interaction.user.id,
                warning_id=self.warning_id,
                rule_name=self.rule_name,
            ),
        )
        await interaction.response.send_message(
            embed=make_confirmation_embed(
                "Report Sent",
                "> Your AutoMod report was sent to the staff team for review.",
                scope=SCOPE_MODERATION,
                guild=guild,
            ),
            ephemeral=True,
        )


class AutoModWarningView(discord.ui.View):
    def __init__(self, *, guild_id: int, warning_id: str, rule_id: int, rule_name: str, content: str, matched_keyword: Optional[str]):
        super().__init__(timeout=86400)
        self.guild_id = guild_id
        self.warning_id = warning_id
        self.rule_id = rule_id
        self.rule_name = rule_name
        self.content = truncate_text(content or "", 1000)
        self.matched_keyword = matched_keyword

    @discord.ui.button(label="Report to Moderator", style=discord.ButtonStyle.secondary)
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            AutoModReportModal(
                guild_id=self.guild_id,
                warning_id=self.warning_id,
                rule_id=self.rule_id,
                rule_name=self.rule_name,
                content=self.content,
                matched_keyword=self.matched_keyword,
            )
        )


async def automod_cmd(interaction: discord.Interaction):
    if not get_feature_flag(bot.data_manager.config, "automod_panel", True):
        await respond_with_error(interaction, "The AutoMod panel is currently turned off in feature settings.", scope=SCOPE_MODERATION)
        return
    await interaction.response.send_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView(), ephemeral=True)
