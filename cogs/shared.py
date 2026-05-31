# modules/commands/shared.py
# Shared utilities, embed helpers, logging, and permission helpers.

import discord
import aiohttp
import asyncio
import ipaddress
import json
import os
import socket
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Union, Tuple, Any
from collections import Counter, defaultdict
import re
import io
import logging
from pathlib import Path
from urllib.parse import urlsplit

from core.constants import (
    BRAND_NAME,
    DEFAULT_ANCHOR_ROLE_ID,
    DEFAULT_GUILD_ID,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    DEFAULT_ROLE_OWNER,
    DEFAULT_RULES,
    EMBED_PALETTE,
    MODMAIL_PANEL_BANNER_URL,
    SCOPE_ANALYTICS,
    SCOPE_MODERATION,
    SCOPE_ROLES,
    SCOPE_SUPPORT,
    SCOPE_SYSTEM,
    TOKEN_ENV_VARS,
)
from core.services import (
    DEFAULT_SCHEMA_VERSION,
    get_feature_flag,
    get_escalation_steps,
    get_native_automod_settings,
    has_capability,
    resolve_escalation_duration,
)
from core.context import bot
from core.utils import iso_to_dt, truncate_text, format_duration, create_progress_bar

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("MGXBot")
logging.getLogger("discord.http").setLevel(logging.ERROR)

# ----------------- PATHS -----------------
BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "database"
ROLES_FILE = DB_DIR / "roles.json"
CONFIG_FILE = DB_DIR / "config.json"
PUNISHMENTS_FILE = DB_DIR / "punishments.json"
MOD_STATS_FILE = DB_DIR / "mod_stats.json"
MESSAGE_CACHE_FILE = DB_DIR / "message_cache.json"
PINGS_FILE = DB_DIR / "pings.json"
LOCKDOWN_FILE = DB_DIR / "lockdown.json"
MODMAIL_FILE = DB_DIR / "modmail.json"
# -----------------------------------------

def read_json_file(path: Path, default: Any):
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path.name, exc)
    return default


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def resolve_bot_token() -> str:
    bootstrap_config = read_json_file(CONFIG_FILE, {})
    env_var_order: List[str] = []

    configured_env_var = bootstrap_config.get("token_env_var")
    if isinstance(configured_env_var, str) and configured_env_var.strip():
        env_var_order.append(configured_env_var.strip())

    for env_var in TOKEN_ENV_VARS:
        if env_var not in env_var_order:
            env_var_order.append(env_var)

    for env_var in env_var_order:
        token = os.getenv(env_var)
        if token:
            return token.strip()

    raise RuntimeError(
        "Discord bot token is not configured. Set one of the supported environment variables "
        f"({', '.join(env_var_order)})."
    )


# Runtime bootstrap lives in modules.bot.

def calculate_smart_punishment(user_id: str, reason: str, rules: dict, history: list) -> tuple[int, bool, str]:
    """
    Internal Point System Calculation:
    - Lookback: 90 days.
    - Points:
        - Standard: Different=1, Same=4
        - Light: Different=0.5, Same=2
    
    Light Offenses: Spamming, Begging, Political, Inappropriate Lang, Off-Topic, Argumentative
    
    Thresholds:
    - 0-2 points: Tier 0 (Base)
    - 3-7 points: Tier 1 (Escalated)
    - 8-11 points: Tier 2 (Escalated x2)
    - 12+ points: Tier 3 (Escalated x4 or Ban)
    - 16+ points: Tier 4 (Auto-Ban)
    """
    now = discord.utils.utcnow()
    lookback_days = 90
    
    light_offenses = {
        "Spamming", "Begging", "Political", "Inappropriate Lang", 
        "Off-Topic", "Argumentative"
    }
    
    points = 0
    has_same_offense = False
    
    for rec in history:
        ts_str = rec.get("timestamp")
        if not ts_str: continue
        dt = iso_to_dt(ts_str)
        if not dt: continue
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            
        if (now - dt).days <= lookback_days:
            rec_reason = rec.get("reason")
            is_light = rec_reason in light_offenses
            
            if rec_reason == reason:
                points += 2 if is_light else 4
                has_same_offense = True
            else:
                points += 0.5 if is_light else 1
    
    base = rules.get("base", 0)
    esc = rules.get("escalated", 0)
    config = bot.data_manager.config if getattr(bot, "data_manager", None) else {}
    duration, escalated, label = resolve_escalation_duration(points, base, esc, config)

    if not escalated:
        return duration, False, label

    context = "Recidivism" if has_same_offense else "General Toxicity"
    return duration, True, f"{label} ({context})"

# ----------------- Security & Utils -----------------
DANGEROUS_PERMISSIONS = {
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "ban_members",
    "kick_members",
    "manage_webhooks",
    "mention_everyone"
}

ROLE_ICON_MAX_BYTES = 256000
MODMAIL_RELAY_MAX_FILES = 5
MODMAIL_RELAY_MAX_FILE_BYTES = 8 * 1024 * 1024
MODMAIL_RELAY_MAX_TOTAL_BYTES = 20 * 1024 * 1024

def has_dangerous_perm(perms: discord.Permissions) -> bool:
    for p in DANGEROUS_PERMISSIONS:
        if getattr(perms, p, False):
            return True
    return False

# ----------------- Utility functions -----------------
def get_custom_role_limit(member: discord.Member) -> int:
    conf = bot.data_manager.config
    uid = str(member.id)
    
    # 1. Check Blacklists
    if uid in conf.get("cr_blacklist_users", []):
        return 0
    
    blocked_roles = conf.get("cr_blacklist_roles", [])
    for r in member.roles:
        if str(r.id) in blocked_roles:
            return 0
            
    limit = 0

    # Server boosters receive at least one personal role slot.
    if member.premium_since is not None:
        limit = 1
    
    # 2. Check User Whitelist
    wl_users = conf.get("cr_whitelist_users", {})
    if uid in wl_users:
        limit = max(limit, int(wl_users[uid]))
        
    # 3. Check Role Whitelist
    wl_roles = conf.get("cr_whitelist_roles", {})
    for r in member.roles:
        rid = str(r.id)
        if rid in wl_roles:
            limit = max(limit, int(wl_roles[rid]))
            
    return limit

