from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import discord

from .models import CaseMetadata, EscalationStep, ValidationFinding


DEFAULT_SCHEMA_VERSION = 3
DEFAULT_CASE_STATUSES = {"open", "review", "appealed", "closed"}
DEFAULT_RESOLUTION_STATES = {"pending", "active", "resolved", "reversed", "expired"}
DEFAULT_TICKET_PRIORITIES = ("low", "normal", "high", "urgent")
DEFAULT_FEATURE_FLAGS = {
    "advanced_case_panel": True,
    "advanced_modmail": True,
    "setup_validation": True,
    "config_panel": True,
    "role_cleanup": True,
    "smart_automod": False,
    "native_automod_bridge": True,
    "automod_panel": True,
    "dm_modmail_prompt": True,
}
DEFAULT_CANNED_REPLIES = {
    "Acknowledged": "Your ticket has been received. A staff member will review it shortly.",
    "Need More Details": "Please send any extra context, message links, screenshots, or IDs that might help.",
    "Resolved": "The issue has been resolved on our side. Let us know if you need anything else.",
}
DEFAULT_ESCALATION_MATRIX = [
    {"minimum_points": 0, "mode": "base", "multiplier": 1, "force_ban": False, "label": "Standard"},
    {"minimum_points": 3, "mode": "escalated", "multiplier": 1, "force_ban": False, "label": "Escalated"},
    {"minimum_points": 8, "mode": "escalated", "multiplier": 2, "force_ban": False, "label": "Escalated x2"},
    {"minimum_points": 12, "mode": "escalated", "multiplier": 4, "force_ban": False, "label": "Escalated x4"},
    {"minimum_points": 16, "mode": "ban", "multiplier": 1, "force_ban": True, "label": "Auto Ban"},
]
DEFAULT_NATIVE_AUTOMOD_SETTINGS = {
    "enabled": True,
    "warning_dm_enabled": True,
    "report_button_enabled": True,
    "default_escalation": {
        "enabled": False,
        "threshold": 3,
        "window_minutes": 1440,
        "duration_minutes": 0,
        "punishment_type": "warn",
        "reason_template": "Repeated native AutoMod violations",
    },
    "rule_overrides": {},
    "immunity_roles": [],
    "immunity_users": [],
    "immunity_channels": [],
}
NATIVE_AUTOMOD_PUNISHMENT_TYPES = {"warn", "timeout", "kick", "ban"}
PERMISSIONS_MATRIX = {
    "case_panel": {"roles": ("role_mod", "role_admin", "role_owner", "role_community_manager"), "allow_admin": True},
    "modmail_panel": {"roles": ("role_mod", "role_admin", "role_owner", "role_community_manager"), "allow_admin": True},
    "setup_panel": {"roles": ("role_admin", "role_owner", "role_community_manager"), "allow_admin": True},
    "config_panel": {"roles": ("role_admin", "role_owner", "role_community_manager"), "allow_admin": True},
    "owner_panel": {"roles": ("role_owner",), "allow_admin": False},
}


def _unique_preserve_order(values: Iterable[Any]) -> List[Any]:
    seen = set()
    output = []
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def _parse_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(
    value: Any,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    normalized = _parse_int(value)
    if normalized is None:
        normalized = default
    if minimum is not None:
        normalized = max(minimum, normalized)
    if maximum is not None:
        normalized = min(maximum, normalized)
    return normalized


def sanitize_tags(values: Sequence[Any], *, limit: int = 8) -> List[str]:
    tags = []
    for value in values:
        tag = str(value).strip().lower()
        if not tag:
            continue
        tags.append(tag[:30])
    return _unique_preserve_order(tags)[:limit]


def sanitize_evidence_links(values: Sequence[Any], *, limit: int = 8) -> List[str]:
    links = []
    for value in values:
        url = str(value).strip()
        if url.startswith(("http://", "https://")):
            links.append(url[:300])
    return _unique_preserve_order(links)[:limit]


def sanitize_linked_cases(values: Sequence[Any], *, current_case_id: Optional[int] = None) -> List[int]:
    linked = []
    for value in values:
        case_id = None
        if isinstance(value, int):
            case_id = value
        elif str(value).isdigit():
            case_id = int(str(value))
        if case_id and case_id > 0 and case_id != current_case_id:
            linked.append(case_id)
    return _unique_preserve_order(linked)[:12]


def build_action_id(case_id: Optional[int]) -> Optional[str]:
    if isinstance(case_id, int) and case_id > 0:
        return f"CASE-{case_id:06d}"
    return None


def ensure_feature_flags(config: Dict[str, Any]) -> bool:
    changed = False
    flags = config.setdefault("feature_flags", {})
    if not isinstance(flags, dict):
        flags = {}
        config["feature_flags"] = flags
        changed = True

    for key, value in DEFAULT_FEATURE_FLAGS.items():
        if key not in flags:
            flags[key] = value
            changed = True
    return changed


def ensure_canned_replies(config: Dict[str, Any]) -> bool:
    changed = False
    replies = config.setdefault("modmail_canned_replies", {})
    if not isinstance(replies, dict):
        replies = {}
        config["modmail_canned_replies"] = replies
        changed = True
    for key, value in DEFAULT_CANNED_REPLIES.items():
        if key not in replies:
            replies[key] = value
            changed = True
    return changed


def get_feature_flag(config: Dict[str, Any], key: str, default: bool = False) -> bool:
    flags = config.get("feature_flags", {})
    if not isinstance(flags, dict):
        return default
    return bool(flags.get(key, default))


def get_escalation_steps(config: Dict[str, Any]) -> List[EscalationStep]:
    raw_steps = config.get("escalation_matrix", DEFAULT_ESCALATION_MATRIX)
    if not isinstance(raw_steps, list):
        raw_steps = DEFAULT_ESCALATION_MATRIX
    steps = []
    for payload in raw_steps:
        if isinstance(payload, dict):
            steps.append(EscalationStep.from_dict(payload))
    if not steps:
        steps = [EscalationStep.from_dict(payload) for payload in DEFAULT_ESCALATION_MATRIX]
    steps.sort(key=lambda step: step.minimum_points)
    return steps


def ensure_escalation_matrix(config: Dict[str, Any]) -> bool:
    if not isinstance(config.get("escalation_matrix"), list):
        config["escalation_matrix"] = copy.deepcopy(DEFAULT_ESCALATION_MATRIX)
        return True
    if not config["escalation_matrix"]:
        config["escalation_matrix"] = copy.deepcopy(DEFAULT_ESCALATION_MATRIX)
        return True
    return False


def _normalize_native_escalation(payload: Any) -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"])
    if not isinstance(payload, dict):
        return defaults

    normalized = defaults
    punishment_type = str(payload.get("punishment_type", defaults["punishment_type"]) or defaults["punishment_type"]).lower()
    if punishment_type not in NATIVE_AUTOMOD_PUNISHMENT_TYPES:
        punishment_type = defaults["punishment_type"]

    duration_minutes = _coerce_int(payload.get("duration_minutes"), defaults["duration_minutes"])
    if punishment_type == "timeout":
        duration_minutes = max(1, min(40320, duration_minutes or 60))
    elif punishment_type == "ban":
        duration_minutes = -1
    else:
        duration_minutes = 0

    normalized["enabled"] = bool(payload.get("enabled", defaults["enabled"]))
    normalized["threshold"] = _coerce_int(payload.get("threshold"), defaults["threshold"], minimum=1)
    normalized["window_minutes"] = _coerce_int(payload.get("window_minutes"), defaults["window_minutes"], minimum=1)
    normalized["duration_minutes"] = duration_minutes
    normalized["punishment_type"] = punishment_type
    normalized["reason_template"] = str(payload.get("reason_template", defaults["reason_template"]) or defaults["reason_template"])[:200]
    return normalized


def _normalize_native_escalation_step(payload: Any) -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"])
    if not isinstance(payload, dict):
        payload = {}

    punishment_type = str(payload.get("punishment_type", defaults["punishment_type"]) or defaults["punishment_type"]).lower()
    if punishment_type not in NATIVE_AUTOMOD_PUNISHMENT_TYPES:
        punishment_type = defaults["punishment_type"]

    threshold = _coerce_int(payload.get("threshold"), defaults["threshold"], minimum=1)
    window_minutes = _coerce_int(payload.get("window_minutes"), defaults["window_minutes"], minimum=1)
    duration_minutes = _coerce_int(payload.get("duration_minutes"), defaults["duration_minutes"])

    if punishment_type == "timeout":
        duration_minutes = max(1, min(40320, duration_minutes or 60))
    elif punishment_type == "ban":
        duration_minutes = -1
    else:
        duration_minutes = 0

    return {
        "threshold": threshold,
        "window_minutes": window_minutes,
        "duration_minutes": duration_minutes,
        "punishment_type": punishment_type,
    }


def _normalize_native_rule_override(payload: Any) -> Dict[str, Any]:
    defaults = copy.deepcopy(DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"])
    normalized = {
        "enabled": False,
        "reason_template": str(defaults["reason_template"]),
        "steps": [],
    }
    if not isinstance(payload, dict):
        return normalized

    normalized["enabled"] = bool(payload.get("enabled", normalized["enabled"]))
    normalized["reason_template"] = str(payload.get("reason_template", normalized["reason_template"]) or normalized["reason_template"])[:200]

    steps: List[Dict[str, Any]] = []
    raw_steps = payload.get("steps")
    if isinstance(raw_steps, list):
        for item in raw_steps:
            if isinstance(item, dict):
                steps.append(_normalize_native_escalation_step(item))
    elif any(key in payload for key in ("threshold", "window_minutes", "duration_minutes", "punishment_type")):
        steps.append(_normalize_native_escalation_step(payload))
        if "enabled" not in payload:
            normalized["enabled"] = True

    steps.sort(key=lambda step: (int(step.get("threshold", 1)), int(step.get("window_minutes", 1)), str(step.get("punishment_type", "warn"))))
    normalized["steps"] = steps[:5]
    if not normalized["steps"]:
        normalized["enabled"] = False
    return normalized


def ensure_native_automod_settings(config: Dict[str, Any]) -> bool:
    current = config.get("native_automod")
    normalized = copy.deepcopy(DEFAULT_NATIVE_AUTOMOD_SETTINGS)
    changed = False

    if isinstance(current, dict):
        normalized["enabled"] = bool(current.get("enabled", normalized["enabled"]))
        normalized["warning_dm_enabled"] = bool(current.get("warning_dm_enabled", normalized["warning_dm_enabled"]))
        normalized["report_button_enabled"] = bool(current.get("report_button_enabled", normalized["report_button_enabled"]))
        normalized["default_escalation"] = _normalize_native_escalation(current.get("default_escalation"))

        overrides = current.get("rule_overrides", {})
        clean_overrides: Dict[str, Dict[str, Any]] = {}
        if isinstance(overrides, dict):
            for key, payload in overrides.items():
                rule_key = str(key).strip()
                if not rule_key:
                    continue
                clean_overrides[rule_key] = _normalize_native_rule_override(payload)
        normalized["rule_overrides"] = clean_overrides

        for key in ("immunity_roles", "immunity_users", "immunity_channels"):
            values = current.get(key, [])
            if isinstance(values, list):
                normalized[key] = [
                    parsed
                    for value in values
                    if (parsed := _parse_int(value)) is not None
                ]
    else:
        changed = True

    if current != normalized:
        config["native_automod"] = normalized
        changed = True
    return changed


def get_native_automod_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    payload = copy.deepcopy(config.get("native_automod", DEFAULT_NATIVE_AUTOMOD_SETTINGS))
    shadow = {"native_automod": payload}
    ensure_native_automod_settings(shadow)
    return shadow["native_automod"]


def resolve_native_automod_policy(config: Dict[str, Any], *, rule_id: Optional[int] = None, rule_name: Optional[str] = None) -> Dict[str, Any]:
    settings = get_native_automod_settings(config)
    overrides = settings.get("rule_overrides", {})

    for candidate in (rule_id, rule_name):
        if candidate is None:
            continue
        key = str(candidate)
        if key in overrides:
            return overrides[key]

    return {
        "enabled": False,
        "reason_template": str(DEFAULT_NATIVE_AUTOMOD_SETTINGS["default_escalation"]["reason_template"]),
        "steps": [],
    }


def resolve_escalation_duration(points: float, base: int, escalated: int, config: Dict[str, Any]) -> Tuple[int, bool, str]:
    steps = get_escalation_steps(config)
    selected = steps[0]
    for step in steps:
        if points >= step.minimum_points:
            selected = step

    if base == -1:
        return -1, False, "Standard (Ban Rule)"

    if selected.force_ban or selected.mode == "ban":
        return -1, True, selected.label or "Auto Ban"

    if selected.mode == "base":
        return base, False, selected.label or "Standard"

    duration = escalated
    if duration != -1:
        duration = duration * max(1, selected.multiplier)
        if duration > 40320:
            duration = -1

    return duration, True, selected.label or "Escalated"


def normalize_case_record(record: Dict[str, Any]) -> bool:
    changed = False
    metadata = CaseMetadata.from_record(record)

    case_id = record.get("case_id")
    if metadata.action_id != build_action_id(case_id):
        metadata.action_id = build_action_id(case_id)
        changed = True

    if metadata.status not in DEFAULT_CASE_STATUSES:
        metadata.status = "open"
        changed = True

    if metadata.resolution_state not in DEFAULT_RESOLUTION_STATES:
        punishment_type = record.get("type")
        if record.get("active"):
            metadata.resolution_state = "active"
        elif punishment_type == "warn":
            metadata.resolution_state = "resolved"
        else:
            metadata.resolution_state = "pending"
        changed = True

    normalized_tags = sanitize_tags(metadata.tags)
    if normalized_tags != metadata.tags:
        metadata.tags = normalized_tags
        changed = True

    normalized_links = sanitize_evidence_links(metadata.evidence_links)
    if normalized_links != metadata.evidence_links:
        metadata.evidence_links = normalized_links
        changed = True

    normalized_cases = sanitize_linked_cases(metadata.linked_cases, current_case_id=case_id)
    if normalized_cases != metadata.linked_cases:
        metadata.linked_cases = normalized_cases
        changed = True

    metadata.apply_to_record(record)
    return changed


def normalize_modmail_ticket(ticket: Dict[str, Any]) -> bool:
    changed = False
    defaults = {
        "status": "open",
        "priority": "normal",
        "tags": [],
        "assigned_moderator": None,
        "claimed_at": None,
        "last_user_message_at": ticket.get("created_at"),
        "last_staff_message_at": None,
        "last_sla_alert_at": None,
    }
    for key, value in defaults.items():
        if key not in ticket:
            ticket[key] = value
            changed = True

    normalized_priority = str(ticket.get("priority", "normal")).lower()
    if normalized_priority not in DEFAULT_TICKET_PRIORITIES:
        ticket["priority"] = "normal"
        changed = True

    tags = sanitize_tags(ticket.get("tags", []), limit=10)
    if tags != ticket.get("tags"):
        ticket["tags"] = tags
        changed = True
    return changed


def run_schema_migrations(
    config: Dict[str, Any],
    punishments: Dict[str, Any],
    modmail: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    changed = False
    notes: List[str] = []

    current_version = int(config.get("schema_version", 0) or 0)
    if current_version < DEFAULT_SCHEMA_VERSION:
        config["schema_version"] = DEFAULT_SCHEMA_VERSION
        changed = True
        notes.append(f"Schema upgraded to v{DEFAULT_SCHEMA_VERSION}.")

    if ensure_feature_flags(config):
        changed = True
        notes.append("Feature flags initialized.")

    if ensure_canned_replies(config):
        changed = True
        notes.append("Default canned replies initialized.")

    if ensure_escalation_matrix(config):
        changed = True
        notes.append("Escalation matrix initialized.")

    if ensure_native_automod_settings(config):
        changed = True
        notes.append("Native automod settings initialized.")

    if "modmail_sla_minutes" not in config:
        config["modmail_sla_minutes"] = 60
        changed = True
        notes.append("Modmail SLA default initialized.")

    for records in punishments.values():
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict) and normalize_case_record(record):
                changed = True

    for ticket in modmail.values():
        if isinstance(ticket, dict) and normalize_modmail_ticket(ticket):
            changed = True

    return changed, notes


def find_case_record(punishments: Dict[str, Any], case_id: int) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    for user_id, records in punishments.items():
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict) and record.get("case_id") == case_id:
                return user_id, record
    return None, None


def export_case_payload(user_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
    payload = copy.deepcopy(record)
    payload["target_user_id"] = user_id
    return payload


def export_config_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    payload = copy.deepcopy(config)
    payload.pop("bot_token", None)
    return payload


def import_config_payload(current_config: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    merged = copy.deepcopy(current_config)
    warnings: List[str] = []

    safe_payload = dict(payload)
    if "bot_token" in safe_payload:
        safe_payload.pop("bot_token")
        warnings.append("Ignored bot_token during import for safety.")

    for key, value in safe_payload.items():
        merged[key] = value

    ensure_feature_flags(merged)
    ensure_canned_replies(merged)
    ensure_escalation_matrix(merged)
    ensure_native_automod_settings(merged)
    merged["schema_version"] = DEFAULT_SCHEMA_VERSION
    return merged, warnings


def has_capability(
    role_ids: Sequence[int],
    capability: str,
    config: Dict[str, Any],
    *,
    administrator: bool = False,
    user_id: Optional[int] = None,
    guild_owner_id: Optional[int] = None,
) -> bool:
    rules = PERMISSIONS_MATRIX.get(capability)
    if not rules:
        return administrator

    if guild_owner_id is not None and user_id == guild_owner_id:
        return True
    if rules.get("allow_admin") and administrator:
        return True

    allowed_roles = {
        role_id
        for role_key in rules.get("roles", ())
        if (role_id := _parse_int(config.get(role_key))) is not None
    }
    return any(role_id in allowed_roles for role_id in role_ids)


def validate_guild_configuration(config: Dict[str, Any], guild: discord.Guild, me: discord.Member) -> List[ValidationFinding]:
    findings: List[ValidationFinding] = []

    role_checks = [
        ("role_owner", "Owner Role"),
        ("role_admin", "Admin Role"),
        ("role_mod", "Mod Role"),
        ("role_community_manager", "Community Manager Role"),
        ("role_anchor", "Anchor Role"),
    ]
    for key, label in role_checks:
        role_id = _parse_int(config.get(key))
        if role_id is None or not guild.get_role(role_id):
            findings.append(ValidationFinding("error", "Roles", f"{label} is missing or invalid."))

    general_log_channel_id = config.get("general_log_channel_id") or config.get("log_channel_id")
    punishment_log_channel_id = config.get("punishment_log_channel_id")
    channel_checks = [
        (general_log_channel_id, "General Bot Log Channel"),
        (punishment_log_channel_id, "Punishment Log Channel"),
        (config.get("appeal_channel_id"), "Appeal Channel"),
        (config.get("automod_log_channel_id"), "AutoMod Log Channel"),
        (config.get("automod_report_channel_id"), "AutoMod Report Channel"),
        (config.get("modmail_inbox_channel"), "Modmail Inbox"),
        (config.get("modmail_action_log_channel"), "Modmail Action Log"),
        (config.get("modmail_panel_channel"), "Modmail Panel Channel"),
    ]
    for channel_id, label in channel_checks:
        normalized_channel_id = _parse_int(channel_id)
        if normalized_channel_id is not None and not guild.get_channel(normalized_channel_id):
            findings.append(ValidationFinding("error", "Channels", f"{label} points to a missing channel."))

    archive_category = _parse_int(config.get("category_archive"))
    if archive_category is not None:
        channel = guild.get_channel(archive_category)
        if channel and not isinstance(channel, discord.CategoryChannel):
            findings.append(ValidationFinding("error", "Channels", "Archive category is set to a non-category channel."))
        elif channel is None:
            findings.append(ValidationFinding("error", "Channels", "Archive category is missing."))

    required_permissions = (
        "manage_roles",
        "manage_channels",
        "moderate_members",
        "ban_members",
        "manage_messages",
        "read_message_history",
        "send_messages",
    )
    for permission_name in required_permissions:
        if not getattr(me.guild_permissions, permission_name, False):
            findings.append(ValidationFinding("warning", "Permissions", f"Bot is missing `{permission_name}` permission."))

    modmail_inbox_id = _parse_int(config.get("modmail_inbox_channel"))
    if modmail_inbox_id is not None:
        inbox = guild.get_channel(modmail_inbox_id)
        if inbox and isinstance(inbox, discord.TextChannel):
            perms = inbox.permissions_for(me)
            if not perms.create_public_threads and not perms.create_private_threads:
                findings.append(ValidationFinding("warning", "Permissions", "Bot cannot create threads in the modmail inbox."))

    modmail_panel_id = _parse_int(config.get("modmail_panel_channel"))
    if modmail_panel_id is not None:
        panel_channel = guild.get_channel(modmail_panel_id)
        if panel_channel and isinstance(panel_channel, discord.TextChannel):
            perms = panel_channel.permissions_for(me)
            if not perms.send_messages:
                findings.append(ValidationFinding("warning", "Permissions", "Bot cannot send messages in the modmail panel channel."))
            if not perms.embed_links:
                findings.append(ValidationFinding("warning", "Permissions", "Bot cannot embed links in the modmail panel channel."))

    if not findings:
        findings.append(ValidationFinding("success", "Validation", "No issues detected in the current setup."))

    return findings


def ticket_needs_sla_alert(ticket: Dict[str, Any], now: datetime, sla_minutes: int) -> bool:
    if ticket.get("status") != "open":
        return False
    last_user_message_at = ticket.get("last_user_message_at")
    if not last_user_message_at:
        return False
    try:
        last_user_dt = datetime.fromisoformat(str(last_user_message_at))
    except ValueError:
        return False
    if last_user_dt.tzinfo is None:
        last_user_dt = last_user_dt.replace(tzinfo=timezone.utc)

    last_staff_message_at = ticket.get("last_staff_message_at")
    if last_staff_message_at:
        try:
            staff_dt = datetime.fromisoformat(str(last_staff_message_at))
            if staff_dt.tzinfo is None:
                staff_dt = staff_dt.replace(tzinfo=timezone.utc)
            if staff_dt >= last_user_dt:
                return False
        except ValueError:
            pass

    threshold = last_user_dt + timedelta(minutes=max(1, sla_minutes))
    if now < threshold:
        return False

    last_alert = ticket.get("last_sla_alert_at")
    if last_alert:
        try:
            alert_dt = datetime.fromisoformat(str(last_alert))
            if alert_dt.tzinfo is None:
                alert_dt = alert_dt.replace(tzinfo=timezone.utc)
            if alert_dt >= last_user_dt:
                return False
        except ValueError:
            pass

    return True