def hex_valid(s: str) -> bool:
    if not isinstance(s, str): return False
    s = s.strip()
    if len(s) != 7 or not s.startswith("#"): return False
    try:
        int(s[1:], 16)
        return True
    except ValueError:
        return False

async def _resolve_image_host_addresses(hostname: str) -> Tuple[List[str], Optional[str]]:
    try:
        return [str(ipaddress.ip_address(hostname))], None
    except ValueError:
        pass

    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return [], "Image host could not be resolved."
    except Exception:
        return [], "Image host could not be validated."

    addresses: List[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        return [], "Image host could not be resolved."
    return addresses, None


def _is_public_image_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def validate_image_fetch_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    candidate = str(url or "").strip()
    parsed = urlsplit(candidate)

    if parsed.scheme.lower() != "https":
        return None, "Image URLs must use HTTPS."
    if parsed.username or parsed.password:
        return None, "Image URLs with embedded credentials are not allowed."
    if not parsed.hostname:
        return None, "Image URL must include a hostname."

    addresses, error = await _resolve_image_host_addresses(parsed.hostname)
    if error:
        return None, error
    if any(not _is_public_image_ip(address) for address in addresses):
        return None, "Image URLs must use a public host."
    return candidate, None


async def fetch_image_bytes(
    url: str,
    timeout: int = 10,
    max_bytes: int = ROLE_ICON_MAX_BYTES,
) -> Tuple[Optional[bytes], Optional[str]]:
    if not bot.session:
        return None, "Image download is unavailable right now."

    validated_url, error = await validate_image_fetch_url(url)
    if error:
        return None, error

    try:
        request_timeout = aiohttp.ClientTimeout(total=timeout)
        async with bot.session.get(
            validated_url,
            timeout=request_timeout,
            allow_redirects=False,
            headers={"Accept": "image/*"},
        ) as resp:
            if 300 <= resp.status < 400:
                return None, "Image URLs cannot redirect."
            if resp.status != 200:
                return None, "Failed to download image. Check the URL."

            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                return None, "URL did not return an image."

            content_length = resp.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        return None, "Image too big! Max size is 256KB."
                except ValueError:
                    pass

            payload = bytearray()
            async for chunk in resp.content.iter_chunked(16384):
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    return None, "Image too big! Max size is 256KB."
            return bytes(payload), None
    except asyncio.TimeoutError:
        return None, "Image download timed out."
    except aiohttp.ClientError:
        return None, "Failed to download image. Check the URL."
    except Exception:
        return None, "Failed to download image. Check the URL."


async def prepare_modmail_relay_attachments(attachments) -> Tuple[List[discord.File], Optional[str]]:
    files: List[discord.File] = []
    skipped_extra = 0
    skipped_oversized = 0
    skipped_total_limit = 0
    total_bytes = 0

    for attachment in attachments:
        if len(files) >= MODMAIL_RELAY_MAX_FILES:
            skipped_extra += 1
            continue

        size = int(getattr(attachment, "size", 0) or 0)
        if size > MODMAIL_RELAY_MAX_FILE_BYTES:
            skipped_oversized += 1
            continue
        if total_bytes + size > MODMAIL_RELAY_MAX_TOTAL_BYTES:
            skipped_total_limit += 1
            continue

        files.append(await attachment.to_file())
        total_bytes += size

    notices = []
    if skipped_extra:
        notices.append(f"Skipped {skipped_extra} attachment(s) after the first {MODMAIL_RELAY_MAX_FILES}.")
    if skipped_oversized:
        notices.append("Skipped attachment(s) over 8 MiB.")
    if skipped_total_limit:
        notices.append("Skipped attachment(s) to stay under 20 MiB total.")
    if notices:
        return files, "Some attachments were not relayed. " + " ".join(notices)
    return files, None


async def send_modmail_thread_intro(thread: discord.Thread, user, category: str, fields_data: List[str]) -> None:
    await thread.send(
        f"**Ticket Created**\nUser: {user.mention}\nCategory: {category}\n\n" + "\n".join(fields_data),
        allowed_mentions=discord.AllowedMentions.none(),
    )

def format_log_quote(value: Optional[str], *, limit: int = 1000) -> str:
    text = truncate_text(str(value or "None").strip(), limit)
    return f">>> {text}" if text else ">>> None"


def format_plain_log_block(*lines: Optional[str], limit: int = 1000) -> str:
    cleaned: List[str] = []
    for line in lines:
        for raw_part in str(line or "").splitlines():
            value = raw_part.strip()
            if not value:
                continue
            if value.startswith(">>>"):
                value = value[3:].strip()
            elif value.startswith("> "):
                value = value[2:].strip()
            elif value.startswith(">"):
                value = value[1:].strip()
            if value:
                cleaned.append(value)
    if not cleaned:
        return "None"
    return truncate_text("\n".join(cleaned), limit)


def format_reason_value(value: Optional[str], *, limit: int = 1000) -> str:
    text = truncate_text(str(value or "None").strip(), limit)
    if not text:
        return "> None"
    if text.startswith(">"):
        return text
    return f"> {text}"


def format_log_notes(*lines: Optional[str], limit: int = 1000) -> str:
    cleaned = []
    for line in lines:
        value = str(line or "").strip()
        if not value:
            continue
        if value.startswith("- ") or value.startswith("> "):
            value = value[2:]
        cleaned.append(value)
    if not cleaned:
        return "> None"
    return truncate_text("\n".join(f"> {line}" for line in cleaned), limit)


UNDO_REASON_PRESETS = [
    {
        "value": "appeal_accepted",
        "label": "Appeal accepted",
        "description": "Staff approved the user's appeal and reversed the action.",
    },
    {
        "value": "staff_error",
        "label": "Staff error",
        "description": "The action was applied incorrectly or by mistake.",
    },
    {
        "value": "insufficient_evidence",
        "label": "Insufficient evidence",
        "description": "The case does not have enough evidence to keep standing.",
    },
    {
        "value": "duplicate_case",
        "label": "Duplicate case",
        "description": "This case duplicated another punishment record.",
    },
    {
        "value": "policy_adjustment",
        "label": "Policy adjustment",
        "description": "Staff adjusted the outcome after further review.",
    },
]
UNDO_REASON_PRESET_MAP = {preset["value"]: preset for preset in UNDO_REASON_PRESETS}


LOG_QUOTE_FIELD_NAMES = {
    "message",
    "blocked message",
    "flagged message",
    "appeal statement",
    "original violation",
    "internal note",
    "message to user",
    "user report",
    "extra context",
    "details",
}
LOG_NONINLINE_FIELD_NAMES = {
    "message",
    "blocked message",
    "flagged message",
    "appeal statement",
    "original violation",
    "internal note",
    "message to user",
    "user report",
    "extra context",
    "escalation",
    "result",
    "reason template",
    "actions",
    "trigger",
}


def normalize_log_field_name(name: str) -> str:
    parts = []
    for raw_part in str(name or "Detail").strip().split():
        part = raw_part.strip()
        if not part:
            continue
        lowered = part.lower()
        if lowered in {"id", "dm", "sla", "url"}:
            parts.append(lowered.upper())
        else:
            parts.append(part[0].upper() + part[1:])
    return truncate_text(" ".join(parts) or "Detail", 256)


def format_log_field_value(name: str, value: Optional[str], *, limit: int = 1024) -> str:
    field_name = str(name or "").strip().lower()
    text = truncate_text(str(value or "None").strip() or "None", limit if field_name not in LOG_QUOTE_FIELD_NAMES else min(limit, 950))
    if field_name in LOG_QUOTE_FIELD_NAMES:
        return format_log_quote(text, limit=min(limit, 950))
    return text


def build_log_detail_fields(*lines: Optional[str], limit: int = 8) -> List[Tuple[str, str, bool]]:
    detail_fields = []
    for line in lines:
        value = str(line or "").strip()
        if not value:
            continue
        value = value[2:] if value.startswith("- ") else value
        if ":" in value:
            name, detail_value = value.split(":", 1)
            name = normalize_log_field_name(name)
            detail_value = detail_value.strip() or "None"
        else:
            name = "Detail"
            detail_value = value
        lowered = name.lower()
        formatted_value = format_log_field_value(name, detail_value)
        inline = len(str(detail_value)) <= 80 and lowered not in LOG_NONINLINE_FIELD_NAMES
        detail_fields.append((name, formatted_value, inline))
        if len(detail_fields) >= limit:
            break
    return detail_fields


def make_action_log_embed(
    title: str,
    description: str,
    *,
    guild: discord.Guild,
    kind: str = "info",
    scope: str = SCOPE_MODERATION,
    actor: Optional[str] = None,
    target: Optional[str] = None,
    reason: Optional[str] = None,
    duration: Optional[str] = None,
    expires: Optional[str] = None,
    message: Optional[str] = None,
    notes: Optional[List[str]] = None,
    thumbnail: Optional[str] = None,
    author_name: Optional[str] = None,
    author_icon: Optional[str] = None,
) -> discord.Embed:
    embed = make_embed(
        title,
        description if description.startswith(">") else f"> {description}",
        kind=kind,
        scope=scope,
        guild=guild,
        thumbnail=thumbnail,
        author_name=author_name,
        author_icon=author_icon,
    )
    if actor:
        embed.add_field(name="Actor", value=actor, inline=True)
    if target:
        embed.add_field(name="Target", value=target, inline=True)
    if reason:
        embed.add_field(name="Reason", value=format_reason_value(reason, limit=500), inline=False)
    if duration:
        embed.add_field(name="Duration", value=duration, inline=True)
    if expires:
        embed.add_field(name="Expires", value=expires, inline=True)
    if message:
        embed.add_field(name="Message", value=format_log_quote(message, limit=900), inline=False)
    if notes:
        for detail_name, detail_value, detail_inline in build_log_detail_fields(*notes):
            embed.add_field(name=detail_name, value=detail_value, inline=detail_inline)
    return embed


def normalize_log_embed(embed: discord.Embed, *, guild: Optional[discord.Guild] = None) -> discord.Embed:
    payload = embed.to_dict()
    description = payload.get("description")
    if description and not str(description).startswith(">"):
        payload["description"] = f"> {description}"

    normalized_fields = []
    for field in payload.get("fields", []):
        name = str(field.get("name", ""))
        value = str(field.get("value", ""))
        lowered = name.lower()
        if lowered == "reason":
            field["value"] = truncate_text(format_reason_value(value, limit=950), 1024)
            field["inline"] = False
            normalized_fields.append(field)
            continue
        if lowered in LOG_QUOTE_FIELD_NAMES:
            stripped = value.strip()
            if not stripped.startswith((">>>", "```")):
                value = format_log_field_value(name, stripped)
            field["value"] = truncate_text(value, 1024)
            normalized_fields.append(field)
            continue
        if lowered == "notes":
            detail_fields = build_log_detail_fields(*[line.strip() for line in value.splitlines() if line.strip()], limit=10)
            if detail_fields:
                for detail_name, detail_value, detail_inline in detail_fields:
                    normalized_fields.append({
                        "name": detail_name,
                        "value": truncate_text(detail_value, 1024),
                        "inline": detail_inline,
                    })
                continue
        field["value"] = truncate_text(value, 1024)
        normalized_fields.append(field)
    payload["fields"] = normalized_fields

    normalized = discord.Embed.from_dict(payload)
    footer = embed.footer
    if footer and footer.text:
        normalized.set_footer(text=footer.text, icon_url=footer.icon_url)
    else:
        brand_embed(normalized, guild=guild)
    if embed.author and embed.author.name:
        normalized.set_author(name=embed.author.name, icon_url=embed.author.icon_url)
    if embed.thumbnail and embed.thumbnail.url:
        normalized.set_thumbnail(url=embed.thumbnail.url)
    if embed.image and embed.image.url:
        normalized.set_image(url=embed.image.url)
    return normalized


def get_general_log_channel_ids(config: Optional[dict] = None) -> List[int]:
    config = config or bot.data_manager.config
    channel_ids: List[int] = []
    for raw_channel_id in (
        config.get("general_log_channel_id"),
        config.get("log_channel_id"),
    ):
        if not raw_channel_id:
            continue
        try:
            channel_id = int(raw_channel_id)
        except (TypeError, ValueError):
            continue
        if channel_id not in channel_ids:
            channel_ids.append(channel_id)
    return channel_ids


def get_general_log_channel_id(config: Optional[dict] = None) -> Optional[int]:
    channel_ids = get_general_log_channel_ids(config)
    return channel_ids[0] if channel_ids else None


def get_punishment_log_channel_ids(config: Optional[dict] = None) -> List[int]:
    config = config or bot.data_manager.config
    channel_ids: List[int] = []
    for raw_channel_id in (
        config.get("punishment_log_channel_id"),
        *get_general_log_channel_ids(config),
    ):
        if not raw_channel_id:
            continue
        try:
            channel_id = int(raw_channel_id)
        except (TypeError, ValueError):
            continue
        if channel_id not in channel_ids:
            channel_ids.append(channel_id)
    return channel_ids


def get_punishment_log_channel_id(config: Optional[dict] = None) -> Optional[int]:
    channel_ids = get_punishment_log_channel_ids(config)
    return channel_ids[0] if channel_ids else None


async def _send_log_to_channels(
    guild: discord.Guild,
    channel_ids: List[int],
    embed: discord.Embed,
    *,
    content: Optional[str] = None,
    view: Optional[discord.ui.View] = None,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
    log_label: str = "log",
) -> bool:
    if not channel_ids:
        return False

    normalized_embed = normalize_log_embed(embed, guild=guild)
    for channel_id in channel_ids:
        channel = guild.get_channel_or_thread(channel_id) or guild.get_channel(channel_id)
        if channel is None:
            logger.warning("Configured %s channel %s was not found in guild %s.", log_label, channel_id, guild.id)
            continue
        try:
            files = None
            if attachments:
                files = [discord.File(io.BytesIO(data), filename=filename) for filename, data in attachments]
            await channel.send(content=content, embed=normalized_embed, view=view, files=files)
            return True
        except Exception as exc:
            logger.warning("Failed to send %s to channel %s: %s", log_label, channel_id, exc)
    return False


async def send_log(
    guild: discord.Guild,
    embed: discord.Embed,
    content: str = None,
    view: discord.ui.View = None,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
):
    await _send_log_to_channels(
        guild,
        get_general_log_channel_ids(),
        embed,
        content=content,
        view=view,
        attachments=attachments,
        log_label="general log",
    )


async def send_punishment_log(
    guild: discord.Guild,
    embed: discord.Embed,
    content: str = None,
    view: discord.ui.View = None,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
):
    await _send_log_to_channels(
        guild,
        get_punishment_log_channel_ids(),
        embed,
        content=content,
        view=view,
        attachments=attachments,
        log_label="punishment log",
    )

def get_valid_duration(minutes: int) -> timedelta:
    # Discord max timeout is 28 days (40320 minutes)
    return timedelta(minutes=min(minutes, 40320))

def has_permission_capability(interaction: discord.Interaction, capability: str) -> bool:
    return has_capability(
        [role.id for role in interaction.user.roles],
        capability,
        bot.data_manager.config,
        administrator=interaction.user.guild_permissions.administrator,
        user_id=interaction.user.id,
        guild_owner_id=interaction.guild.owner_id if interaction.guild else None,
    )


async def respond_with_error(interaction: discord.Interaction, message: str, *, scope: str = SCOPE_SYSTEM):
    embed = make_error_embed("Request Failed", f"> {message}", scope=scope, guild=interaction.guild)
    if not interaction.response.is_done():
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


def is_staff_member(member: discord.Member) -> bool:
    conf = bot.data_manager.config
    allowed = {
        conf.get("role_mod", DEFAULT_ROLE_MOD),
        conf.get("role_admin", DEFAULT_ROLE_ADMIN),
        conf.get("role_owner", DEFAULT_ROLE_OWNER),
        conf.get("role_community_manager", DEFAULT_ROLE_COMMUNITY_MANAGER),
    }
    if any(role.id in allowed for role in member.roles):
        return True
    mod_roles = bot.data_manager.config.get("mod_roles", [])
    if any(role.id in mod_roles for role in member.roles):
        return True
    return member.guild_permissions.moderate_members


def is_staff(interaction: discord.Interaction) -> bool:
    if has_permission_capability(interaction, "case_panel"):
        return True
    mod_roles = bot.data_manager.config.get("mod_roles", [])
    if any(r.id in mod_roles for r in interaction.user.roles):
        return True
    return interaction.user.guild_permissions.moderate_members




async def resolve_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    member = guild.get_member(user_id)
    if member:
        return member

    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def make_embed(
    title: str,
    description: Optional[str] = None,
    *,
    kind: str = "neutral",
    scope: str = SCOPE_SYSTEM,
    guild: Optional[discord.Guild] = None,
    thumbnail: Optional[str] = None,
    author_name: Optional[str] = None,
    author_icon: Optional[str] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=EMBED_PALETTE.get(kind, EMBED_PALETTE["neutral"]),
    )
    embed.timestamp = discord.utils.utcnow()
    footer_text = f"{BRAND_NAME} • {scope}"
    if guild and guild.icon:
        embed.set_footer(text=footer_text, icon_url=guild.icon.url)
    else:
        embed.set_footer(text=footer_text)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if author_name:
        embed.set_author(name=author_name, icon_url=author_icon)
    return embed


def brand_embed(
    embed: discord.Embed,
    *,
    guild: Optional[discord.Guild] = None,
    scope: str = SCOPE_SYSTEM,
) -> discord.Embed:
    embed.timestamp = discord.utils.utcnow()
    footer_text = f"{BRAND_NAME} • {scope}"
    if guild and guild.icon:
        embed.set_footer(text=footer_text, icon_url=guild.icon.url)
    else:
        embed.set_footer(text=footer_text)
    return embed


def make_empty_state_embed(
    title: str,
    description: str,
    *,
    scope: str = SCOPE_SYSTEM,
    guild: Optional[discord.Guild] = None,
    thumbnail: Optional[str] = None,
) -> discord.Embed:
    return make_embed(title, description, kind="muted", scope=scope, guild=guild, thumbnail=thumbnail)


def make_error_embed(
    title: str,
    description: str,
    *,
    scope: str = SCOPE_SYSTEM,
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    return make_embed(title, description, kind="danger", scope=scope, guild=guild)


def make_confirmation_embed(
    title: str,
    description: str,
    *,
    scope: str = SCOPE_SYSTEM,
    guild: Optional[discord.Guild] = None,
    thumbnail: Optional[str] = None,
) -> discord.Embed:
    return make_embed(title, description, kind="success", scope=scope, guild=guild, thumbnail=thumbnail)


def make_analytics_card(
    title: str,
    *,
    description: Optional[str] = None,
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    return make_embed(title, description, kind="analytics", scope=SCOPE_ANALYTICS, guild=guild)


def join_lines(lines: List[str], fallback: str = "None") -> str:
    rendered = [line for line in lines if line]
    return "\n".join(rendered) if rendered else fallback


def upsert_embed_field(embed: discord.Embed, name: str, value: str, *, inline: bool = False):
    for index, field in enumerate(embed.fields):
        if field.name == name:
            embed.set_field_at(index, name=name, value=value, inline=inline)
            return
    embed.add_field(name=name, value=value, inline=inline)


def get_modal_item_label(item: discord.ui.Item) -> str:
    underlying = getattr(item, "_underlying", None)
    label = getattr(underlying, "label", None)
    if label:
        return str(label)
    return "Field"


def get_user_display_name(user: Union[discord.User, discord.Member]) -> str:
    raw_name = (
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or str(getattr(user, "id", "Unknown User"))
    )
    return truncate_text(discord.utils.escape_markdown(str(raw_name).strip() or "Unknown User"), 80)


def format_user_ref(user: Union[discord.User, discord.Member]) -> str:
    return f"{get_user_display_name(user)} • {user.mention} (`{user.id}`)"


def format_user_id_ref(user_id: Union[int, str], *, fallback_name: Optional[str] = None) -> str:
    prefix = ""
    if fallback_name:
        clean_name = truncate_text(discord.utils.escape_markdown(str(fallback_name).strip()), 80)
        if clean_name:
            prefix = f"{clean_name} • "
    return f"{prefix}<@{user_id}> (`{user_id}`)"


def extract_snowflake_id(raw_value: str) -> Optional[int]:
    match = re.search(r"(\d{15,22})", str(raw_value or ""))
    if match:
        return int(match.group(1))
    return int(raw_value) if str(raw_value).isdigit() else None


def get_primary_guild() -> Optional[discord.Guild]:
    if not getattr(bot, "data_manager", None):
        return bot.guilds[0] if bot.guilds else None
    guild_id = bot.data_manager.config.get("guild_id", DEFAULT_GUILD_ID)
    if guild_id:
        guild = bot.get_guild(int(guild_id))
        if guild:
            return guild
    return bot.guilds[0] if bot.guilds else None


def get_context_guild(interaction: discord.Interaction) -> Optional[discord.Guild]:
    return interaction.guild or get_primary_guild()


async def send_modmail_panel_message(
    destination: Union[discord.abc.Messageable, discord.TextChannel, discord.User],
    guild: discord.Guild,
    *,
    intro: Optional[str] = None,
    in_dm: bool = False,
):
    is_dm_panel = in_dm or isinstance(destination, (discord.User, discord.Member, discord.DMChannel))
    embed = build_modmail_panel_embed(guild, in_dm=is_dm_panel)
    if intro:
        note_value = str(intro).strip()
        if note_value and not note_value.lstrip().startswith((">", "-", "*")):
            note_value = f"> {note_value}"
        if note_value:
            embed.add_field(name="Quick Note", value=note_value, inline=False)

    # Lazy import to avoid circular dependency: modmail.py imports from shared.py
    from .modmail import ModmailPanelView  # noqa: PLC0415

    img_data, _ = await fetch_image_bytes(MODMAIL_PANEL_BANNER_URL)
    if img_data:
        embed.set_image(url="attachment://banner.png")
        file = discord.File(io.BytesIO(img_data), filename="banner.png")
        return await destination.send(embed=embed, file=file, view=ModmailPanelView())

    embed.set_image(url=MODMAIL_PANEL_BANNER_URL)
    return await destination.send(embed=embed, view=ModmailPanelView())


async def maybe_send_dm_modmail_panel(user: discord.User, *, guild: Optional[discord.Guild] = None, force: bool = False, intro: Optional[str] = None) -> bool:
    if not get_feature_flag(bot.data_manager.config, "dm_modmail_prompt", True):
        return False

    guild = guild or get_primary_guild()
    if guild is None:
        return False

    cooldown_minutes = max(1, int(bot.data_manager.config.get("dm_modmail_panel_cooldown_minutes", 30) or 30))
    now_ts = time.time()
    last_sent = bot.dm_modmail_prompt_cooldowns.get(user.id, 0.0)
    if not force and last_sent and now_ts - last_sent < cooldown_minutes * 60:
        return False

    note = intro or "Need staff help? Open one private ticket below. Once it is open, keep replying in this DM."
    try:
        await send_modmail_panel_message(user, guild, intro=note, in_dm=True)
    except discord.Forbidden:
        return False
    except Exception as exc:
        logger.warning("Failed to send DM modmail panel to %s: %s", user.id, exc)
        return False

    bot.dm_modmail_prompt_cooldowns[user.id] = now_ts
    return True


async def send_automod_log(
    guild: discord.Guild,
    embed: discord.Embed,
    *,
    content: Optional[str] = None,
    preferred_channel_id: Optional[int] = None,
):
    candidate_ids = []
    for raw_channel_id in (
        preferred_channel_id,
        bot.data_manager.config.get("automod_log_channel_id"),
        *get_punishment_log_channel_ids(),
    ):
        if not raw_channel_id:
            continue
        channel_id = int(raw_channel_id)
        if channel_id not in candidate_ids:
            candidate_ids.append(channel_id)

    await _send_log_to_channels(
        guild,
        candidate_ids,
        embed,
        content=content,
        log_label="automod log",
    )


def build_role_landing_embed(member: discord.Member, *, is_booster: bool, limit: int) -> discord.Embed:
    embed = make_embed(
        "Custom Role",
        "> Server boosters can create and personalize a custom role as a boost perk.",
        kind="info",
        scope=SCOPE_ROLES,
        guild=member.guild,
        thumbnail=member.display_avatar.url,
    )
    embed.add_field(name="Booster Slot", value=f"1 of {limit}", inline=True)
    embed.add_field(name="Customizable", value="Name, color, icon, style", inline=True)
    embed.add_field(
        name="How It Works",
        value="> 1. Create your role below.\n> 2. Adjust name, color, icon, and style at any time.\n> 3. Return to this panel whenever you want to make changes.",
        inline=False,
    )
    return embed


def build_modmail_panel_embed(guild: discord.Guild, *, in_dm: bool = False) -> discord.Embed:
    description = (
        "> Need staff help? Open a ticket below — once it's open, continue replying here in DMs."
        if in_dm
        else "> Need staff help? Open a private ticket below — the bot will follow up with you in DMs."
    )
    embed = make_embed(
        "Contact Staff",
        description,
        kind="support",
        scope=SCOPE_SUPPORT,
        guild=guild,
    )
    embed.add_field(name="Report", value="User reports, message links, IDs, or evidence.", inline=True)
    embed.add_field(name="General Support", value="Server help, questions, or moderator assistance.", inline=True)
    embed.add_field(name="Bot Support", value="Bugs, broken commands, or automation issues.", inline=True)
    embed.add_field(name="Partnership", value="Partnership requests and server details.", inline=True)
    embed.add_field(
        name="Before You Open",
        value="> Include usernames, links, IDs, or screenshots when possible.\n> Pick the closest type so staff can route your ticket faster.",
        inline=False,
    )
    return embed


def build_setup_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    config = bot.data_manager.config
    general_log_channel_id = get_general_log_channel_id(config)
    configured_punishment_log_channel_id = config.get("punishment_log_channel_id")
    embed = make_embed(
        "Server Configuration",
        "> Use the panels below to configure roles, channels, and guild-wide settings.",
        kind="warning",
        scope=SCOPE_SYSTEM,
        guild=guild,
    )

    # --- Roles ---
    embed.add_field(name="Owner", value=f"<@&{config.get('role_owner', DEFAULT_ROLE_OWNER)}>", inline=True)
    embed.add_field(name="Admin", value=f"<@&{config.get('role_admin', DEFAULT_ROLE_ADMIN)}>", inline=True)
    embed.add_field(name="Moderator", value=f"<@&{config.get('role_mod', DEFAULT_ROLE_MOD)}>", inline=True)
    embed.add_field(name="Anchor Role", value=f"<@&{config.get('role_anchor', DEFAULT_ANCHOR_ROLE_ID)}>", inline=True)
    embed.add_field(name="Community Manager", value=f"<@&{config.get('role_community_manager', DEFAULT_ROLE_COMMUNITY_MANAGER)}>", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

    # --- Log Channels ---
    _automod_log = config.get("automod_log_channel_id")
    _automod_report = config.get("automod_report_channel_id")
    _modmail_inbox = config.get("modmail_inbox_channel")
    _modmail_action_log = config.get("modmail_action_log_channel")
    _modmail_panel = config.get("modmail_panel_channel")
    embed.add_field(
        name="Log Channels",
        value=join_lines([
            "General: " + (f"<#{general_log_channel_id}>" if general_log_channel_id else "Not set"),
            "Punishments: " + (f"<#{configured_punishment_log_channel_id}>" if configured_punishment_log_channel_id else "Falls back to general"),
            "AutoMod: " + (f"<#{_automod_log}>" if _automod_log else "Not set"),
            "Reports: " + (f"<#{_automod_report}>" if _automod_report else "Not set"),
        ]),
        inline=True,
    )
    embed.add_field(
        name="Modmail",
        value=join_lines([
            "Inbox: " + (f"<#{_modmail_inbox}>" if _modmail_inbox else "Not set"),
            "Actions: " + (f"<#{_modmail_action_log}>" if _modmail_action_log else "Not set"),
            "Panel: " + (f"<#{_modmail_panel}>" if _modmail_panel else "Not set"),
        ]),
        inline=True,
    )

    _appeal = config.get("appeal_channel_id")
    embed.add_field(
        name="Appeal Channel",
        value=join_lines([
            f"<#{_appeal}>" if _appeal else "Not set",
        ]),
        inline=True,
    )

    return embed


def build_config_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    config = bot.data_manager.config
    native_settings = get_native_automod_settings(config)
    embed = make_embed(
        "Bot Settings",
        "> Manage backups, imports, punishment scaling, and automation settings.",
        kind="info",
        scope=SCOPE_SYSTEM,
        guild=guild,
    )
    embed.add_field(name="Schema Version", value=f"v{config.get('schema_version', DEFAULT_SCHEMA_VERSION)}", inline=True)
    embed.add_field(name="Native AutoMod", value="On" if native_settings.get("enabled", True) else "Off", inline=True)
    embed.add_field(name="Escalation Steps", value=str(len(get_escalation_steps(config))), inline=True)
    return embed


def build_rules_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    rules = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
    steps = get_escalation_steps(bot.data_manager.config)
    embed = make_embed(
        "Punishment Rules",
        "> Preset rule baselines used by the punishment console. Base = first offence, Escalated = repeat offence.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(name="Total Rules", value=str(len(rules)), inline=True)
    embed.add_field(name="Escalation Tiers", value=str(len(steps)), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    for rule_name, data in list(rules.items())[:6]:
        embed.add_field(
            name=rule_name,
            value=f"Base: {format_duration(data['base'])}\nEsc: {format_duration(data['escalated'])}",
            inline=True,
        )
    return embed


def build_automod_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    settings = get_native_automod_settings(bot.data_manager.config)
    total_steps = 0
    configured_rules = 0
    for payload in settings.get("rule_overrides", {}).values():
        from .automod import get_native_automod_policy_steps
        step_count = len(get_native_automod_policy_steps(payload))
        total_steps += step_count
        if step_count:
            configured_rules += 1
    smart_enabled = bool(bot.data_manager.config.get("feature_flags", {}).get("smart_automod", False))
    embed = make_embed(
        "AutoMod Control Center",
        join_lines([
            "> Manage how the bot follows up on Discord AutoMod and runs its own smart filters.",
            "",
            "**Rules** — `Rule Punishments` set escalating actions per AutoMod rule · `Response Settings` control what the user sees.",
            "**Protection** — `Smart Filters`, `Immunity`, and `Log Channels`.",
        ]),
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(
        name="Rule Punishments",
        value=join_lines([
            f"Rules configured: {configured_rules}",
            f"Punishment steps: {total_steps}",
        ]),
        inline=True,
    )
    embed.add_field(
        name="Response Settings",
        value=join_lines([
            f"Channel message: {'On' if settings.get('enabled', True) else 'Off'}",
            f"User DM: {'On' if settings.get('warning_dm_enabled', True) else 'Off'}",
            f"Report button: {'On' if settings.get('report_button_enabled', True) else 'Off'}",
        ]),
        inline=True,
    )
    embed.add_field(
        name="Smart Filters",
        value=f"Status: {'Enabled' if smart_enabled else 'Disabled'}",
        inline=True,
    )
    embed.add_field(
        name="Log Channels",
        value=join_lines([
            f"Warn Logs: <#{bot.data_manager.config.get('automod_log_channel_id', 0)}>" if bot.data_manager.config.get('automod_log_channel_id') else "Warn Logs: Uses the native alert channel or punishment logs",
            f"Reports: <#{bot.data_manager.config.get('automod_report_channel_id', 0)}>" if bot.data_manager.config.get('automod_report_channel_id') else "Reports: Uses appeals or punishment logs",
        ]),
        inline=False,
    )
    embed.add_field(name="Exempt Users", value=str(len(settings.get("immunity_users", []))), inline=True)
    embed.add_field(name="Exempt Roles", value=str(len(settings.get("immunity_roles", []))), inline=True)
    embed.add_field(name="Exempt Channels", value=str(len(settings.get("immunity_channels", []))), inline=True)
    return embed


def build_escalation_matrix_embed(guild: discord.Guild) -> discord.Embed:
    embed = make_embed(
        "Punishment Scaling",
        "> Controls how punishments scale when a user reoffends. Each tier activates at a point threshold.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    for step in get_escalation_steps(bot.data_manager.config):
        mode_label = "Base duration" if step.mode == "base" else ("Scaled duration" if step.mode == "escalated" else "Ban")
        ban_note = " • Auto Ban" if step.force_ban else ""
        embed.add_field(
            name=step.label or f"{step.mode.title()} Tier",
            value=f"From **{step.minimum_points}** pts\n{mode_label} × {step.multiplier}{ban_note}",
            inline=True,
        )
    return embed


def build_canned_replies_embed(guild: discord.Guild) -> discord.Embed:
    replies = bot.data_manager.config.get("modmail_canned_replies", {})
    embed = make_embed(
        "Saved Replies",
        "> Quick reply templates staff can send in modmail.",
        kind="support",
        scope=SCOPE_SUPPORT,
        guild=guild,
    )
    for key, value in list(replies.items())[:10]:
        embed.add_field(name=key, value=truncate_text(value, 200), inline=False)
    if not replies:
        embed.add_field(name="Templates", value="No saved replies have been added yet.", inline=False)
    return embed


def build_setup_validation_embed(guild: discord.Guild, findings: List[Any]) -> discord.Embed:
    summary_counter = Counter(finding.level for finding in findings)
    kind = "success" if summary_counter.get("error", 0) == 0 and summary_counter.get("warning", 0) == 0 else ("warning" if summary_counter.get("error", 0) == 0 else "danger")
    embed = make_embed(
        "Setup Check",
        "> This checks whether your saved channels, roles, and bot permissions still look correct.",
        kind=kind,
        scope=SCOPE_SYSTEM,
        guild=guild,
    )
    embed.add_field(name="Errors", value=str(summary_counter.get("error", 0)), inline=True)
    embed.add_field(name="Warnings", value=str(summary_counter.get("warning", 0)), inline=True)
    embed.add_field(name="Success", value=str(summary_counter.get("success", 0)), inline=True)
    grouped = defaultdict(list)
    for finding in findings:
        grouped[finding.section].append(f"[{finding.level.upper()}] {finding.message}")
    for section, messages in grouped.items():
        embed.add_field(name=section, value=truncate_text("\n".join(messages), 1024), inline=False)
    return embed


def build_status_embed(guild: discord.Guild) -> discord.Embed:
    latency = round(bot.latency * 1000)
    uptime_seconds = int(time.time() - bot.start_time)
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"

    if latency < 100:
        latency_label = f"`{latency}ms` — Good"
    elif latency < 250:
        latency_label = f"`{latency}ms` — Fair"
    else:
        latency_label = f"`{latency}ms` — High"

    total_records = sum(len(records) for records in bot.data_manager.punishments.values())
    open_tickets = sum(1 for ticket in bot.data_manager.modmail.values() if ticket.get("status") == "open")
    embed = make_embed(
        "System Status",
        "> Operational health for runtime and staff-facing systems.",
        kind="info",
        scope=SCOPE_SYSTEM,
        guild=guild,
    )
    embed.add_field(name="Latency", value=latency_label, inline=True)
    embed.add_field(name="Uptime", value=f"`{uptime_str}`", inline=True)
    embed.add_field(name="Members", value=str(guild.member_count or 0), inline=True)
    embed.add_field(name="Open Tickets", value=str(open_tickets), inline=True)
    embed.add_field(name="Punishment Records", value=str(total_records), inline=True)
    embed.add_field(name="Cache Size", value=str(len(bot.data_manager.message_cache)), inline=True)
    return embed

async def handle_abuse(interaction: discord.Interaction, moderator: discord.Member):
    # Security Protocol: Strip Roles
    mod_roles = bot.data_manager.config.get("mod_roles", [])
    to_remove = []
    for rid in mod_roles:
        role = interaction.guild.get_role(rid)
        if role and role in moderator.roles:
            to_remove.append(role)
    
    if to_remove:
        try:
            await moderator.remove_roles(*to_remove, reason="Anti-Abuse: Rate limit exceeded")
        except Exception:
            pass
            
    embed = make_embed(
        "Security Alert: Abuse Detected",
        "> The anti-abuse rate limiter flagged a moderation action burst and removed elevated roles.",
        kind="danger",
        scope=SCOPE_SYSTEM,
        guild=interaction.guild,
        thumbnail=moderator.display_avatar.url,
    )
    embed.add_field(name="Actor", value=format_user_ref(moderator), inline=True)
    embed.add_field(name="System Action", value="Roles stripped due to rate-limit violation", inline=True)
    await send_log(interaction.guild, embed)
    await interaction.response.send_message("Action blocked. You have been flagged for abuse.", ephemeral=True)

async def punish_rogue_mod(guild: discord.Guild, member: discord.User, reason: str, embed: discord.Embed = None, restore_data: dict = None):
    # Fetch fresh member to ensure roles are up to date and we have a Member object
    target_member = guild.get_member(member.id)
    if not target_member:
        try:
            target_member = await guild.fetch_member(member.id)
        except Exception:
            target_member = None

    action_log = "No configured staff roles found on user."
    stripped_ids = []
    
    if target_member:
        # 1. Strip Mod Roles
        mod_roles_ids = bot.data_manager.config.get("mod_roles", [])
        to_remove = []
        for rid in mod_roles_ids:
            role = guild.get_role(rid)
            if role and role in target_member.roles:
                to_remove.append(role)
        
        if to_remove:
            try:
                await target_member.remove_roles(*to_remove, reason=f"ANTI-NUKE: {reason}")
                action_log = f"Stripped Staff Roles: {', '.join([r.name for r in to_remove])}"
                stripped_ids = [r.id for r in to_remove]
            except Exception as e:
                action_log = f"Failed to strip roles: {e}"
    else:
        action_log = "User left guild or not found."

    # 2. Log
    if embed is None:
        embed = make_embed(
            "Security Alert: Anti-Nuke Triggered",
            "> A protected action was automatically reverted and the actor was restricted.",
            kind="danger",
            scope=SCOPE_SYSTEM,
            guild=guild,
        )
        embed.add_field(name="Actor", value=f"<@{member.id}> (`{member.id}`)", inline=True)
        embed.add_field(name="Violation", value=truncate_text(reason, 1000), inline=False)

    embed.add_field(name="System Action", value=f"> {action_log}", inline=True)
    brand_embed(embed, guild=guild, scope=SCOPE_SYSTEM)
    
    view = None
    if restore_data:
        restore_data["stripped_roles"] = stripped_ids
        restore_data["actor_id"] = member.id
        # Lazy import to avoid circular dependency
        from .system import AntiNukeResolveView  # noqa: PLC0415
        view = AntiNukeResolveView(restore_data)
        
    # Dynamic pings
    r_admin = bot.data_manager.config.get("role_admin", DEFAULT_ROLE_ADMIN)
    r_owner = bot.data_manager.config.get("role_owner", DEFAULT_ROLE_OWNER)
    pings = f"<@&{r_admin}> <@&{r_owner}>"
    
    await send_log(guild, embed, content=pings, view=view)




def check_admin(interaction: discord.Interaction) -> bool:
    return has_permission_capability(interaction, "setup_panel")


def check_owner(interaction: discord.Interaction) -> bool:
    return has_permission_capability(interaction, "owner_panel")
