from dotenv import load_dotenv
load_dotenv()

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import copy
from discord.ext import tasks
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
import tempfile
from pathlib import Path
from types import SimpleNamespace
from modules.mbx_models import CaseNote
from modules.mbx_services import (
    DEFAULT_CANNED_REPLIES,
    DEFAULT_ESCALATION_MATRIX,
    DEFAULT_FEATURE_FLAGS,
    DEFAULT_NATIVE_AUTOMOD_SETTINGS,
    DEFAULT_SCHEMA_VERSION,
    DEFAULT_TICKET_PRIORITIES,
    export_case_payload,
    export_config_payload,
    get_feature_flag,
    get_escalation_steps,
    get_native_automod_settings,
    has_capability,
    import_config_payload,
    normalize_case_record,
    normalize_modmail_ticket,
    resolve_escalation_duration,
    resolve_native_automod_policy,
    run_schema_migrations,
    sanitize_evidence_links,
    sanitize_linked_cases,
    sanitize_tags,
    ticket_needs_sla_alert,
    validate_guild_configuration,
)

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("MGXBot")
# Suppress noisy rate limit warnings from discord.http
logging.getLogger("discord.http").setLevel(logging.ERROR)

# ----------------- CONFIG -----------------
DEFAULT_GUILD_ID = 1351136089259114516
DEFAULT_ANCHOR_ROLE_ID = 1433987521133674597  # custom roles go under this role
BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "database"
ROLES_FILE = DB_DIR / "roles.json"
CONFIG_FILE = DB_DIR / "config.json"
PUNISHMENTS_FILE = DB_DIR / "punishments.json"
MOD_STATS_FILE = DB_DIR / "mod_stats.json"
MESSAGE_CACHE_FILE = DB_DIR / "message_cache.json"
PINGS_FILE = DB_DIR / "pings.json"
LOCKDOWN_FILE = DB_DIR / "lockdown.json"
MODMAIL_FILE = DB_DIR / "modmail.json"
TOKEN_ENV_VARS = ("DISCORD_BOT_TOKEN", "MBX_BOT_TOKEN")
DEFAULT_MESSAGE_CACHE_LIMIT = 5000
DEFAULT_MESSAGE_CACHE_RETENTION_DAYS = 14
DEFAULT_MAX_UNREAD_PINGS = 100
BRAND_NAME = "Mysterious Bot X"
SCOPE_SYSTEM = "Control Center"
SCOPE_MODERATION = "Moderation Suite"
SCOPE_SUPPORT = "Support Center"
SCOPE_ROLES = "Custom Roles"
SCOPE_ANALYTICS = "Analytics"
THEME_ORANGE = discord.Color.from_rgb(255, 153, 0)
EMBED_PALETTE = {
    "neutral": THEME_ORANGE,
    "success": discord.Color.from_rgb(87, 242, 135),
    "warning": THEME_ORANGE,
    "danger": discord.Color.from_rgb(237, 66, 69),
    "info": THEME_ORANGE,
    "muted": THEME_ORANGE,
    "support": THEME_ORANGE,
    "analytics": THEME_ORANGE,
}
FEATURE_FLAG_LABELS = {
    "advanced_case_panel": "Case Panel",
    "advanced_modmail": "Advanced Modmail",
    "setup_validation": "Setup Check",
    "config_panel": "Bot Settings Panel",
    "role_cleanup": "Lost Booster Role Cleanup",
    "smart_automod": "Smart Auto-Moderation",
    "native_automod_bridge": "Native AutoMod Follow-Up",
    "automod_panel": "AutoMod Panel",
    "dm_modmail_prompt": "DM Modmail Prompt",
}

# --- Role Constants ---
DEFAULT_ROLE_OWNER = 1351544048934191185
DEFAULT_ROLE_ADMIN = 1351544086556835921
DEFAULT_ROLE_MOD = 1351544100482056254
DEFAULT_ROLE_COMMUNITY_MANAGER = 1453995056586424340
DEFAULT_ARCHIVE_CAT_ID = 1454629061556043890
DEFAULT_SPAM_ROLE_ID = 1352841838985482322
HOLO_PRIMARY = 11127295
HOLO_SECONDARY = 16759788
HOLO_TERTIARY = 16761760
MODMAIL_PANEL_BANNER_URL = "https://cdn.discordapp.com/attachments/1430583478713450506/1475440172790452466/New_Project_4_2.png?ex=699d7e3d&is=699c2cbd&hm=3ab07aa5ab3a612760ce8b4d8af6e6460a67df380fe28059468b9570429093e5&"

# Default rules if config is empty.
# Discord Timeout Limit is ~28 days (40320 minutes).
DEFAULT_RULES = {
    "Spamming": {"base": 0, "escalated": 60},             # Warn -> 1h
    "Insults": {"base": 0, "escalated": 120},             # Warn -> 2h
    "Harassment": {"base": 1440, "escalated": 10080},     # 24h -> 7d
    "NSFW": {"base": 10080, "escalated": 40320},          # 7d -> 28d
    "Scamming": {"base": 40320, "escalated": 40320},      # 28d -> 28d
    "Racism": {"base": 40320, "escalated": 40320},        # 28d -> 28d
    "Advertising": {"base": 0, "escalated": 1440},        # Warn -> 24h
    "Begging": {"base": 0, "escalated": 60},              # Warn -> 1h
    "Trolling": {"base": 60, "escalated": 1440},          # 1h -> 24h
    "Doxing": {"base": 40320, "escalated": 40320},        # 28d -> 28d
    "Hate Speech": {"base": 10080, "escalated": 40320},   # 7d -> 28d
    "Impersonation": {"base": 1440, "escalated": 10080},  # 24h -> 7d
    "Political": {"base": 0, "escalated": 60},            # Warn -> 1h
    "Raid": {"base": 40320, "escalated": 40320},          # 28d -> 28d
    "Exploiting": {"base": 10080, "escalated": 40320},    # 7d -> 28d
    "Inappropriate Lang": {"base": 0, "escalated": 60},   # Warn -> 1h
    "Ping Spam": {"base": 30, "escalated": 120},          # 30m -> 2h
    "Misinformation": {"base": 0, "escalated": 360},      # Warn -> 6h
    "Off-Topic": {"base": 0, "escalated": 30},            # Warn -> 30m
    "Argumentative": {"base": 0, "escalated": 60}         # Warn -> 1h
}
COOLDOWN_SECONDS = 60
# ------------------------------------------


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

    config_token = bootstrap_config.get("bot_token")
    if isinstance(config_token, str) and config_token.strip():
        logger.warning("Loading bot token from config.json. Prefer using an environment variable instead.")
        return config_token.strip()

    raise RuntimeError(
        "Discord bot token is not configured. Set DISCORD_BOT_TOKEN (recommended) or add bot_token to config.json."
    )


TOKEN = resolve_bot_token()

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
if hasattr(intents, 'auto_moderation_configuration'):
    intents.auto_moderation_configuration = True
if hasattr(intents, 'auto_moderation_execution'):
    intents.auto_moderation_execution = True

class MGXBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session: Optional[aiohttp.ClientSession] = None
        self.data_manager = None
        self.start_time = time.time()
        self.active_executions = {}
        self.dm_modmail_prompt_cooldowns: Dict[int, float] = {}
        self.native_automod_event_cache: Dict[Tuple[int, int, int, str, str], float] = {}

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.data_manager = DataManager(self)
        await self.data_manager.load_all()
        
        # Sync commands to the specific guild for instant updates during dev/testing
        # NOTE: Auto-sync disabled to prevent 429 Rate Limits on restart.
        # Use !sync command to update slash commands manually.
        # self.tree.copy_global_to(guild=discord.Object(id=GUILD_ID))
        # await self.tree.sync(guild=discord.Object(id=GUILD_ID))

        # Register Persistent Views
        self.add_view(ModmailPanelView())

        for legacy_command in (
            "case",
            "roleadmin",
        ):
            self.tree.remove_command(legacy_command)

        # Restore active modmail tickets
        for uid, data in self.data_manager.modmail.items():
            if data.get("status") == "open":
                log_id = data.get("log_id")
                if log_id:
                    self.add_view(ModmailControlView(uid), message_id=log_id)

        self.check_tempbans.start()
        self.background_save_task.start()
        self.status_task.start()
        self.modmail_sla_task.start()
        self.role_cleanup_task.start()

    async def close(self):
        self.check_tempbans.cancel()
        self.background_save_task.cancel()
        self.status_task.cancel()
        self.modmail_sla_task.cancel()
        self.role_cleanup_task.cancel()
        if self.data_manager:
            await self.data_manager.save_all(force=True)
        if self.session:
            await self.session.close()
        await super().close()

    @tasks.loop(minutes=1)
    async def check_tempbans(self):
        now = discord.utils.utcnow()
        changed = False
        
        if not self.data_manager: return
        
        for uid, records in self.data_manager.punishments.items():
            for rec in records:
                # Check for active tempbans
                if rec.get("type") == "ban" and rec.get("active", False):
                    mins = rec.get("duration_minutes", 0)
                    if mins > 0:
                        ts = iso_to_dt(rec.get("timestamp"))
                        if ts:
                            expiry = ts + timedelta(minutes=mins)
                            if now >= expiry:
                                # Expired - Unban
                                gid = self.data_manager.config.get("guild_id", DEFAULT_GUILD_ID)
                                guild = self.get_guild(gid)
                                if guild:
                                    try:
                                        await guild.unban(discord.Object(id=int(uid)), reason="Tempban Expired")
                                    except Exception:
                                        pass # User might already be unbanned
                                
                                rec["active"] = False
                                changed = True
        
        if changed:
            await self.data_manager.save_punishments()

    @tasks.loop(minutes=2)
    async def background_save_task(self):
        if self.data_manager:
            await self.data_manager.save_all()

    @tasks.loop(minutes=30)
    async def status_task(self):
        await self.change_presence(activity=discord.Game(name="DM for modmail"))

    @tasks.loop(minutes=10)
    async def modmail_sla_task(self):
        if not self.data_manager or not get_feature_flag(self.data_manager.config, "advanced_modmail", True):
            return

        guild = self.get_guild(self.data_manager.config.get("guild_id", DEFAULT_GUILD_ID))
        if not guild:
            return

        now = discord.utils.utcnow()
        sla_minutes = max(5, int(self.data_manager.config.get("modmail_sla_minutes", 60)))
        changed = False

        for user_id, ticket in self.data_manager.modmail.items():
            if not isinstance(ticket, dict):
                continue
            if not ticket_needs_sla_alert(ticket, now, sla_minutes):
                continue

            thread_id = ticket.get("thread_id")
            thread = guild.get_thread(thread_id) if thread_id else None
            if not thread and thread_id:
                try:
                    thread = await self.fetch_channel(thread_id)
                except Exception:
                    thread = None

            assigned = ticket.get("assigned_moderator")
            assigned_text = f"<@{assigned}>" if assigned else "Unassigned"
            sla_embed = make_embed(
                "Reply Reminder",
                f"> This ticket has not received a staff reply in over **{sla_minutes} minute{'s' if sla_minutes != 1 else ''}**.",
                kind="warning",
                scope=SCOPE_SUPPORT,
            )
            sla_embed.add_field(name="Assigned To", value=assigned_text, inline=True)
            sla_embed.add_field(name="SLA Threshold", value=f"{sla_minutes} min", inline=True)
            if thread:
                try:
                    await thread.send(embed=sla_embed)
                except Exception:
                    pass

            ticket["last_sla_alert_at"] = now_iso()
            changed = True

        if changed:
            await self.data_manager.save_modmail()

    @tasks.loop(hours=6)
    async def role_cleanup_task(self):
        if not self.data_manager or not get_feature_flag(self.data_manager.config, "role_cleanup", True):
            return

        guild = self.get_guild(self.data_manager.config.get("guild_id", DEFAULT_GUILD_ID))
        if not guild:
            return

        removed_any = False
        for user_id, record in list(self.data_manager.roles.items()):
            if not isinstance(record, dict):
                continue
            role_id = record.get("role_id")
            role = guild.get_role(role_id) if role_id else None
            member = guild.get_member(int(user_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None

            if member and get_custom_role_limit(member) > 0:
                continue

            if role:
                try:
                    await role.delete(reason="Custom role eligibility cleanup")
                except Exception:
                    pass

            self.data_manager.roles.pop(user_id, None)
            removed_any = True

            embed = make_embed(
                "Custom Role Cleanup",
                "> A custom role was removed because the owner no longer meets the eligibility requirements.",
                kind="warning",
                scope=SCOPE_ROLES,
                guild=guild,
            )
            embed.add_field(name="Target", value=f"<@{user_id}> (`{user_id}`)", inline=True)
            embed.add_field(name="Reason", value=format_reason_value("Lost booster or approved-role eligibility", limit=300), inline=False)
            await send_log(guild, embed)

        if removed_any:
            await self.data_manager.save_roles()

    @status_task.before_loop
    async def before_status_task(self):
        await self.wait_until_ready()

    @modmail_sla_task.before_loop
    async def before_modmail_sla_task(self):
        await self.wait_until_ready()

    @role_cleanup_task.before_loop
    async def before_role_cleanup_task(self):
        await self.wait_until_ready()

bot = MGXBot(command_prefix="!", intents=intents)
tree = bot.tree

# ----------------- Storage helpers -----------------
class DataManager:
    def __init__(self, bot):
        self.bot = bot
        self.config = {}
        self.roles = {}
        self.punishments = {}
        self.case_index: Dict[int, Tuple[str, dict]] = {}
        self.mod_stats = {}
        self.message_cache = deque(maxlen=DEFAULT_MESSAGE_CACHE_LIMIT)
        self.message_cache_index: Dict[int, dict] = {}
        self.pings = {}
        self.modmail = {}
        self.modmail_threads: Dict[int, str] = {}
        self.lockdown = {}
        self.message_cache_retention_days = DEFAULT_MESSAGE_CACHE_RETENTION_DAYS
        
        self._dirty_config = False
        self._dirty_roles = False
        self._dirty_punishments = False
        self._dirty_stats = False
        self._dirty_message_cache = False
        self._dirty_pings = False
        self._dirty_modmail = False
        self._dirty_lockdown = False
        self._save_lock = asyncio.Lock()

    def _load_json(self, path, default):
        return read_json_file(Path(path), default)

    def _save_json_sync(self, path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None

        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
            ) as temp_file:
                json.dump(data, temp_file, indent=2, ensure_ascii=False)
                temp_file.write("\n")
                temp_name = temp_file.name
            os.replace(temp_name, path)
        finally:
            if temp_name and os.path.exists(temp_name):
                try:
                    os.remove(temp_name)
                except OSError:
                    pass

    async def _save_json(self, path, data):
        await asyncio.to_thread(self._save_json_sync, path, data)

    def _normalize_positive_int(self, value: Any, default: int, *, minimum: int = 1, maximum: Optional[int] = None) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = default

        if maximum is not None:
            normalized = min(normalized, maximum)
        return max(minimum, normalized)

    def _configure_cache_limits(self):
        cache_limit = self._normalize_positive_int(
            self.config.get("message_cache_limit", DEFAULT_MESSAGE_CACHE_LIMIT),
            DEFAULT_MESSAGE_CACHE_LIMIT,
            minimum=100,
            maximum=50000,
        )

        if self.message_cache.maxlen != cache_limit:
            self.message_cache = deque(list(self.message_cache)[-cache_limit:], maxlen=cache_limit)

        self.message_cache_retention_days = self._normalize_positive_int(
            self.config.get("message_cache_retention_days", DEFAULT_MESSAGE_CACHE_RETENTION_DAYS),
            DEFAULT_MESSAGE_CACHE_RETENTION_DAYS,
            minimum=1,
            maximum=90,
        )
        self._rebuild_message_cache_index()

    def _parse_optional_int(self, value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _normalize_message_cache_record(self, record: Any) -> Optional[dict]:
        if not isinstance(record, dict):
            return None

        normalized = dict(record)
        message_id = self._parse_optional_int(normalized.get("id"))
        if message_id is None:
            return None

        normalized["id"] = message_id
        author_id = self._parse_optional_int(normalized.get("author_id"))
        if author_id is not None:
            normalized["author_id"] = author_id

        channel_id = self._parse_optional_int(normalized.get("channel_id"))
        if channel_id is not None:
            normalized["channel_id"] = channel_id

        created_at = normalized.get("created_at")
        if not isinstance(created_at, datetime):
            normalized["created_at"] = parse_iso_datetime(created_at) or discord.utils.utcnow()

        attachments = normalized.get("attachments", [])
        normalized["attachments"] = attachments if isinstance(attachments, list) else []
        stickers = normalized.get("stickers", [])
        normalized["stickers"] = stickers if isinstance(stickers, list) else []
        normalized["deleted"] = bool(normalized.get("deleted", False))
        normalized["edited"] = bool(normalized.get("edited", False))
        return normalized

    def _rebuild_message_cache_index(self):
        self.message_cache_index = {}
        for record in self.message_cache:
            record_id = self._parse_optional_int(record.get("id"))
            if record_id is not None:
                record["id"] = record_id
                self.message_cache_index[record_id] = record

    def _rebuild_modmail_index(self):
        self.modmail_threads = {}
        for user_id, ticket in self.modmail.items():
            thread_id = self._parse_optional_int(ticket.get("thread_id"))
            if thread_id is not None:
                self.modmail_threads[thread_id] = user_id

    def _rebuild_case_index(self):
        self.case_index = {}
        for user_id, records in self.punishments.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                self._index_case_record(user_id, record)

    def _index_case_record(self, user_id: str, record: dict):
        case_id = record.get("case_id")
        if isinstance(case_id, int) and case_id > 0:
            self.case_index[case_id] = (user_id, record)

    def _prune_message_cache(self):
        cutoff = discord.utils.utcnow() - timedelta(days=self.message_cache_retention_days)
        pruned = False

        while self.message_cache:
            oldest = self.message_cache[0]
            created_at = oldest.get("created_at")
            if not isinstance(created_at, datetime):
                created_at = parse_iso_datetime(created_at) or discord.utils.utcnow()
                oldest["created_at"] = created_at

            if created_at >= cutoff:
                break

            removed = self.message_cache.popleft()
            self.message_cache_index.pop(removed.get("id"), None)
            pruned = True

        if pruned:
            self._dirty_message_cache = True

    def _append_message_record(self, record: dict, *, mark_dirty: bool = True):
        normalized = self._normalize_message_cache_record(record)
        if normalized is None:
            if mark_dirty:
                self._dirty_message_cache = True
            return

        if len(self.message_cache) >= self.message_cache.maxlen:
            removed = self.message_cache.popleft()
            self.message_cache_index.pop(removed.get("id"), None)

        self.message_cache.append(normalized)
        record_id = normalized["id"]
        if record_id is not None:
            self.message_cache_index[record_id] = normalized

        self._prune_message_cache()
        if mark_dirty:
            self._dirty_message_cache = True

    def _serialize_message_cache(self) -> List[dict]:
        serializable = []
        for msg in list(self.message_cache):
            msg_copy = msg.copy()
            if isinstance(msg_copy.get("created_at"), datetime):
                msg_copy["created_at"] = msg_copy["created_at"].isoformat()
            serializable.append(msg_copy)
        return serializable

    def _ensure_dict(self, value: Any, path: Path) -> dict:
        if isinstance(value, dict):
            return value
        logger.warning("Expected %s to contain a JSON object. Resetting to defaults.", path.name)
        return {}

    def _ensure_list(self, value: Any, path: Path) -> list:
        if isinstance(value, list):
            return value
        logger.warning("Expected %s to contain a JSON array. Resetting to defaults.", path.name)
        return []

    async def load_all(self):
        self.config = self._ensure_dict(self._load_json(CONFIG_FILE, {}), CONFIG_FILE)
        had_general_log_channel = "general_log_channel_id" in self.config
        legacy_log_channel_id = self.config.get("log_channel_id")
        # Ensure defaults
        defaults = {
            "min_boosts_for_role": 0, "whitelist": {}, "punishment_rules": DEFAULT_RULES,
            "mod_roles": [], "stats": {"total_issued": 0, "cases_cleared": 0},
            "locked_channels": {}, "archived_channels": {}, 
            "cr_whitelist_users": {}, "cr_whitelist_roles": {}, "cr_blacklist_users": [], "cr_blacklist_roles": [],
            "security": {"max_actions_per_min": 10},
            "smart_automod": {
                "duplicate_window_seconds": 20,
                "duplicate_threshold": 4,
                "max_caps_ratio": 0.75,
                "caps_min_length": 12,
                "blocked_patterns": [],
                "exempt_channels": [],
                "exempt_roles": [],
            },
            "native_automod": DEFAULT_NATIVE_AUTOMOD_SETTINGS,
            "immunity_list": [], "debug": {},
            "token_env_var": "DISCORD_BOT_TOKEN",
            "case_counter": 0,
            "schema_version": DEFAULT_SCHEMA_VERSION,
            "message_cache_limit": DEFAULT_MESSAGE_CACHE_LIMIT,
            "message_cache_retention_days": DEFAULT_MESSAGE_CACHE_RETENTION_DAYS,
            "max_unread_pings_per_user": DEFAULT_MAX_UNREAD_PINGS,
            "feature_flags": {},
            "modmail_canned_replies": DEFAULT_CANNED_REPLIES,
            "modmail_sla_minutes": 60,
            "dm_modmail_panel_cooldown_minutes": 30,
            "escalation_matrix": [],
            # Customizable IDs
            "guild_id": DEFAULT_GUILD_ID,
            "general_log_channel_id": 0,
            "punishment_log_channel_id": 0,
            "automod_log_channel_id": 0,
            "automod_report_channel_id": 0,
            "role_owner": DEFAULT_ROLE_OWNER,
            "role_admin": DEFAULT_ROLE_ADMIN,
            "role_mod": DEFAULT_ROLE_MOD,
            "role_community_manager": DEFAULT_ROLE_COMMUNITY_MANAGER,
            "role_anchor": DEFAULT_ANCHOR_ROLE_ID,
            "category_archive": DEFAULT_ARCHIVE_CAT_ID,
            "role_mention_spam_target": DEFAULT_SPAM_ROLE_ID
        }
        for k, v in defaults.items():
            if k not in self.config:
                self.config[k] = copy.deepcopy(v)
                self._dirty_config = True

        if not had_general_log_channel and legacy_log_channel_id:
            self.config["general_log_channel_id"] = legacy_log_channel_id
            self._dirty_config = True

        self._configure_cache_limits()
        self.roles = self._ensure_dict(self._load_json(ROLES_FILE, {}), ROLES_FILE)
        self.punishments = self._ensure_dict(self._load_json(PUNISHMENTS_FILE, {}), PUNISHMENTS_FILE)
        self._normalize_punishments()
        self.mod_stats = self._ensure_dict(self._load_json(MOD_STATS_FILE, {}), MOD_STATS_FILE)
        self.pings = self._ensure_dict(self._load_json(PINGS_FILE, {}), PINGS_FILE)
        self.modmail = self._ensure_dict(self._load_json(MODMAIL_FILE, {}), MODMAIL_FILE)
        migrated, migration_notes = run_schema_migrations(self.config, self.punishments, self.modmail)
        if migrated:
            self._dirty_config = True
            self._dirty_punishments = True
            self._dirty_modmail = True
            for note in migration_notes:
                logger.info("Migration: %s", note)
        self.lockdown = self._ensure_dict(self._load_json(LOCKDOWN_FILE, {}), LOCKDOWN_FILE)
        self._rebuild_case_index()
        self._rebuild_modmail_index()

        self.message_cache.clear()
        self.message_cache_index.clear()
        raw_cache = self._ensure_list(self._load_json(MESSAGE_CACHE_FILE, []), MESSAGE_CACHE_FILE)
        for msg in raw_cache:
            normalized = self._normalize_message_cache_record(msg)
            if normalized is None:
                self._dirty_message_cache = True
                continue
            self._append_message_record(normalized, mark_dirty=False)
        self._prune_message_cache()

    async def save_all(self, force=False):
        async with self._save_lock:
            if self._dirty_config or force:
                await self._save_json(CONFIG_FILE, self.config)
                self._dirty_config = False
            if self._dirty_roles or force:
                await self._save_json(ROLES_FILE, self.roles)
                self._dirty_roles = False
            if self._dirty_punishments or force:
                self._rebuild_case_index()
                await self._save_json(PUNISHMENTS_FILE, self.punishments)
                self._dirty_punishments = False
            if self._dirty_stats or force:
                await self._save_json(MOD_STATS_FILE, self.mod_stats)
                self._dirty_stats = False
            if self._dirty_message_cache or force:
                self._prune_message_cache()
                await self._save_json(MESSAGE_CACHE_FILE, self._serialize_message_cache())
                self._dirty_message_cache = False
            if self._dirty_pings or force:
                await self._save_json(PINGS_FILE, self.pings)
                self._dirty_pings = False
            if self._dirty_modmail or force:
                self._rebuild_modmail_index()
                await self._save_json(MODMAIL_FILE, self.modmail)
                self._dirty_modmail = False
            if self._dirty_lockdown or force:
                await self._save_json(LOCKDOWN_FILE, self.lockdown)
                self._dirty_lockdown = False

    async def save_message_cache(self):
        self._dirty_message_cache = True
        await self.save_all()

    def mark_config_dirty(self):
        self._dirty_config = True

    async def save_config(self):
        self.mark_config_dirty()
        await self.save_all()

    async def save_roles(self):
        self._dirty_roles = True
        await self.save_all()

    async def save_punishments(self):
        self._dirty_punishments = True
        await self.save_all()

    async def save_mod_stats(self):
        self._dirty_stats = True
        await self.save_all()

    async def save_lockdown(self):
        self._dirty_lockdown = True
        await self.save_all()

    async def add_punishment(self, uid, record, *, persist: bool = True):
        if uid not in self.punishments:
            self.punishments[uid] = []
        prepared = self.prepare_punishment_record(record)
        self.punishments[uid].append(prepared)
        self._index_case_record(uid, prepared)
        self._dirty_punishments = True
        if persist:
            await self.save_all()
        return prepared

    async def save_modmail(self):
        self._dirty_modmail = True
        await self.save_all()

    def cache_message(self, record: dict):
        self._append_message_record(record)

    def get_cached_message(self, message_id: int) -> Optional[dict]:
        return self.message_cache_index.get(message_id)

    def mark_message_deleted(self, message_id: int) -> bool:
        record = self.get_cached_message(message_id)
        if not record:
            return False
        record["deleted"] = True
        self._dirty_message_cache = True
        return True

    def update_cached_message(self, message_id: int, **changes) -> bool:
        record = self.get_cached_message(message_id)
        if not record:
            return False
        record.update(changes)
        self._dirty_message_cache = True
        return True

    def get_modmail_user_id(self, thread_id: int) -> Optional[str]:
        return self.modmail_threads.get(thread_id)

    def get_case(self, case_id: int) -> Tuple[Optional[str], Optional[dict]]:
        normalized_case_id = self._parse_optional_int(case_id)
        if normalized_case_id is None:
            return None, None
        entry = self.case_index.get(normalized_case_id)
        if entry is not None:
            user_id, record = entry
            if record in self.punishments.get(user_id, []):
                return entry
            self.case_index.pop(normalized_case_id, None)

        self._rebuild_case_index()
        return self.case_index.get(normalized_case_id, (None, None))

    def get_user_cases(self, user_id: int) -> List[dict]:
        records = self.punishments.get(str(user_id), [])
        return sorted(
            [record for record in records if isinstance(record, dict)],
            key=lambda record: record.get("case_id", 0),
            reverse=True,
        )

    def allocate_case_id(self) -> int:
        current = self._normalize_positive_int(self.config.get("case_counter", 0), 0, minimum=0)
        next_case_id = current + 1
        self.config["case_counter"] = next_case_id
        self._dirty_config = True
        return next_case_id

    def prepare_punishment_record(self, record: dict) -> dict:
        prepared = dict(record)
        case_id = prepared.get("case_id")
        if not isinstance(case_id, int) or case_id <= 0:
            prepared["case_id"] = self.allocate_case_id()

        if "timestamp" not in prepared:
            prepared["timestamp"] = now_iso()

        if "active" not in prepared:
            prepared["active"] = prepared.get("type") == "ban"

        normalize_case_record(prepared)

        return prepared

    def _normalize_punishments(self):
        if not isinstance(self.punishments, dict):
            self.punishments = {}
            self._dirty_punishments = True
            return

        highest_case_id = self._normalize_positive_int(self.config.get("case_counter", 0), 0, minimum=0)
        changed = False
        now = discord.utils.utcnow()

        for uid, records in list(self.punishments.items()):
            if not isinstance(records, list):
                self.punishments[uid] = []
                changed = True
                continue

            normalized_records = []
            for record in records:
                if not isinstance(record, dict):
                    changed = True
                    continue

                case_id = record.get("case_id")
                if isinstance(case_id, int) and case_id > 0:
                    highest_case_id = max(highest_case_id, case_id)
                else:
                    highest_case_id += 1
                    record["case_id"] = highest_case_id
                    changed = True

                record_type = record.get("type")
                if record_type == "ban":
                    duration = record.get("duration_minutes", 0)
                    if duration == -1:
                        active = True
                    elif duration > 0:
                        issued_at = iso_to_dt(record.get("timestamp"))
                        active = bool(issued_at and issued_at + timedelta(minutes=duration) > now)
                    else:
                        active = False
                    if record.get("active") != active:
                        record["active"] = active
                        changed = True

                if normalize_case_record(record):
                    changed = True

                normalized_records.append(record)

            self.punishments[uid] = normalized_records

        if self.config.get("case_counter") != highest_case_id:
            self.config["case_counter"] = highest_case_id
            self._dirty_config = True

        self._rebuild_case_index()
        if changed:
            self._dirty_punishments = True

def now_iso():
    return discord.utils.utcnow().isoformat()

def iso_to_dt(s: str) -> Optional[datetime]:
    return parse_iso_datetime(s)

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
class AntiAbuseSystem:
    def __init__(self):
        self._tracker = defaultdict(lambda: deque(maxlen=15))
        self.cooldowns: Dict[str, float] = {}
        self.mention_spam_tracker = defaultdict(lambda: deque(maxlen=10))
        self.smart_automod_tracker = defaultdict(lambda: deque(maxlen=8))
        
    def check_rate_limit(self, user_id: int) -> bool:
        now = time.time()
        limit = bot.data_manager.config["security"].get("max_actions_per_min", 10)
        # Clean old timestamps
        while self._tracker[user_id] and now - self._tracker[user_id][0] > 60:
            self._tracker[user_id].popleft()
            
        self._tracker[user_id].append(now)
        return len(self._tracker[user_id]) > limit

abuse_system = AntiAbuseSystem()

def parse_duration_str(s: str) -> int:
    """
    Parses strings like '1d 2h', '30m', 'ban' into minutes.
    Returns -1 for Ban, 0 for Warn.
    """
    if not s: return 0
    s = s.lower().strip()
    if s in ["ban", "perm", "permanent", "infinity"]: return -1
    if s in ["warn", "warning", "0"]: return 0
    
    # Explicitly handle "-1" string as Ban
    try:
        if int(s) == -1: return -1
    except ValueError:
        pass
    
    total = 0
    matches = re.findall(r'(\d+)\s*([wdhm]?)', s)
    for amount, unit in matches:
        val = int(amount)
        if unit == 'w': total += val * 10080
        elif unit == 'd': total += val * 1440
        elif unit == 'h': total += val * 60
        else: total += val # default to minutes
    return total if total > 0 else 0

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

async def fetch_image_bytes(url: str, timeout: int = 10) -> Optional[bytes]:
    if not bot.session: return None
    try:
        async with bot.session.get(url, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        return None
    return None

def format_duration(minutes: int) -> str:
    if minutes == -1:
        return "Ban"
    if minutes == 0:
        return "Warning"
    if minutes < 60:
        return f"{minutes} mins"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"

def create_progress_bar(percent: float, length: int = 10) -> str:
    percent = max(0.0, min(1.0, percent))
    filled = int(length * percent)
    return "█" * filled + "░" * (length - filled)

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


def truncate_text(value: Optional[str], limit: int) -> str:
    if not value:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


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

    img_data = await fetch_image_bytes(MODMAIL_PANEL_BANNER_URL)
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
            "`/mod case` — Open a case panel for notes, status, evidence, and assignment.",
            "`/mod history` — Browse a user’s disciplinary record case-by-case.",
            "`/mod active` — View all active bans and timeouts.",
            "`/mod undopunish` — Reverse a punishment with a reason and case selector.",
        ]),
        inline=False,
    )
    embed.add_field(
        name="Actions",
        value="\n".join([
            "`/mod punish` — Open the sanction console with smart escalation.",
            "`/mod publicpunish` — Punish and post the result publicly in the channel.",
            "`/mod purge` — Bulk-delete messages with user or keyword filtering.",
        ]),
        inline=False,
    )
    embed.add_field(
        name="Channel Controls",
        value="\n".join([
            "`/mod lock` — Restrict messaging in the current channel.",
            "`/mod unlock` — Restore messaging in the current channel.",
        ]),
        inline=False,
    )
    return embed


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


MODMAIL_PANEL_CATEGORIES = [
    ("Report", "User reports, message links, IDs, or evidence."),
    ("General Support", "Server help, questions, or moderator assistance."),
    ("Bot Support", "Bugs, broken commands, or automation issues."),
    ("Partnership", "Partnership requests and server details."),
]


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

    # --- Support Channels ---
    _modmail_inbox = config.get("modmail_inbox_channel")
    _modmail_panel = config.get("modmail_panel_channel")
    _appeal = config.get("appeal_channel_id")
    embed.add_field(
        name="Support Channels",
        value=join_lines([
            "Modmail Inbox: " + (f"<#{_modmail_inbox}>" if _modmail_inbox else "Not set"),
            "Modmail Panel: " + (f"<#{_modmail_panel}>" if _modmail_panel else "Not set"),
            "Appeals: " + (f"<#{_appeal}>" if _appeal else "Not set"),
        ]),
        inline=True,
    )

    return embed


def build_modmail_settings_embed(guild: discord.Guild) -> discord.Embed:
    config = bot.data_manager.config
    discussion_threads = config.get("modmail_discussion_threads", True)
    dm_prompt = get_feature_flag(config, "dm_modmail_prompt", True)
    sla = config.get("modmail_sla_minutes", 60)
    cooldown = config.get("dm_modmail_panel_cooldown_minutes", 30)
    open_count = sum(1 for t in bot.data_manager.modmail.values() if t.get("status") == "open")
    embed = make_embed(
        "Modmail Settings",
        "> Configure how the ticket inbox behaves for staff and users.",
        kind="support",
        scope=SCOPE_SUPPORT,
        guild=guild,
    )
    embed.add_field(name="Discussion Threads", value="On" if discussion_threads else "Off", inline=True)
    embed.add_field(name="DM Prompt", value="On" if dm_prompt else "Off", inline=True)
    embed.add_field(name="SLA Reminder", value=f"{sla} min", inline=True)
    embed.add_field(name="DM Panel Cooldown", value=f"{cooldown} min", inline=True)
    embed.add_field(name="Open Tickets", value=str(open_count), inline=True)
    return embed


def build_config_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    config = bot.data_manager.config
    flags = config.get("feature_flags", {})
    enabled_count = sum(1 for value in flags.values() if value)
    native_settings = get_native_automod_settings(config)
    embed = make_embed(
        "Bot Settings",
        "> Manage backups, feature toggles, punishment scaling, and quick replies.",
        kind="info",
        scope=SCOPE_SYSTEM,
        guild=guild,
    )
    embed.add_field(name="Features Active", value=f"{enabled_count} / {len(flags)}", inline=True)
    embed.add_field(name="Schema Version", value=f"v{config.get('schema_version', DEFAULT_SCHEMA_VERSION)}", inline=True)
    embed.add_field(name="SLA Reminder", value=f"{config.get('modmail_sla_minutes', 60)} min", inline=True)
    embed.add_field(name="Native AutoMod", value="On" if native_settings.get("enabled", True) else "Off", inline=True)
    embed.add_field(name="Escalation Steps", value=str(len(get_escalation_steps(config))), inline=True)
    canned = config.get("modmail_canned_replies", {})
    embed.add_field(name="Saved Replies", value=str(len(canned)), inline=True)
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
        step_count = len(get_native_automod_policy_steps(payload))
        total_steps += step_count
        if step_count:
            configured_rules += 1
    embed = make_embed(
        "AutoMod Setup",
        "> Configure the bot's follow-up after Discord AutoMod triggers.",
        kind="warning",
        scope=SCOPE_MODERATION,
        guild=guild,
    )
    embed.add_field(
        name="Bot Response",
        value=join_lines([
            f"Status: {'On' if settings.get('enabled', True) else 'Off'}",
            f"User DMs: {'On' if settings.get('warning_dm_enabled', True) else 'Off'}",
            f"Report Button: {'On' if settings.get('report_button_enabled', True) else 'Off'}",
        ]),
        inline=True,
    )
    embed.add_field(
        name="Rules",
        value=join_lines([
            f"Rules Configured: {configured_rules}",
            f"Punishment Steps: {total_steps}",
        ]),
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


def build_feature_flags_embed(guild: discord.Guild) -> discord.Embed:
    flags = bot.data_manager.config.get("feature_flags", {})
    enabled_count = sum(1 for v in flags.values() if v)
    embed = make_embed(
        "Feature Toggles",
        f"> **{enabled_count}/{len(flags)}** systems are currently active. Use the toggles below to enable or disable features.",
        kind="info",
        scope=SCOPE_SYSTEM,
        guild=guild,
    )
    for key, value in sorted(flags.items()):
        status = "✅ On" if value else "⬜ Off"
        embed.add_field(name=get_feature_flag_name(key), value=status, inline=True)
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
        view = AntiNukeResolveView(restore_data)
        
    # Dynamic pings
    r_admin = bot.data_manager.config.get("role_admin", DEFAULT_ROLE_ADMIN)
    r_owner = bot.data_manager.config.get("role_owner", DEFAULT_ROLE_OWNER)
    pings = f"<@&{r_admin}> <@&{r_owner}>"
    
    await send_log(guild, embed, content=pings, view=view)


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

    # Anti-Abuse: Hierarchy Check
    if member_target and member_target.id != guild.owner_id and member_target != moderator:
        if member_target.top_role >= moderator.top_role:
            await interaction.response.send_message("**Anti-Abuse:** You cannot punish a user with equal or higher role hierarchy.", ephemeral=True)
            return

    # Anti-Abuse: Rate Limit
    if abuse_system.check_rate_limit(moderator.id):
        await handle_abuse(interaction, moderator)
        return

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        if is_kick:
            if not member_target:
                await interaction.followup.send("User is not in the server, cannot kick.", ephemeral=True)
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
                await interaction.followup.send("User is not in the server, cannot timeout.", ephemeral=True)
                return
            duration = get_valid_duration(minutes)
            await member_target.timeout(duration, reason=f"{reason} (By {moderator})")
    except discord.Forbidden:
        await interaction.followup.send("I cannot punish this user (Permission Error).", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)
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
def build_role_info_embed(member: discord.Member, rec: dict, role_obj: Optional[discord.Role], include_tips=False) -> discord.Embed:
    color_hex = rec.get("color", "#000000")
    color = discord.Color(int(color_hex.lstrip("#"), 16)) if hex_valid(color_hex) else EMBED_PALETTE["muted"]
    embed = make_embed(
        "Manage Your Custom Role",
        "> Review and update your saved custom role configuration.",
        kind="info" if color.value == 0 else "neutral",
        scope=SCOPE_ROLES,
        guild=member.guild,
    )
    embed.color = EMBED_PALETTE["muted"] if color.value == 0 else color
    if role_obj:
        embed.add_field(name="Role", value=f"{role_obj.mention}", inline=False)
        embed.add_field(name="Name", value=role_obj.name, inline=True)
        embed.add_field(name="Members", value=str(len(role_obj.members)), inline=True)
        if rec.get("secondary_color"):
            embed.add_field(name="Secondary (Gradient)", value=f"`{rec.get('secondary_color')}`", inline=True)
        if rec.get("tertiary_color"):
            embed.add_field(name="Tertiary (Holograph)", value=f"`{rec.get('tertiary_color')}`", inline=True)
    else:
        embed.add_field(name="Role", value=f"<@&{rec.get('role_id')}> (missing)", inline=False)
        embed.add_field(name="Name", value=rec.get("name", "Unknown"), inline=True)

    embed.add_field(name="Color", value=f"`{rec.get('color','Unknown')}`", inline=True)
    
    created_at = rec.get("created_at")
    if created_at:
        dt = iso_to_dt(created_at)
        if dt:
            embed.add_field(name="Created", value=discord.utils.format_dt(dt, style="f"), inline=True)
            delta = discord.utils.utcnow() - dt
            days = delta.days
            hours = delta.seconds // 3600
            embed.add_field(name="Age", value=f"{days}d {hours}h", inline=True)
        else:
            embed.add_field(name="Created", value=created_at, inline=True)

    icon_url = rec.get("icon")
    if icon_url and icon_url.startswith(("http://", "https://")):
        embed.set_thumbnail(url=icon_url)
    else:
        embed.set_thumbnail(url=member.display_avatar.url)

    if include_tips:
        embed.add_field(
            name="Tips",
            value=join_lines([
                "Use the action menu below to update the name, colors, icon, and style.",
                "If the icon URL fails, use the upload flow instead.",
            ]),
            inline=False,
        )

    return embed

def build_punish_embed(user: discord.Member) -> discord.Embed:
    uid = str(user.id)
    history = bot.data_manager.punishments.get(uid, [])
    active_records = get_active_records_for_user(user.id)
    risk_score, risk_label = calculate_member_risk(history)
    embed = make_embed(
        "Moderation Console",
        "> Select a violation category below, then review history if needed before acting.",
        kind="muted",
        scope=SCOPE_MODERATION,
        guild=user.guild if isinstance(user, discord.Member) else None,
        thumbnail=user.display_avatar.url,
    )
    embed.add_field(name="Target", value=format_user_ref(user), inline=True)
    embed.add_field(name="Total Cases", value=str(len(history)), inline=True)
    embed.add_field(name="Active Cases", value=str(len(active_records)), inline=True)
    embed.add_field(name="Risk", value=f"{risk_label} ({risk_score})", inline=True)
    if isinstance(user, discord.Member) and user.joined_at:
        embed.add_field(name="Joined Server", value=discord.utils.format_dt(user.joined_at, "f"), inline=True)
    embed.add_field(name="Account Created", value=discord.utils.format_dt(user.created_at, "f"), inline=True)
    return embed

# ----------------- Modals -----------------
class CreateRoleModal(discord.ui.Modal, title="Create your custom role"):
    role_name = discord.ui.TextInput(label="Role name", max_length=100)
    hex_color = discord.ui.TextInput(label="Hex color (Optional)", placeholder="#FF66CC", max_length=7, required=False)
    icon_url = discord.ui.TextInput(label="Icon URL (optional)", required=False, placeholder="https://...")

    def __init__(self, member: discord.Member):
        super().__init__()
        self._member = member

    async def on_submit(self, interaction: discord.Interaction):
        member = self._member
        guild = interaction.guild

        await interaction.response.defer(ephemeral=True)

        allowed = get_custom_role_limit(member)
        if allowed <= 0:
            await interaction.followup.send("You are not authorized to create a custom role.", ephemeral=True)
            return

        current = 1 if str(member.id) in bot.data_manager.roles else 0
        if current >= allowed:
            await interaction.followup.send(f"You are allowed {allowed} role(s) and already have {current}.", ephemeral=True)
            return

        name = self.role_name.value.strip()[:100]
        color_text = self.hex_color.value.strip() if self.hex_color.value else None
        
        if color_text:
            if not hex_valid(color_text):
                await interaction.followup.send("Invalid hex color (use #RRGGBB).", ephemeral=True)
                return
        else:
            color_text = "#000000" # Default

        try:
            color = discord.Color(int(color_text.lstrip("#"), 16))
        except Exception:
            color = discord.Color.default()

        try:
            new_role = await guild.create_role(name=name, color=color, mentionable=True, reason=f"Custom role created by {member}")
        except discord.Forbidden:
            await interaction.followup.send("Bot lacks permissions or role hierarchy prevents creation.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"Failed to create role: {e}", ephemeral=True)
            return

        anchor_id = bot.data_manager.config.get("role_anchor", DEFAULT_ANCHOR_ROLE_ID)
        anchor = guild.get_role(anchor_id)
        if not anchor:
            try: anchor = await guild.fetch_role(anchor_id)
            except Exception: pass
            
        if anchor:
            try:
                target_pos = max(anchor.position - 1, 1)
                await new_role.edit(position=target_pos, reason="Positioning under anchor")
            except Exception:
                pass

        icon_val = self.icon_url.value.strip() if self.icon_url.value else None
        if icon_val and not icon_val.startswith(("http://", "https://")):
            icon_val = None
            
        if icon_val:
            img = await fetch_image_bytes(icon_val)
            if img:
                try:
                    await new_role.edit(display_icon=img)
                except Exception:
                    pass

        try:
            await member.add_roles(new_role, reason="Assigned custom role")
        except Exception:
            pass

        bot.data_manager.roles[str(member.id)] = {
            "role_id": new_role.id,
            "name": name,
            "color": color_text,
            "icon": icon_val if icon_val else None,
            "created_at": now_iso()
        }
        await bot.data_manager.save_roles()

        embed = make_embed(
            "Custom Role Created",
            f"> Your role {new_role.mention} has been created successfully.",
            kind="success",
            scope=SCOPE_ROLES,
            guild=guild,
        )
        embed.color = color
        embed.add_field(name="Role", value=f"{new_role.mention}", inline=False)
        embed.add_field(name="Color", value=color_text, inline=True)
        if icon_val:
            embed.set_thumbnail(url=icon_val)
        await interaction.followup.send(embed=embed, ephemeral=True)

class EditNameModal(discord.ui.Modal, title="Edit role name"):
    new_name = discord.ui.TextInput(label="New role name", max_length=100)
    def __init__(self, member, role):
        super().__init__()
        self.member = member
        self.role = role
    async def on_submit(self, interaction):
        name = self.new_name.value.strip()[:100]
        try:
            await self.role.edit(name=name, reason=f"Renamed by {interaction.user}")
        except Exception as e:
            await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
            return
        rec = bot.data_manager.roles.get(str(self.member.id))
        if rec:
            rec["name"] = name
            await bot.data_manager.save_roles()
        embed = make_embed(
            "Role Renamed",
            f"> The custom role has been renamed to `{name}`.",
            kind="success",
            scope=SCOPE_ROLES,
            guild=interaction.guild,
        )
        embed.color = self.role.color
        await interaction.response.send_message(embed=embed, ephemeral=True)

class EditColorModal(discord.ui.Modal, title="Edit role color"):
    new_color = discord.ui.TextInput(label="Hex color", placeholder="#FF66CC", max_length=7)
    def __init__(self, member, role):
        super().__init__()
        self.member = member
        self.role = role
    async def on_submit(self, interaction):
        c = self.new_color.value.strip()
        if not hex_valid(c):
            await interaction.response.send_message("Invalid hex color.", ephemeral=True)
            return
        try:
            color = discord.Color(int(c.lstrip("#"),16))
            await self.role.edit(color=color, reason=f"Edited by {interaction.user}")
        except Exception as e:
            await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
            return
        rec = bot.data_manager.roles.get(str(self.member.id))
        if rec:
            rec["color"] = c
            await bot.data_manager.save_roles()
        embed = make_embed(
            "Role Color Updated",
            f"> The role color has been changed to `{c}`.",
            kind="success",
            scope=SCOPE_ROLES,
            guild=interaction.guild,
        )
        embed.color = color
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ConfirmRevokeView(discord.ui.View):
    def __init__(self, parent_view, target_message):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.target_message = target_message

    @discord.ui.button(label="Yes, Revoke", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.parent_view.finish_revoke(interaction, self.target_message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Revocation cancelled.", view=None)

class DenyAppealModal(discord.ui.Modal, title="Deny Appeal"):
    reason = discord.ui.TextInput(label="Reason for Denial", style=discord.TextStyle.paragraph, required=True)

    def __init__(self, target_id: int, origin_message: discord.Message, view: discord.ui.View):
        super().__init__()
        self.target_id = target_id
        self.origin_message = origin_message
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        embed = self.origin_message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Status", value=f"> Denied by {interaction.user.mention}\n> Reason: {self.reason.value}", inline=False)
        brand_embed(embed, guild=interaction.guild, scope=SCOPE_MODERATION)
        
        for child in self.view.children:
            child.disabled = True
        
        await self.origin_message.edit(embed=embed, view=self.view)
        
        user = interaction.guild.get_member(self.target_id)
        if not user:
            try: user = await interaction.client.fetch_user(self.target_id)
            except Exception: user = None
            
        if user:
            try:
                dm_embed = make_embed(
                    "Appeal Denied",
                    f"> Your punishment appeal in **{interaction.guild.name}** was reviewed and denied.",
                    kind="danger",
                    scope=SCOPE_MODERATION,
                    guild=interaction.guild,
                    thumbnail=interaction.guild.icon.url if interaction.guild.icon else None,
                )
                dm_embed.add_field(name="Reason", value=format_reason_value(self.reason.value, limit=1024), inline=False)
                await user.send(embed=dm_embed)
            except Exception:
                pass
        
        await interaction.response.send_message("Appeal denied.", ephemeral=True)

class RevokeAppealView(discord.ui.View):
    def __init__(self, target_id: int, moderator_id: int, duration: int, timestamp: str):
        super().__init__(timeout=None)
        self.target_id = target_id
        self.moderator_id = moderator_id
        self.duration = duration
        self.timestamp = timestamp

    @discord.ui.button(label="Revoke Punishment", style=discord.ButtonStyle.danger, custom_id="revoke_punishment_btn")
    async def start_revoke(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Are you sure you want to revoke this punishment?", view=ConfirmRevokeView(self, interaction.message), ephemeral=True)

    @discord.ui.button(label="Deny Appeal", style=discord.ButtonStyle.secondary, custom_id="deny_appeal_btn")
    async def deny_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DenyAppealModal(self.target_id, interaction.message, self))

    async def finish_revoke(self, interaction: discord.Interaction, message: discord.Message):
        await interaction.response.edit_message(content="Processing revocation...", view=None)
        
        guild = interaction.guild
        uid = str(self.target_id)
        revoked_record = None
        records = bot.data_manager.punishments.get(uid, [])
        for record in records:
            if record.get("timestamp") == self.timestamp:
                revoked_record = record
                break
        case_label = get_case_label(revoked_record) if revoked_record else "Case"
        
        # 1. Remove from database
        if uid in bot.data_manager.punishments:
            original_len = len(bot.data_manager.punishments[uid])
            bot.data_manager.punishments[uid] = [r for r in bot.data_manager.punishments[uid] if r.get("timestamp") != self.timestamp]
            
            if len(bot.data_manager.punishments[uid]) != original_len:
                await bot.data_manager.save_punishments()

        # 2. Reverse Stats
        mod_id = str(self.moderator_id)
        if "reversals" not in bot.data_manager.mod_stats: bot.data_manager.mod_stats["reversals"] = {}
        bot.data_manager.mod_stats["reversals"][mod_id] = bot.data_manager.mod_stats["reversals"].get(mod_id, 0) + 1
        await bot.data_manager.save_mod_stats()

        # 3. Physical Revocation
        action_taken = "Record removed"
        try:
            if self.duration == -1:
                # Unban
                user_obj = discord.Object(id=self.target_id)
                try:
                    await guild.unban(user_obj, reason=f"Appeal Accepted by {interaction.user}")
                    action_taken = "Unbanned & Record removed"
                except Exception:
                    action_taken = "User not banned (Record removed)"
            elif self.duration > 0:
                # Untimeout
                member = guild.get_member(self.target_id)
                if member:
                    if member.is_timed_out():
                        await member.timeout(None, reason=f"Appeal Accepted by {interaction.user}")
                        action_taken = "Timeout removed & Record removed"
                    else:
                        action_taken = "User not timed out (Record removed)"
                else:
                    action_taken = "User not in server (Record removed)"
            else:
                # Warning
                action_taken = "Warning revoked (Points removed)"
        except Exception as e:
            action_taken = f"Revocation error: {e}"

        # 4. Update Embed
        embed = message.embeds[0]
        embed.color = discord.Color.green()
        embed.title = f"{case_label} Appeal Resolved"
        embed.add_field(name="Status", value=f"> Revoked by {interaction.user.mention}\n> {action_taken}", inline=False)
        brand_embed(embed, guild=guild, scope=SCOPE_MODERATION)
        
        self.children[0].label = "Punishment Revoked"
        for child in self.children:
            child.disabled = True
        await message.edit(embed=embed, view=self)

        # 5. DM User
        user = interaction.guild.get_member(self.target_id)
        if not user:
            try:
                user = await interaction.client.fetch_user(self.target_id)
            except Exception:
                user = None
            
        if user:
            try:
                dm_embed = make_embed(
                    "Punishment Revoked",
                    f"> {case_label} in **{interaction.guild.name}** has been revoked.",
                    kind="success",
                    scope=SCOPE_MODERATION,
                    guild=interaction.guild,
                    thumbnail=interaction.guild.icon.url if interaction.guild.icon else None,
                )
                dm_embed.add_field(name="Outcome", value=truncate_text(action_taken, 1024), inline=False)
                await user.send(embed=dm_embed)
            except Exception:
                pass
            
        await interaction.followup.send("Punishment revoked successfully.", ephemeral=True)
        
        # 6. Log to General Logs (if different from current channel)
        target_str = format_user_ref(user) if user else format_user_id_ref(self.target_id, fallback_name=(revoked_record or {}).get("target_name"))
        log_embed = make_action_log_embed(
            f"{case_label} Revoked",
            "A punishment appeal was accepted and the system attempted to reverse the action.",
            guild=guild,
            kind="success",
            scope=SCOPE_MODERATION,
            actor=format_user_ref(interaction.user),
            target=target_str,
            reason="Appeal accepted",
            duration="Revoked",
            expires="N/A",
            notes=[f"Result: {truncate_text(action_taken, 500)}"],
            thumbnail=user.display_avatar.url if user else None,
        )
        await send_punishment_log(guild, log_embed)

class AppealModal(discord.ui.Modal, title="Appeal Punishment"):
    reason = discord.ui.TextInput(label="Why should this be revoked?", style=discord.TextStyle.paragraph, max_length=500)
    
    def __init__(self, guild_id: int, target_id: int, moderator_id: int, duration: int, timestamp: str, original_reason: str):
        super().__init__()
        self.guild_id = guild_id
        self.target_id = target_id
        self.moderator_id = moderator_id
        self.duration = duration
        self.timestamp = timestamp
        self.original_reason = original_reason

    async def on_submit(self, interaction: discord.Interaction):
        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("Server not found.", ephemeral=True)
            return

        record = next(
            (
                item for item in bot.data_manager.punishments.get(str(self.target_id), [])
                if item.get("timestamp") == self.timestamp
            ),
            None,
        )
        case_label = get_case_label(record) if record else "Case"

        embed = make_action_log_embed(
            f"{case_label} Appeal",
            "A user submitted an appeal for moderator review.",
            guild=guild,
            kind="warning",
            scope=SCOPE_MODERATION,
            actor=format_user_ref(interaction.user),
            target=case_label,
            reason=self.original_reason,
            message=self.reason.value,
            notes=[f"Moderator ID: {self.moderator_id}", f"Original Timestamp: {self.timestamp}"],
            thumbnail=interaction.user.display_avatar.url,
            author_name=f"{interaction.user.display_name} ({interaction.user.id})",
            author_icon=interaction.user.display_avatar.url,
        )
        
        view = RevokeAppealView(self.target_id, self.moderator_id, self.duration, self.timestamp)
        
        # Check for specific appeal channel
        appeal_cid = bot.data_manager.config.get("appeal_channel_id")
        sent = False
        if appeal_cid:
            appeal_chan = guild.get_channel(appeal_cid)
            if appeal_chan:
                try:
                    await appeal_chan.send(embed=embed, view=view)
                    sent = True
                except Exception:
                    pass
        
        # Fallback to General Logs only if Appeal Log failed or isn't set
        if not sent:
            await send_punishment_log(guild, embed, view=view)
            
        await interaction.response.send_message("Your appeal has been sent to the staff team.", ephemeral=True)

class AppealView(discord.ui.View):
    def __init__(self, guild_id: int, target_id: int, moderator_id: int, duration: int, timestamp: str, reason: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.target_id = target_id
        self.moderator_id = moderator_id
        self.duration = duration
        self.timestamp = timestamp
        self.reason = reason

    @discord.ui.button(label="Appeal Punishment", style=discord.ButtonStyle.secondary)
    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AppealModal(self.guild_id, self.target_id, self.moderator_id, self.duration, self.timestamp, self.reason))

class GradientModal(discord.ui.Modal, title="Set Gradient Style"):
    secondary = discord.ui.TextInput(label="Secondary Color (Hex)", placeholder="#RRGGBB", min_length=7, max_length=7)

    def __init__(self, member, role):
        super().__init__()
        self.member = member
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        sec_val = self.secondary.value.strip()
        if not hex_valid(sec_val):
            await interaction.response.send_message("Invalid hex color.", ephemeral=True)
            return

        sec_int = int(sec_val.lstrip("#"), 16)
        prim_int = self.role.color.value

        try:
            edited_role = await self.role.edit(
                color=prim_int,
                secondary_color=sec_int,
                tertiary_color=None,
                reason=f"Gradient style update by {interaction.user}",
            )
            if edited_role is not None:
                self.role = edited_role

            rec = bot.data_manager.roles.get(str(self.member.id))
            if rec:
                rec['color'] = f"#{prim_int:06X}"
                rec['secondary_color'] = sec_val
                rec['tertiary_color'] = None
                await bot.data_manager.save_roles()

            await interaction.response.send_message(
                embed=make_confirmation_embed(
                    "Gradient Style Applied",
                    f"> The role now uses Discord's enhanced gradient colors with secondary color `{sec_val}`.",
                    scope=SCOPE_ROLES,
                    guild=interaction.guild,
                ),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Failed to update style: {e.status} {e.text}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed to update style: {e}", ephemeral=True)

class RoleStyleView(discord.ui.View):
    def __init__(self, member, role):
        super().__init__(timeout=60)
        self.member = member
        self.role = role

    @discord.ui.button(label="Static (Reset)", style=discord.ButtonStyle.secondary)
    async def static_style(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            edited_role = await self.role.edit(
                color=self.role.color.value,
                secondary_color=None,
                tertiary_color=None,
                reason=f"Style reset by {interaction.user}",
            )
            if edited_role is not None:
                self.role = edited_role

            rec = bot.data_manager.roles.get(str(self.member.id))
            if rec:
                rec['secondary_color'] = None
                rec['tertiary_color'] = None
                await bot.data_manager.save_roles()
            await interaction.response.send_message("Role style reset to Static.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Failed: {e.status} {e.text}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Gradient", style=discord.ButtonStyle.primary)
    async def gradient_style(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GradientModal(self.member, self.role))

    @discord.ui.button(label="Holographic", style=discord.ButtonStyle.success)
    async def holographic_style(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            edited_role = await self.role.edit(
                color=HOLO_PRIMARY,
                secondary_color=HOLO_SECONDARY,
                tertiary_color=HOLO_TERTIARY,
                reason=f"Holographic style update by {interaction.user}",
            )
            if edited_role is not None:
                self.role = edited_role

            rec = bot.data_manager.roles.get(str(self.member.id))
            if rec:
                rec['color'] = f"#{HOLO_PRIMARY:06X}"
                rec['secondary_color'] = f"#{HOLO_SECONDARY:06X}"
                rec['tertiary_color'] = f"#{HOLO_TERTIARY:06X}"
                await bot.data_manager.save_roles()

            await interaction.response.send_message(
                embed=make_confirmation_embed(
                    "Holographic Style Applied",
                    "> The role now uses Discord's holographic enhanced role style preset.",
                    scope=SCOPE_ROLES,
                    guild=interaction.guild,
                ),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Failed: {e.status} {e.text}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

class IconURLModal(discord.ui.Modal, title="Set Icon via URL"):
    url = discord.ui.TextInput(label="Image URL", placeholder="https://...", required=True)

    def __init__(self, member, role):
        super().__init__()
        self.member = member
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        val = self.url.value.strip()
        
        img = await fetch_image_bytes(val)
        if not img:
            await interaction.followup.send("Failed to download image. Check the URL.", ephemeral=True)
            return
            
        if len(img) > 256000:
             await interaction.followup.send("Image too big! Max size is 256KB.", ephemeral=True)
             return

        try:
            await self.role.edit(display_icon=img, reason=f"Icon updated by {interaction.user}")
            rec = bot.data_manager.roles.get(str(self.member.id))
            if rec:
                rec["icon"] = val
                await bot.data_manager.save_roles()
            await interaction.followup.send("Icon updated successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to update icon: {e}", ephemeral=True)

class UploadIconView(discord.ui.View):
    def __init__(self, member, role):
        super().__init__(timeout=60)
        self.member = member
        self.role = role

    @discord.ui.button(label="Upload File", style=discord.ButtonStyle.primary)
    async def upload_file(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        
        await interaction.followup.send(f"{interaction.user.mention}, please reply to this message with your image file now.", ephemeral=True)
        
        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id and m.attachments

        try:
            msg = await bot.wait_for('message', check=check, timeout=60)
            attachment = msg.attachments[0]
            if attachment.size > 256000:
                await interaction.followup.send("Image too big! Max size is 256KB.", ephemeral=True)
                return
            
            img_data = await attachment.read()
            await self.role.edit(display_icon=img_data, reason=f"Icon updated by {interaction.user}")
            await interaction.followup.send("Icon updated successfully!", ephemeral=True)
            
            rec = bot.data_manager.roles.get(str(self.member.id))
            if rec:
                rec["icon"] = attachment.url
                await bot.data_manager.save_roles()
            
            try: await msg.delete()
            except Exception: pass

        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Enter URL", style=discord.ButtonStyle.secondary)
    async def enter_url(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(IconURLModal(self.member, self.role))

class RoleActionSelect(discord.ui.Select):
    def __init__(self, member, role):
        self.member = member
        self.role = role
        options = [
            discord.SelectOption(label="Rename Role", value="name", description="Change the role name."),
            discord.SelectOption(label="Change Color", value="color", description="Update the primary role color."),
            discord.SelectOption(label="Update Icon", value="icon", description="Open the icon upload or URL options."),
            discord.SelectOption(label="Change Style", value="style", description="Pick static, gradient, or holographic style."),
            discord.SelectOption(label="Delete Role", value="delete", description="Remove the custom role permanently."),
        ]
        super().__init__(placeholder="Choose a role action...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "name":
            await interaction.response.send_modal(EditNameModal(self.member, self.role))
            return
        if action == "color":
            await interaction.response.send_modal(EditColorModal(self.member, self.role))
            return
        if action == "icon":
            await interaction.response.send_message("Choose icon method:", view=UploadIconView(self.member, self.role), ephemeral=True)
            return
        if action == "style":
            await interaction.response.send_message("Choose a role style:", view=RoleStyleView(self.member, self.role), ephemeral=True)
            return
        if action == "delete":
            await interaction.response.send_message("Are you sure?", view=ConfirmDelete(self.member, self.role), ephemeral=True)

class EditView(discord.ui.View):
    def __init__(self, member, role):
        super().__init__(timeout=None)
        self.member = member
        self.role = role
        self.add_item(RoleActionSelect(member, role))

    @discord.ui.button(label="Refresh Panel", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        rec = bot.data_manager.roles.get(str(self.member.id))
        role_obj = interaction.guild.get_role(rec.get("role_id")) if rec else None
        if not rec or not role_obj:
            await interaction.response.edit_message(
                embed=make_empty_state_embed(
                    "Custom Role Not Found",
                    "> The tracked custom role could not be loaded. Re-run `/role` to create or reopen it.",
                    scope=SCOPE_ROLES,
                    guild=interaction.guild,
                    thumbnail=self.member.display_avatar.url,
                ),
                view=None,
            )
            return
        self.role = role_obj
        await interaction.response.edit_message(embed=build_role_info_embed(self.member, rec, role_obj, include_tips=True), view=EditView(self.member, role_obj))

class ConfirmDelete(discord.ui.View):
    def __init__(self, member, role):
        super().__init__(timeout=60)
        self.member = member
        self.role = role

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.role.delete(reason=f"Deleted by {interaction.user} (via Menu)")
        except Exception:
            pass
        bot.data_manager.roles.pop(str(self.member.id), None)
        await bot.data_manager.save_roles()
        await interaction.response.edit_message(content="Role deleted.", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Deletion canceled.", embed=None, view=None)
        self.stop()

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

    async def on_submit(self, interaction: discord.Interaction):
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

    async def on_submit(self, interaction: discord.Interaction):
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
                    await interaction.followup.send("Invalid duration for temporary ban.", ephemeral=True)
                    return
        elif self.p_type == "timeout":
            final_type = "timeout"
            if self.duration_str:
                minutes = parse_duration_str(self.duration_str.value)
                if minutes <= 0:
                    await interaction.followup.send("Invalid duration for timeout.", ephemeral=True)
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

    async def callback(self, interaction: discord.Interaction):
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

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "custom":
            await interaction.response.send_message("Select the type of custom punishment:", view=CustomTypeView(self.target, self.moderator, interaction.message, public=self.public, reaction_count=self.reaction_count), ephemeral=True)
            return
        reason = self.values[0]
        rules_config = bot.data_manager.config.get("punishment_rules", DEFAULT_RULES)
        rules = rules_config.get(reason)
        if not rules:
            return
        await interaction.response.send_modal(PunishDetailsModal(self.target, self.moderator, reason, rules, interaction.message, public=self.public, reaction_count=self.reaction_count))

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
            await interaction.followup.send(f"Failed to archive channel: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"Channel archived successfully to **{self.target_cat.name}**.", ephemeral=True)

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
            await interaction.followup.send(f"Failed to clone channel: {e}", ephemeral=True)
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
            await interaction.followup.send(f"Channel cloned to {new_channel.mention}, but failed to archive old channel: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"Success! Channel cloned to {new_channel.mention} and original archived.", ephemeral=True)
        
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
            await interaction.response.send_message(f"**Immune Users:**\n" + ", ".join(mentions), ephemeral=True)

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

async def log_modmail_action(guild, title, fields):
    cid = bot.data_manager.config.get("modmail_action_log_channel")
    if not cid: return
    channel = guild.get_channel(cid)
    if not channel: return

    embed = make_embed(title, "> A staff action was performed on a modmail ticket.", kind="support", scope=SCOPE_SUPPORT, guild=guild)
    for n, v in fields:
        embed.add_field(name=n, value=v, inline=True)
    try: await channel.send(embed=embed)
    except Exception: pass


def apply_modmail_ticket_state(embed: discord.Embed, ticket: dict, guild: discord.Guild) -> discord.Embed:
    status = str(ticket.get("status", "open")).title()
    priority = str(ticket.get("priority", "normal")).title()
    tags = ", ".join(f"`{tag}`" for tag in ticket.get("tags", [])) or "None"
    assigned = ticket.get("assigned_moderator")
    assignee = f"<@{assigned}>" if assigned else "Unclaimed"
    last_user = iso_to_dt(ticket.get("last_user_message_at"))
    last_staff = iso_to_dt(ticket.get("last_staff_message_at"))

    embed.color = EMBED_PALETTE["danger"] if ticket.get("status") == "closed" else (EMBED_PALETTE["warning"] if ticket.get("priority") in {"high", "urgent"} else EMBED_PALETTE["support"])
    upsert_embed_field(embed, "Status", status, inline=True)
    upsert_embed_field(embed, "Urgency", priority, inline=True)
    upsert_embed_field(embed, "Assigned To", assignee, inline=True)
    upsert_embed_field(
        embed,
        "Activity",
        join_lines([
            f"User: {discord.utils.format_dt(last_user, 'R') if last_user else 'Unknown'}",
            f"Staff: {discord.utils.format_dt(last_staff, 'R') if last_staff else 'No reply yet'}",
        ]),
        inline=True,
    )
    upsert_embed_field(embed, "Tags", tags, inline=True)
    brand_embed(embed, guild=guild, scope=SCOPE_SUPPORT)
    return embed


async def refresh_modmail_message(
    message: Optional[discord.Message],
    guild: Optional[discord.Guild],
    user_id: str,
    view: "ModmailControlView",
) -> bool:
    ticket = bot.data_manager.modmail.get(user_id)
    if not ticket or message is None or not message.embeds or guild is None:
        return False
    view.sync_buttons(ticket)
    embed = apply_modmail_ticket_state(message.embeds[0], ticket, guild)
    try:
        await message.edit(embed=embed, view=view)
        return True
    except discord.NotFound:
        logger.warning("Modmail panel message for user %s no longer exists.", user_id)
    except discord.Forbidden:
        logger.warning("Missing permission to refresh modmail panel message for user %s.", user_id)
    except discord.HTTPException as exc:
        logger.warning("Failed to refresh modmail panel message for user %s: %s", user_id, exc)
    return False


async def refresh_modmail_ticket_log(guild: discord.Guild, user_id: str):
    ticket = bot.data_manager.modmail.get(user_id)
    if not ticket:
        return
    log_channel_id = bot.data_manager.config.get("modmail_inbox_channel")
    log_id = ticket.get("log_id")
    if not log_channel_id or not log_id:
        return
    channel = guild.get_channel(log_channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(log_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    view = ModmailControlView(user_id)
    view.message = message
    await refresh_modmail_message(message, guild, user_id, view)


async def export_modmail_transcript(thread: discord.Thread, user_id: str) -> discord.File:
    messages = []
    async for message in thread.history(limit=None, oldest_first=True):
        messages.append({
            "author_name": message.author.display_name,
            "author_avatar_url": message.author.display_avatar.url,
            "created_at": message.created_at,
            "content": message.content,
            "attachments": [{"filename": attachment.filename, "url": attachment.url} for attachment in message.attachments],
            "channel_id": thread.id,
            "deleted": False,
            "edited": bool(message.edited_at),
        })
    transcript_user = SimpleNamespace(display_name=f"Ticket {user_id}", id=int(user_id))
    html_content = generate_transcript_html(messages, transcript_user)
    return discord.File(io.BytesIO(html_content.encode("utf-8")), filename=f"modmail_transcript_{user_id}.html")


def _parse_user_id(value: Union[str, int, None]) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def resolve_modmail_user(user_id: Union[str, int, None]) -> Optional[discord.User]:
    normalized_user_id = _parse_user_id(user_id)
    if normalized_user_id is None:
        return None
    cached = bot.get_user(normalized_user_id)
    if cached is not None:
        return cached
    try:
        return await bot.fetch_user(normalized_user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def resolve_modmail_thread(guild: Optional[discord.Guild], ticket: Optional[dict]) -> Optional[discord.Thread]:
    if guild is None or not isinstance(ticket, dict):
        return None

    thread_id = _parse_user_id(ticket.get("thread_id"))
    if thread_id is None:
        return None

    candidate = guild.get_thread(thread_id) or guild.get_channel_or_thread(thread_id)
    if isinstance(candidate, discord.Thread):
        return candidate

    try:
        fetched = await bot.fetch_channel(thread_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.Thread) else None

class ModmailPrioritySelect(discord.ui.Select):
    def __init__(self, panel: "ModmailControlView"):
        self.panel = panel
        ticket = bot.data_manager.modmail.get(panel.user_id, {})
        current = str(ticket.get("priority", "normal")).lower()
        options = [
            discord.SelectOption(label=priority.title(), value=priority, default=priority == current)
            for priority in DEFAULT_TICKET_PRIORITIES
        ]
        super().__init__(placeholder="Choose how urgent this ticket is...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        ticket = bot.data_manager.modmail.get(self.panel.user_id)
        if not ticket:
            await respond_with_error(interaction, "Ticket data not found.", scope=SCOPE_SUPPORT)
            return
        ticket["priority"] = self.values[0]
        await bot.data_manager.save_modmail()
        await refresh_modmail_message(self.panel.message or interaction.message, interaction.guild, self.panel.user_id, self.panel)
        await log_modmail_action(interaction.guild, "Ticket Priority Updated", [
            ("User", f"<@{self.panel.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Priority", self.values[0].title()),
        ])
        await interaction.response.edit_message(
            embed=make_confirmation_embed(
                "Ticket Priority Updated",
                f"> Priority set to **{self.values[0].title()}**.",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            ),
            view=None,
        )


class ModmailPriorityView(discord.ui.View):
    def __init__(self, panel: "ModmailControlView"):
        super().__init__(timeout=120)
        self.add_item(ModmailPrioritySelect(panel))


class ModmailTagsModal(discord.ui.Modal, title="Update Ticket Tags"):
    tags = discord.ui.TextInput(
        label="Tags",
        placeholder="bug, urgent, follow-up",
        max_length=200,
        required=False,
    )

    def __init__(self, panel: "ModmailControlView"):
        super().__init__()
        self.panel = panel
        ticket = bot.data_manager.modmail.get(panel.user_id, {})
        self.tags.default = ", ".join(ticket.get("tags", []))

    async def on_submit(self, interaction: discord.Interaction):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        ticket = bot.data_manager.modmail.get(self.panel.user_id)
        if not ticket:
            await respond_with_error(interaction, "Ticket data not found.", scope=SCOPE_SUPPORT)
            return
        ticket["tags"] = sanitize_tags(_split_case_input(self.tags.value), limit=10)
        await bot.data_manager.save_modmail()
        await refresh_modmail_message(self.panel.message, interaction.guild, self.panel.user_id, self.panel)
        await log_modmail_action(interaction.guild, "Ticket Tags Updated", [
            ("User", f"<@{self.panel.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Tags", ", ".join(ticket["tags"]) or "None"),
        ])
        await interaction.response.send_message(
            embed=make_confirmation_embed("Ticket Tags Updated", "> Ticket tags were updated.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            ephemeral=True,
        )


class CannedReplySelect(discord.ui.Select):
    def __init__(self, panel: "ModmailControlView"):
        self.panel = panel
        replies = bot.data_manager.config.get("modmail_canned_replies", {})
        options = [
            discord.SelectOption(label=key, value=key, description=truncate_text(value, 100))
            for key, value in list(replies.items())[:25]
        ]
        if not options:
            options.append(discord.SelectOption(label="No saved replies", value="__empty__", description="Add reply templates in /config"))
        super().__init__(placeholder="Choose a quick reply...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        if self.values[0] == "__empty__":
            await respond_with_error(interaction, "No saved replies have been set up yet.", scope=SCOPE_SUPPORT)
            return
        ticket = bot.data_manager.modmail.get(self.panel.user_id)
        if not ticket:
            await respond_with_error(interaction, "Ticket data not found.", scope=SCOPE_SUPPORT)
            return
        reply_key = self.values[0]
        reply_body = bot.data_manager.config.get("modmail_canned_replies", {}).get(reply_key, "")
        user = await resolve_modmail_user(self.panel.user_id)
        if user is None:
            await respond_with_error(interaction, "Unable to resolve the user for this ticket.", scope=SCOPE_SUPPORT)
            return
        try:
            embed = make_embed(
                "Staff Reply",
                truncate_text(reply_body, 4096),
                kind="info",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            )
            await user.send(embed=embed)
        except discord.Forbidden:
            await respond_with_error(interaction, "Unable to DM the user with the saved reply.", scope=SCOPE_SUPPORT)
            return
        except discord.HTTPException as exc:
            await respond_with_error(interaction, f"Failed to send the saved reply: {exc}", scope=SCOPE_SUPPORT)
            return

        ticket["last_staff_message_at"] = now_iso()
        await bot.data_manager.save_modmail()
        if isinstance(interaction.channel, discord.Thread):
            await interaction.channel.send(f"Sent quick reply `{reply_key}` to <@{self.panel.user_id}>.")
        await refresh_modmail_message(self.panel.message or interaction.message, interaction.guild, self.panel.user_id, self.panel)
        await log_modmail_action(interaction.guild, "Canned Reply Sent", [
            ("User", f"<@{self.panel.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Template", reply_key),
        ])
        await interaction.response.edit_message(
            embed=make_confirmation_embed("Quick Reply Sent", "> The saved reply was sent to the user.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            view=None,
        )


class CannedReplyView(discord.ui.View):
    def __init__(self, panel: "ModmailControlView"):
        super().__init__(timeout=120)
        self.add_item(CannedReplySelect(panel))


class ModmailControlView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.message: Optional[discord.Message] = None
        self.sync_buttons(bot.data_manager.modmail.get(self.user_id, {}))

    def sync_buttons(self, ticket: dict):
        status = ticket.get("status", "open")
        assigned = ticket.get("assigned_moderator")
        self.close_ticket.disabled = status == "closed"
        self.open_ticket.disabled = status != "closed"
        self.claim_ticket.label = "Unclaim Ticket" if assigned else "Claim Ticket"
        self.claim_ticket.style = discord.ButtonStyle.secondary if assigned else discord.ButtonStyle.success

    def _get_ticket(self) -> Optional[dict]:
        return bot.data_manager.modmail.get(self.user_id)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="mm_close", row=0)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        self.message = interaction.message
        ticket = self._get_ticket()
        if not ticket or ticket.get("status") == "closed":
            await interaction.response.send_message("Ticket is already closed.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        ticket["status"] = "closed"
        ticket["last_staff_message_at"] = now_iso()
        await bot.data_manager.save_modmail()

        thread = await resolve_modmail_thread(interaction.guild, ticket)

        transcript_file = None
        if isinstance(thread, discord.Thread):
            try:
                transcript_file = await export_modmail_transcript(thread, self.user_id)
            except Exception as exc:
                logger.warning("Failed to export modmail transcript for %s: %s", self.user_id, exc)

        await refresh_modmail_message(interaction.message, interaction.guild, self.user_id, self)

        if isinstance(thread, discord.Thread):
            try:
                await thread.send(f"**Ticket Closed** by {interaction.user.mention}.")
                await thread.edit(locked=True, archived=True)
            except discord.HTTPException as exc:
                logger.warning("Failed to finalize closed thread for %s: %s", self.user_id, exc)

        user = await resolve_modmail_user(self.user_id)
        if user is not None:
            dm_embed = make_embed(
                "Ticket Closed",
                "> Your support ticket has been closed by the staff team.\n> If you need anything else, open a new ticket anytime.",
                kind="danger",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            )
            try:
                await user.send(embed=dm_embed)
            except discord.HTTPException as exc:
                logger.warning("Failed to DM closed-ticket notice to %s: %s", self.user_id, exc)

        log_channel_id = bot.data_manager.config.get("modmail_action_log_channel")
        log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None
        if transcript_file and log_channel:
            try:
                await log_channel.send(content=f"Transcript for closed ticket <@{self.user_id}>", file=transcript_file)
            except discord.HTTPException as exc:
                logger.warning("Failed to upload modmail transcript for %s: %s", self.user_id, exc)

        await log_modmail_action(interaction.guild, "Ticket Closed", [
            ("User", f"<@{self.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Priority", str(ticket.get("priority", "normal")).title()),
            ("Ticket ID", str(ticket.get("thread_id", "N/A"))),
        ])
        await interaction.followup.send(
            embed=make_confirmation_embed("Ticket Closed", "> Ticket closed and transcript exported when available.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.success, custom_id="mm_open", disabled=True, row=0)
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        self.message = interaction.message
        ticket = self._get_ticket()
        if not ticket:
            await interaction.response.send_message("Ticket data not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        ticket["status"] = "open"
        ticket["last_staff_message_at"] = now_iso()
        await bot.data_manager.save_modmail()
        await refresh_modmail_message(interaction.message, interaction.guild, self.user_id, self)

        thread = await resolve_modmail_thread(interaction.guild, ticket)

        if isinstance(thread, discord.Thread):
            try:
                await thread.edit(locked=False, archived=False)
                await thread.send(f"**Ticket Re-opened** by {interaction.user.mention}.")
            except discord.HTTPException as exc:
                logger.warning("Failed to reopen thread for %s: %s", self.user_id, exc)

        user = await resolve_modmail_user(self.user_id)
        if user is not None:
            dm_embed = make_embed(
                "Ticket Re-opened",
                "> Your support ticket has been re-opened. You can continue messaging the staff team.",
                kind="success",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            )
            try:
                await user.send(embed=dm_embed)
            except discord.HTTPException as exc:
                logger.warning("Failed to DM reopened-ticket notice to %s: %s", self.user_id, exc)

        await log_modmail_action(interaction.guild, "Ticket Re-opened", [
            ("User", f"<@{self.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Ticket ID", str(ticket.get("thread_id", "N/A"))),
        ])
        await interaction.followup.send(
            embed=make_confirmation_embed("Ticket Re-opened", "> Ticket reopened successfully.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, custom_id="mm_claim", row=0)
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return

        self.message = interaction.message
        ticket = self._get_ticket()
        if not ticket:
            await interaction.response.send_message("Ticket data not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        current = ticket.get("assigned_moderator")
        ticket["assigned_moderator"] = None if current == interaction.user.id else interaction.user.id
        ticket["claimed_at"] = now_iso() if ticket.get("assigned_moderator") else None
        await bot.data_manager.save_modmail()
        await refresh_modmail_message(interaction.message, interaction.guild, self.user_id, self)
        await log_modmail_action(interaction.guild, "Ticket Assignment Updated", [
            ("User", f"<@{self.user_id}>"),
            ("Moderator", interaction.user.mention),
            ("Assigned", interaction.user.mention if ticket.get("assigned_moderator") else "Unclaimed"),
        ])
        await interaction.followup.send("Ticket assignment updated.", ephemeral=True)

    @discord.ui.button(label="Urgency", style=discord.ButtonStyle.primary, custom_id="mm_priority", row=1)
    async def priority(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        self.message = interaction.message
        await interaction.response.send_message(
            embed=make_embed("Ticket Urgency", "> Choose how urgent this ticket is for staff.", kind="warning", scope=SCOPE_SUPPORT, guild=interaction.guild),
            view=ModmailPriorityView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="Tags", style=discord.ButtonStyle.primary, custom_id="mm_tags", row=1)
    async def tags(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        self.message = interaction.message
        await interaction.response.send_modal(ModmailTagsModal(self))

    @discord.ui.button(label="Quick Reply", style=discord.ButtonStyle.secondary, custom_id="mm_canned", row=1)
    async def canned_reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        self.message = interaction.message
        await interaction.response.send_message(embed=build_canned_replies_embed(interaction.guild), view=CannedReplyView(self), ephemeral=True)

    @discord.ui.button(label="Download Transcript", style=discord.ButtonStyle.secondary, custom_id="mm_export", row=1)
    async def export_transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
            await interaction.response.send_message("Access denied.", ephemeral=True)
            return
        ticket = self._get_ticket()
        thread = await resolve_modmail_thread(interaction.guild, ticket)
        if not isinstance(thread, discord.Thread):
            await respond_with_error(interaction, "Transcript export is only available from the ticket thread.", scope=SCOPE_SUPPORT)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        file = await export_modmail_transcript(thread, self.user_id)
        await interaction.followup.send(
            embed=make_confirmation_embed("Transcript Ready", "> The ticket transcript has been generated.", scope=SCOPE_SUPPORT, guild=interaction.guild),
            file=file,
            ephemeral=True,
        )

class ModmailModal(discord.ui.Modal):
    def __init__(self, category: str):
        super().__init__(title=f"Open {category} Ticket")
        self.category = category
        
        if category == "Report":
            self.add_item(discord.ui.TextInput(label="Reported User (ID or Name)", placeholder="e.g. 123456789...", required=True))
            self.add_item(discord.ui.TextInput(label="Reason", placeholder="Short summary...", required=True))
            self.add_item(discord.ui.TextInput(label="Evidence / Details", style=discord.TextStyle.paragraph, placeholder="Please provide links or detailed explanation...", required=True))
        elif category == "Partnership":
            self.add_item(discord.ui.TextInput(label="Server Name", required=True))
            self.add_item(discord.ui.TextInput(label="Server Link (Permanent)", required=True))
            self.add_item(discord.ui.TextInput(label="Subject", style=discord.TextStyle.paragraph, required=True))
        else:
            # Support
            self.add_item(discord.ui.TextInput(label="Subject", placeholder="Brief title...", required=True))
            self.add_item(discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, placeholder="How can we help?", required=True))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = get_context_guild(interaction)
        if guild is None:
            await interaction.followup.send("This server could not be resolved for modmail. Ask an administrator to set the Guild ID in setup.", ephemeral=True)
            return

        existing_ticket = bot.data_manager.modmail.get(str(interaction.user.id))
        if existing_ticket and existing_ticket.get("status") == "open":
            await interaction.followup.send("You already have an open ticket. Keep replying in DM and staff will receive it.", ephemeral=True)
            return
        
        log_channel_id = bot.data_manager.config.get("modmail_inbox_channel")
        if not log_channel_id:
            await interaction.followup.send("Modmail system is not fully configured (Inbox channel missing). Contact admin.", ephemeral=True)
            return
            
        log_channel = guild.get_channel(log_channel_id)
        if not log_channel:
            await interaction.followup.send("Inbox channel not found.", ephemeral=True)
            return

        # Create Log Embed
        embed = make_embed(
            f"New Ticket: {self.category}",
            "> A new ticket has been submitted through the support panel.",
            kind="support",
            scope=SCOPE_SUPPORT,
            guild=guild,
            thumbnail=interaction.user.display_avatar.url,
            author_name=f"{interaction.user.display_name} ({interaction.user.id})",
            author_icon=interaction.user.display_avatar.url,
        )
        
        fields_data = []
        for child in self.children:
            field_label = get_modal_item_label(child)
            embed.add_field(name=field_label, value=f">>> {child.value}", inline=False)
            fields_data.append(f"**{field_label}**: {child.value}")

        ticket_payload = {
            "status": "open",
            "category": self.category,
            "created_at": now_iso(),
            "priority": "normal",
            "tags": [],
            "assigned_moderator": None,
            "claimed_at": None,
            "last_user_message_at": now_iso(),
            "last_staff_message_at": None,
            "last_sla_alert_at": None,
        }
        normalize_modmail_ticket(ticket_payload)
        apply_modmail_ticket_state(embed, ticket_payload, guild)
        
        # Send Log & Create Thread
        try:
            view = ModmailControlView(str(interaction.user.id))
            
            ping_roles = bot.data_manager.config.get("modmail_ping_roles", [])
            if ping_roles:
                pings = " ".join([f"<@&{rid}>" for rid in ping_roles])
            else:
                r_mod = bot.data_manager.config.get("role_mod", DEFAULT_ROLE_MOD)
                r_admin = bot.data_manager.config.get("role_admin", DEFAULT_ROLE_ADMIN)
                r_cm = bot.data_manager.config.get("role_community_manager", DEFAULT_ROLE_COMMUNITY_MANAGER)
                pings = f"<@&{r_mod}> <@&{r_admin}> <@&{r_cm}>"

            log_msg = await log_channel.send(content=f"New Ticket from {interaction.user.mention} {pings}", embed=embed, view=view)
            thread = await log_msg.create_thread(name=f"ticket-{interaction.user.name}")
            
            # Create Staff Discussion Thread
            if bot.data_manager.config.get("modmail_discussion_threads", True):
                disc_msg = await log_channel.send(f"**Staff Discussion** for {interaction.user.mention} (Ticket #{log_msg.id})")
                await disc_msg.create_thread(name=f"discuss-{interaction.user.name}")
            
            # Save Ticket Data
            ticket_payload["thread_id"] = thread.id
            ticket_payload["log_id"] = log_msg.id
            bot.data_manager.modmail[str(interaction.user.id)] = ticket_payload
            await bot.data_manager.save_modmail()
            
            # Initial Thread Msg
            await thread.send(f"**Ticket Created**\nUser: {interaction.user.mention}\nCategory: {self.category}\n\n" + "\n".join(fields_data))
            
            # DM User
            dm_embed = make_embed(
                "Ticket Created",
                f"> Your **{self.category}** ticket has been opened.\n> A staff member will be with you shortly.\n> Reply to this DM to send further details.",
                kind="support",
                scope=SCOPE_SUPPORT,
                guild=interaction.guild,
            )
            await interaction.user.send(embed=dm_embed)
            
            # Log Action
            await log_modmail_action(guild, "Ticket Created", [
                ("User", interaction.user.mention),
                ("Category", self.category),
                ("Ticket ID", str(thread.id))
            ])
            
            await interaction.followup.send("Ticket created successfully! Check your DMs.", ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"Failed to create ticket: {e}", ephemeral=True)

class ModmailPanelSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=label, description=truncate_text(description, 100))
            for label, description in MODMAIL_PANEL_CATEGORIES
        ]
        super().__init__(
            placeholder="Choose the ticket type you want to open...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="mm_ticket_type_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ModmailModal(self.values[0]))


class ModmailPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ModmailPanelSelect())

def build_role_settings_embed(guild: discord.Guild) -> discord.Embed:
    conf = bot.data_manager.config
    embed = make_embed(
        "Custom Role Settings",
        "> Manage who can create custom roles, review tracked roles, and open admin edit tools from one control panel.",
        kind="info",
        scope=SCOPE_ROLES,
        guild=guild,
    )
    embed.add_field(name="Whitelisted Users", value=str(len(conf.get("cr_whitelist_users", {}))), inline=True)
    embed.add_field(name="Whitelisted Roles", value=str(len(conf.get("cr_whitelist_roles", {}))), inline=True)
    embed.add_field(name="Blocked Entries", value=str(len(conf.get("cr_blacklist_users", [])) + len(conf.get("cr_blacklist_roles", []))), inline=True)
    embed.add_field(name="Tracked Custom Roles", value=str(len(bot.data_manager.roles)), inline=True)
    embed.add_field(
        name="What You Can Do",
        value=join_lines([
            "Review the current allow/block lists.",
            "Allow or block specific members or roles from custom role access.",
            "Reset one entry or open a member's custom role admin panel.",
        ]),
        inline=False,
    )
    return embed


def build_role_permissions_overview_embed(guild: discord.Guild) -> discord.Embed:
    conf = bot.data_manager.config
    embed = make_embed(
        "Custom Role Access Rules",
        "> Current allow and block rules for the booster custom role system.",
        kind="info",
        scope=SCOPE_ROLES,
        guild=guild,
    )

    wl_users = conf.get("cr_whitelist_users", {})
    if wl_users:
        lines = [f"<@{uid}>: {limit}" for uid, limit in wl_users.items()]
        embed.add_field(name="Allowed Users", value=truncate_text("\n".join(lines), 1024), inline=False)
    else:
        embed.add_field(name="Allowed Users", value="None", inline=False)

    wl_roles = conf.get("cr_whitelist_roles", {})
    if wl_roles:
        lines = [f"<@&{rid}>: {limit}" for rid, limit in wl_roles.items()]
        embed.add_field(name="Allowed Roles", value=truncate_text("\n".join(lines), 1024), inline=False)
    else:
        embed.add_field(name="Allowed Roles", value="None", inline=False)

    bl_users = conf.get("cr_blacklist_users", [])
    embed.add_field(name="Blocked Users", value=truncate_text(", ".join(f"<@{uid}>" for uid in bl_users) or "None", 1024), inline=False)
    bl_roles = conf.get("cr_blacklist_roles", [])
    embed.add_field(name="Blocked Roles", value=truncate_text(", ".join(f"<@&{rid}>" for rid in bl_roles) or "None", 1024), inline=False)
    return embed


def split_embed_entries(entries: List[str], *, limit: int = 1024) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_length = 0

    for raw_entry in entries:
        entry = truncate_text(raw_entry, min(limit, 950))
        entry_length = len(entry)
        if current and current_length + entry_length + 2 > limit:
            chunks.append("\n\n".join(current))
            current = [entry]
            current_length = entry_length
            continue
        current.append(entry)
        current_length = current_length + entry_length + (2 if len(current) > 1 else 0)

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def build_custom_role_registry_entries(guild: discord.Guild) -> List[str]:
    entries: List[Tuple[str, str]] = []
    for uid, data in bot.data_manager.roles.items():
        rid = data.get("role_id")
        role = guild.get_role(rid) if rid else None
        owner = guild.get_member(int(uid)) if str(uid).isdigit() else None
        role_name = discord.utils.escape_markdown(str(role.name if role else data.get("name", "Unknown") or "Unknown"))
        role_ref = role.mention if role else f"`Missing role ({rid or 'unknown'})`"
        owner_ref = owner.mention if owner else f"<@{uid}>"

        entry_lines = [
            f"**{truncate_text(role_name, 90)}**",
            f"> Role: {role_ref}",
            f"> Owner: {owner_ref}",
        ]
        if role is None:
            entry_lines.append("> Status: Missing from server")
        entries.append((role_name.lower(), "\n".join(entry_lines)))

    entries.sort(key=lambda item: item[0])
    return [entry for _, entry in entries]


def add_custom_role_registry_fields(embed: discord.Embed, guild: discord.Guild, *, field_name: str = "Registry") -> int:
    entries = build_custom_role_registry_entries(guild)
    if not entries:
        embed.add_field(name=field_name, value="No custom roles are currently tracked.", inline=False)
        return 0

    for index, chunk in enumerate(split_embed_entries(entries)):
        name = field_name if index == 0 else f"{field_name} Cont."
        embed.add_field(name=name, value=chunk, inline=False)
    return len(entries)


def build_role_registry_embed(guild: discord.Guild) -> discord.Embed:
    embed = make_embed(
        "Tracked Custom Roles",
        "> Registry of current custom roles and their recorded owners.",
        kind="warning",
        scope=SCOPE_ROLES,
        guild=guild,
    )
    total_roles = add_custom_role_registry_fields(embed, guild, field_name="Registry")
    embed.add_field(name="Total Roles", value=str(total_roles), inline=True)
    return embed


class RoleSettingsTargetModal(discord.ui.Modal):
    target_value = discord.ui.TextInput(label="Target ID or Mention", placeholder="Paste a user or role ID", max_length=30)
    limit_value = discord.ui.TextInput(label="Role Limit", placeholder="1", required=False, max_length=3)

    def __init__(self, *, title: str, action: str, target_type: str, require_limit: bool = False):
        super().__init__(title=title)
        self.action = action
        self.target_type = target_type
        self.require_limit = require_limit
        self.limit_value.required = require_limit
        if not require_limit:
            self.remove_item(self.limit_value)

    async def on_submit(self, interaction: discord.Interaction):
        target_id = extract_snowflake_id(self.target_value.value)
        if not target_id:
            await interaction.response.send_message("Enter a valid ID or mention.", ephemeral=True)
            return

        if self.target_type == "member":
            target = interaction.guild.get_member(target_id)
            if not target:
                try:
                    target = await interaction.guild.fetch_member(target_id)
                except Exception:
                    target = None
        else:
            target = interaction.guild.get_role(target_id)

        if target is None:
            await interaction.response.send_message("That target could not be found in this server.", ephemeral=True)
            return

        limit = 1
        if self.require_limit:
            try:
                limit = max(1, int(self.limit_value.value or 1))
            except ValueError:
                await interaction.response.send_message("Role limit must be a number.", ephemeral=True)
                return

        await role_manage.callback(interaction, self.action, target, limit)


class RoleSettingsManageMemberModal(discord.ui.Modal, title="Open Member Role Panel"):
    member_value = discord.ui.TextInput(label="Member ID or Mention", placeholder="Paste a user ID", max_length=30)

    async def on_submit(self, interaction: discord.Interaction):
        member_id = extract_snowflake_id(self.member_value.value)
        if not member_id:
            await interaction.response.send_message("Enter a valid member ID or mention.", ephemeral=True)
            return
        member = interaction.guild.get_member(member_id)
        if not member:
            try:
                member = await interaction.guild.fetch_member(member_id)
            except Exception:
                member = None
        if member is None:
            await interaction.response.send_message("That member could not be found in this server.", ephemeral=True)
            return
        await role_manage.callback(interaction, "manage_user", member, 1)


class RoleSettingsAccessSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Allow Member", value="whitelist_member", description="Whitelist one member and set a role limit."),
            discord.SelectOption(label="Allow Role", value="whitelist_role", description="Whitelist one role and set a role limit."),
            discord.SelectOption(label="Block Member", value="blacklist_member", description="Block one member from custom role access."),
            discord.SelectOption(label="Block Role", value="blacklist_role", description="Block one role from custom role access."),
            discord.SelectOption(label="Reset Member", value="reset_member", description="Remove one member from all role access lists."),
            discord.SelectOption(label="Reset Role", value="reset_role", description="Remove one role from all role access lists."),
        ]
        super().__init__(placeholder="Choose an access rule action...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == "whitelist_member":
            await interaction.response.send_modal(RoleSettingsTargetModal(title="Allow Member", action="whitelist", target_type="member", require_limit=True))
            return
        if value == "whitelist_role":
            await interaction.response.send_modal(RoleSettingsTargetModal(title="Allow Role", action="whitelist", target_type="role", require_limit=True))
            return
        if value == "blacklist_member":
            await interaction.response.send_modal(RoleSettingsTargetModal(title="Block Member", action="blacklist", target_type="member"))
            return
        if value == "blacklist_role":
            await interaction.response.send_modal(RoleSettingsTargetModal(title="Block Role", action="blacklist", target_type="role"))
            return
        if value == "reset_member":
            await interaction.response.send_modal(RoleSettingsTargetModal(title="Reset Member", action="reset", target_type="member"))
            return
        if value == "reset_role":
            await interaction.response.send_modal(RoleSettingsTargetModal(title="Reset Role", action="reset", target_type="role"))


class RoleSettingsAccessView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(RoleSettingsAccessSelect())


class RoleSettingsActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Refresh Overview", value="refresh", description="Reload the counts and dashboard summary."),
            discord.SelectOption(label="Review Access", value="review_access", description="Open the current allow and block lists."),
            discord.SelectOption(label="Tracked Roles", value="tracked_roles", description="Open the current custom role registry."),
            discord.SelectOption(label="Change Access Rules", value="access_rules", description="Open the access rule action menu."),
            discord.SelectOption(label="Manage Member Role", value="manage_member", description="Open one member's custom role panel."),
        ]
        super().__init__(placeholder="Choose a role settings action...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "refresh":
            await interaction.response.edit_message(embed=build_role_settings_embed(interaction.guild), view=RoleSettingsView())
            return
        if action == "review_access":
            await interaction.response.send_message(embed=build_role_permissions_overview_embed(interaction.guild), ephemeral=True)
            return
        if action == "tracked_roles":
            await interaction.response.send_message(embed=build_role_registry_embed(interaction.guild), ephemeral=True)
            return
        if action == "access_rules":
            await interaction.response.send_message(
                embed=build_role_permissions_overview_embed(interaction.guild),
                view=RoleSettingsAccessView(),
                ephemeral=True,
            )
            return
        if action == "manage_member":
            await interaction.response.send_modal(RoleSettingsManageMemberModal())


class RoleSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(RoleSettingsActionSelect())


# ----------------- Commands -----------------
# --- Command Groups ---

@tree.command(name="role", description="Manage your personal custom role")
async def role_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException as e:
        if e.code != 40060:
            raise e
    
    # Check for Booster or Whitelist
    is_booster = interaction.user.premium_since is not None
    limit = get_custom_role_limit(interaction.user)
    
    if not is_booster and limit <= 0:
        await interaction.followup.send("You must be a **Server Booster** to use this perk.", ephemeral=True)
        return

    rec = bot.data_manager.roles.get(str(interaction.user.id))
    
    # Check if role exists on Discord
    role = None
    if rec:
        role_id = rec.get("role_id")
        role = interaction.guild.get_role(role_id)
        if not role:
            try:
                role = await interaction.guild.fetch_role(role_id)
            except discord.NotFound:
                # Role was deleted manually, clean up DB
                bot.data_manager.roles.pop(str(interaction.user.id), None)
                await bot.data_manager.save_roles()
                rec = None
            except Exception: pass
    
    if role:
        # User has a valid role -> Show Manage View
        embed = build_role_info_embed(interaction.user, rec, role, include_tips=True)
        view = EditView(interaction.user, role)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        # User has no role (or it's deleted) -> Show Create Option
        embed = build_role_landing_embed(interaction.user, is_booster=is_booster, limit=max(1, limit))
        view = discord.ui.View()
        btn = discord.ui.Button(label="Create Role", style=discord.ButtonStyle.success)
        
        async def create_callback(inter: discord.Interaction):
            await inter.response.send_modal(CreateRoleModal(inter.user))
        
        btn.callback = create_callback
        view.add_item(btn)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# --- Setup / Config System ---
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


class ModmailSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ModmailDiscussionThreadSelect())

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

# --- Permission Checks ---
def check_admin(interaction: discord.Interaction) -> bool:
    return has_permission_capability(interaction, "setup_panel")

def check_owner(interaction: discord.Interaction) -> bool:
    return has_permission_capability(interaction, "owner_panel")

@tree.command(name="listcommands", description="List all available commands | admin/owner")
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

class RevokeUndoView(discord.ui.View):
    def __init__(self, target_id: int, record: dict, actor_id: int):
        super().__init__(timeout=None)
        self.target_id = target_id
        self.record = record
        self.actor_id = actor_id

    @discord.ui.button(label="Revoke Undo", style=discord.ButtonStyle.danger)
    async def revoke_undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction):
             await interaction.response.send_message("Access denied.", ephemeral=True)
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

@app_commands.default_permissions(moderate_members=True)
class ModGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="mod", description="Advanced moderation suite")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_staff(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="punish", description="Sanction a user with a warning, timeout, or ban | mod")
    @app_commands.default_permissions(moderate_members=True)
    async def punish(self, interaction: discord.Interaction, user: discord.User):
        await show_punish_menu(interaction, user)

    @app_commands.command(name="publicpunish", description="Punish a user and announce it publicly in this channel | mod")
    @app_commands.default_permissions(moderate_members=True)
    async def publicpunish(self, interaction: discord.Interaction, user: discord.User):
        await show_punish_menu(interaction, user, public=True)

    @app_commands.command(name="history", description="Retrieve the complete disciplinary history of a user | mod")
    @app_commands.default_permissions(moderate_members=True)
    async def history(self, interaction: discord.Interaction, user: discord.Member):
        await show_history_menu(interaction, user)

    @app_commands.command(name="active", description="Display a list of all currently active punishments | mod")
    @app_commands.default_permissions(moderate_members=True)
    async def active(self, interaction: discord.Interaction):
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
                    # Bans are always active for this list
                    expiry = datetime.max.replace(tzinfo=timezone.utc)
                elif dur > 0:
                    expiry = ts + timedelta(minutes=dur)
                
                if dur == -1 or expiry > now:
                    member = interaction.guild.get_member(int(uid))
                    name = member.display_name if member else uid
                    active_list.append((uid, rec, expiry, i+1, name))
        if not active_list:
            await interaction.followup.send("No active punishments found.", ephemeral=True)
            return
        active_list.sort(key=lambda x: x[2])
        embed = build_active_punishments_embed(interaction.guild, active_list, now)
        view = ActiveView(active_list)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="undopunish", description="Open the punishment undo control panel | mod")
    @app_commands.describe(reason="Optional reason to prefill in the undo panel")
    @app_commands.default_permissions(moderate_members=True)
    async def undopunish(self, interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
        await show_history_menu(interaction, user, mode="undo", initial_undo_reason=reason)

    @app_commands.command(name="purge", description="Bulk delete messages (Channel or User) | mod")
    @app_commands.describe(amount="Messages to check/delete (max 999)", user="Optional: Target specific user", keyword="Optional: Filter by keyword")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int, user: discord.Member = None, keyword: str = None):
        if amount < 1 or amount > 999:
            await interaction.response.send_message("Amount must be between 1 and 999.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Scenario 1: Simple Channel Purge (No filters)
        if not user and not keyword:
            try:
                deleted = await interaction.channel.purge(limit=amount)
                await interaction.followup.send(f"Cleared **{len(deleted)}** messages.", ephemeral=True)
                
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
                await interaction.followup.send(f"Failed to purge: {e}", ephemeral=True)
            return

        # Scenario 2: Filtered Purge (User or Keyword)
        to_delete = []
        manual_delete = []
        deleted_count = 0
        
        now = discord.utils.utcnow()
        two_weeks_ago = now - timedelta(days=14)
        
        # Scan deeper for filtered purge
        async for message in interaction.channel.history(limit=10000):
            if deleted_count + len(to_delete) + len(manual_delete) >= amount:
                break
                
            # Filter Logic
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
             await interaction.followup.send(f"No matching messages found to purge.", ephemeral=True)
             return

        target_str = user.mention if user else "Anyone"
        await interaction.followup.send(f"Cleared **{deleted_count}** messages from {target_str}.", ephemeral=True)
        
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

    @app_commands.command(name="lock", description="Restrict message sending permissions in this channel | mod")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction):
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
            await interaction.followup.send("Channel locked.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="unlock", description="Restore message sending permissions in this channel | mod")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
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
            await interaction.followup.send("Channel unlocked.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="help", description="View all moderation commands")
    async def help(self, interaction: discord.Interaction):
        embed = build_mod_help_embed(interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="case", description="Open the case panel for a user or case ID | mod")
    @app_commands.describe(case_id="Open a specific case by ID", user="Open the most recent case for a user")
    async def case(self, interaction: discord.Interaction, case_id: Optional[app_commands.Range[int, 1, 999999]] = None, user: Optional[discord.Member] = None):
        await show_case_panel(interaction, case_id=case_id, user=user)


# --- Admin Commands (Flattened) ---

@tree.command(name="stats", description="Display comprehensive server-wide moderation analytics | admin")
@app_commands.default_permissions(manage_guild=True)
async def stats(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    conf = bot.data_manager.config
    allowed = {
        conf.get("role_admin", DEFAULT_ROLE_ADMIN),
        conf.get("role_owner", DEFAULT_ROLE_OWNER),
        conf.get("role_community_manager", DEFAULT_ROLE_COMMUNITY_MANAGER)
    }
    if not any(r.id in allowed for r in interaction.user.roles):
        await interaction.response.send_message("**Access Denied:** You do not have the required Admin role.", ephemeral=True)
        return

    if target:
        uid = str(target.id)
        cases = get_mod_cases(uid)
        
        # Check if user is currently staff or has history
        is_staff_member = False
        if target.guild_permissions.administrator:
            is_staff_member = True
        else:
            mod_role_ids = bot.data_manager.config.get("mod_roles", [])
            if mod_role_ids:
                if any(r.id in mod_role_ids for r in target.roles):
                    is_staff_member = True
            elif target.guild_permissions.moderate_members:
                is_staff_member = True
        
        if not is_staff_member and not cases:
            await interaction.response.send_message(f"{target.mention} is not a staff member and has no recorded history.", ephemeral=True)
            return

        reversals = bot.data_manager.mod_stats.get("reversals", {}).get(uid, 0)
        embed = get_staff_stats_embed(target, cases, reversals)
        
        view = StaffProfileView(target, cases, [], None, embed, interaction.guild)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return

    # Server-wide logic
    await interaction.response.defer(ephemeral=True)
    
    all_records = []
    for records in bot.data_manager.punishments.values():
        all_records.extend(records)
    
    # Basic Counts
    active_cases = sum(1 for record in all_records if is_record_active(record))
    total_issued = bot.data_manager.config.get("stats", {}).get("total_issued", active_cases)
    cases_cleared = bot.data_manager.config.get("stats", {}).get("cases_cleared", 0)
    
    bans = sum(1 for r in all_records if r.get("type") == "ban")
    warns = sum(1 for r in all_records if r.get("type") == "warn")
    timeouts = sum(1 for r in all_records if r.get("type") == "timeout")
    
    # Advanced Stats
    mod_counts = Counter(r.get("moderator") for r in all_records)
    top_mods = mod_counts.most_common(3)
    
    reason_counts = Counter(r.get("reason") for r in all_records)
    top_reasons = reason_counts.most_common(3)
    
    now = discord.utils.utcnow()
    last_24h = sum(1 for r in all_records if (dt := iso_to_dt(r.get("timestamp"))) and dt > now - timedelta(hours=24))
    last_7d = sum(1 for r in all_records if (dt := iso_to_dt(r.get("timestamp"))) and dt > now - timedelta(days=7))

    embed = make_embed(
        "Server Moderation Analytics",
        "> Server-wide moderation totals, recent activity, and staff output trends.",
        kind="analytics",
        scope=SCOPE_ANALYTICS,
        guild=interaction.guild,
        thumbnail=interaction.guild.icon.url if interaction.guild.icon else None,
    )
    
    # Overview
    embed.add_field(name="Lifetime Overview", value=f">>> Total Issued: **{total_issued}**\nCases Cleared: **{cases_cleared}**\nActive Records: **{active_cases}**", inline=False)
    
    # Breakdown
    embed.add_field(name="Action Breakdown", value=f">>> Bans: **{bans}**\nTimeouts: **{timeouts}**\nWarnings: **{warns}**", inline=True)
    embed.add_field(name="Recent Activity", value=f">>> Last 24 Hours: **{last_24h}**\nLast 7 Days: **{last_7d}**", inline=True)
    
    # Top Mods
    if top_mods:
        mod_str = "\n".join([f"<@{m}>: **{c}**" for m, c in top_mods])
        embed.add_field(name="Top Moderators", value=f">>> {mod_str}", inline=True)
    
    # Top Reasons
    if top_reasons:
        reason_str = "\n".join([f"{r}: **{c}**" for r, c in top_reasons])
        embed.add_field(name="Common Violations", value=f">>> {reason_str}", inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="directory", description="Display staff team directory | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def directory(interaction: discord.Interaction):
    conf = bot.data_manager.config
    allowed = {
        conf.get("role_admin", DEFAULT_ROLE_ADMIN),
        conf.get("role_owner", DEFAULT_ROLE_OWNER),
        conf.get("role_community_manager", DEFAULT_ROLE_COMMUNITY_MANAGER)
    }
    if not any(r.id in allowed for r in interaction.user.roles):
        await interaction.response.send_message("**Access Denied:** You do not have the required Admin role.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    admins = []
    mods = []
    mod_role_ids = bot.data_manager.config.get("mod_roles", [])
    
    for member in interaction.guild.members:
        if member.bot: continue
        if member.guild_permissions.administrator:
            admins.append(member)
        elif any(r.id in mod_role_ids for r in member.roles):
            mods.append(member)
        elif not mod_role_ids and member.guild_permissions.moderate_members:
            mods.append(member)
            
    admins.sort(key=lambda m: m.top_role.position, reverse=True)
    mods.sort(key=lambda m: m.top_role.position, reverse=True)
    
    embed = make_embed(
        "Staff Team Directory",
        "> Current configured staff roster for moderation and administrative access.",
        kind="info",
        scope=SCOPE_ANALYTICS,
        guild=interaction.guild,
    )
    
    if admins:
        embed.add_field(name="Administrator", value=">>> " + "\n".join([m.mention for m in admins]), inline=False)
    if mods:
        embed.add_field(name="Moderator", value=">>> " + "\n".join([m.mention for m in mods]), inline=False)
        
    if not admins and not mods:
        embed.description = "> No staff members found."
        
    all_staff = admins + mods
    unique_staff = []
    seen = set()
    for m in all_staff:
        if m.id not in seen:
            unique_staff.append(m)
            seen.add(m.id)
            
    view = StaffView(unique_staff) if unique_staff else None
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@tree.command(name="setup", description="Open the configuration dashboard | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
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

@tree.command(name="publicexecution", description="Start a public vote to ban a user | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def publicexecution(interaction: discord.Interaction, user: discord.User, reaction_count: int):
    await show_punish_menu(interaction, user, public=True, reaction_count=reaction_count)

@tree.command(name="internals", description="View system constants and definitions | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
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
    
    # Mod Commands
    mod_commands = [
        "/mod punish", "/mod history", "/mod active", "/mod undopunish",
        "/mod lock", "/mod unlock", "/mod purge"
    ]
    mod_cmds_fmt = "\n".join(mod_commands)
    embed.add_field(name="Classified Mod Commands", value=f">>> {mod_cmds_fmt}", inline=False)
    
    # Immunity List
    immune_count = len(bot.data_manager.config.get("immunity_list", []))
    embed.add_field(name="Immunity List", value=f"> {immune_count} users immune", inline=False)
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="archive", description="Move this channel to the archive category | admin")
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

@tree.command(name="unarchive", description="Restore this channel from the archives | admin")
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
    
    await interaction.followup.send(f"Channel unarchived and restored.", ephemeral=True)
    
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

@tree.command(name="clone", description="Archive current channel and create a fresh clone | admin")
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

@tree.command(name="rules", description="Configure automated punishment escalation rules | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def rules(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_rules_dashboard_embed(interaction.guild), view=RulesDashboardView(), ephemeral=True)

@tree.command(name="roleadmin", description="Manage custom role permissions | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
@app_commands.choices(action=[
    app_commands.Choice(name="Whitelist", value="whitelist"),
    app_commands.Choice(name="Blacklist", value="blacklist"),
    app_commands.Choice(name="Reset", value="reset"),
    app_commands.Choice(name="List Permissions", value="list_permission"),
    app_commands.Choice(name="List All Roles", value="list_all"),
    app_commands.Choice(name="Manage User Role", value="manage_user")
])
@app_commands.describe(action="Action to perform", target="User or Role (Optional for List)", limit="Max roles (Whitelist only)")
async def role_manage(interaction: discord.Interaction, action: str, target: Optional[Union[discord.Member, discord.Role]] = None, limit: int = 1):
    await interaction.response.defer(ephemeral=True)
    conf = bot.data_manager.config
    
    if action == "list_permission":
        embed = make_embed(
            "Custom Role Permissions",
            "> Current whitelist and blacklist rules for personal role access.",
            kind="info",
            scope=SCOPE_ROLES,
            guild=interaction.guild,
        )
        
        # Whitelisted Users
        wl_users = conf.get("cr_whitelist_users", {})
        if wl_users:
            lines = [f"<@{uid}>: {lim}" for uid, lim in wl_users.items()]
            val = "\n".join(lines)
            if len(val) > 1024: val = val[:1021] + "..."
            embed.add_field(name="Whitelisted Users", value=val, inline=False)
        else:
            embed.add_field(name="Whitelisted Users", value="None", inline=False)

        # Blacklisted Users
        bl_users = conf.get("cr_blacklist_users", [])
        if bl_users:
            lines = [f"<@{uid}>" for uid in bl_users]
            val = ", ".join(lines)
            if len(val) > 1024: val = val[:1021] + "..."
            embed.add_field(name="Blacklisted Users", value=val, inline=False)
        else:
            embed.add_field(name="Blacklisted Users", value="None", inline=False)

        # Whitelisted Roles
        wl_roles = conf.get("cr_whitelist_roles", {})
        if wl_roles:
            lines = [f"<@&{rid}>: {lim}" for rid, lim in wl_roles.items()]
            val = "\n".join(lines)
            if len(val) > 1024: val = val[:1021] + "..."
            embed.add_field(name="Whitelisted Roles", value=val, inline=False)
        else:
            embed.add_field(name="Whitelisted Roles", value="None", inline=False)

        # Blacklisted Roles
        bl_roles = conf.get("cr_blacklist_roles", [])
        if bl_roles:
            lines = [f"<@&{rid}>" for rid in bl_roles]
            val = ", ".join(lines)
            if len(val) > 1024: val = val[:1021] + "..."
            embed.add_field(name="Blacklisted Roles", value=val, inline=False)
        else:
            embed.add_field(name="Blacklisted Roles", value="None", inline=False)
            
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    if action == "list_all":
        # List all custom roles
        embed = make_embed(
            "Server Custom Roles Registry",
            "> Inventory of tracked custom roles and their recorded owners.",
            kind="warning",
            scope=SCOPE_ROLES,
            guild=interaction.guild,
        )
        total_roles = add_custom_role_registry_fields(embed, interaction.guild, field_name="Tracked Roles")
        embed.add_field(name="Total Roles", value=str(total_roles), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    if action == "manage_user":
        if not isinstance(target, discord.Member):
            await interaction.followup.send("Target must be a user.", ephemeral=True)
            return
        
        rec = bot.data_manager.roles.get(str(target.id))
        role = None
        if rec:
            role = interaction.guild.get_role(rec.get("role_id"))
        
        if role:
            embed = build_role_info_embed(target, rec, role, include_tips=True)
            embed.set_footer(text=f"Admin Control Panel for {target.display_name}")
            view = EditView(target, role)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(f"{target.mention} does not have a custom role.", ephemeral=True)
        return

    if target is None:
        await interaction.followup.send("Target is required for this action.", ephemeral=True)
        return

    tid = str(target.id)
    msg = ""

    if action == "whitelist":
        if isinstance(target, discord.Member):
            if "cr_whitelist_users" not in conf: conf["cr_whitelist_users"] = {}
            conf["cr_whitelist_users"][tid] = limit
            if "cr_blacklist_users" in conf and tid in conf["cr_blacklist_users"]:
                conf["cr_blacklist_users"].remove(tid)
            msg = f"Whitelisted user {target.mention} with limit **{limit}**."
        else:
            if "cr_whitelist_roles" not in conf: conf["cr_whitelist_roles"] = {}
            conf["cr_whitelist_roles"][tid] = limit
            if "cr_blacklist_roles" in conf and tid in conf["cr_blacklist_roles"]:
                conf["cr_blacklist_roles"].remove(tid)
            msg = f"Whitelisted role {target.mention} with limit **{limit}**."
    
    elif action == "blacklist":
        if isinstance(target, discord.Member):
            if "cr_blacklist_users" not in conf: conf["cr_blacklist_users"] = []
            if tid not in conf["cr_blacklist_users"]:
                conf["cr_blacklist_users"].append(tid)
            if "cr_whitelist_users" in conf and tid in conf["cr_whitelist_users"]:
                del conf["cr_whitelist_users"][tid]
            msg = f"Blacklisted user {target.mention}."
        else:
            if "cr_blacklist_roles" not in conf: conf["cr_blacklist_roles"] = []
            if tid not in conf["cr_blacklist_roles"]:
                conf["cr_blacklist_roles"].append(tid)
            if "cr_whitelist_roles" in conf and tid in conf["cr_whitelist_roles"]:
                del conf["cr_whitelist_roles"][tid]
            msg = f"Blacklisted role {target.mention}."

    elif action == "reset":
        changes = []
        if isinstance(target, discord.Member):
            if "cr_whitelist_users" in conf and tid in conf["cr_whitelist_users"]:
                del conf["cr_whitelist_users"][tid]
                changes.append("Removed from User Whitelist")
            if "cr_blacklist_users" in conf and tid in conf["cr_blacklist_users"]:
                conf["cr_blacklist_users"].remove(tid)
                changes.append("Removed from User Blacklist")
        else:
            if "cr_whitelist_roles" in conf and tid in conf["cr_whitelist_roles"]:
                del conf["cr_whitelist_roles"][tid]
                changes.append("Removed from Role Whitelist")
            if "cr_blacklist_roles" in conf and tid in conf["cr_blacklist_roles"]:
                conf["cr_blacklist_roles"].remove(tid)
                changes.append("Removed from Role Blacklist")
        
        if changes:
            msg = f"Reset {target.mention}: {', '.join(changes)}"
        else:
            msg = f"{target.mention} was not in any list."

    await bot.data_manager.save_config()
    await interaction.followup.send(msg, ephemeral=True)

@tree.command(name="rolesettings", description="Open the custom role settings panel | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def role_settings(interaction: discord.Interaction):
    embed = build_role_settings_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, view=RoleSettingsView(), ephemeral=True)

@tree.command(name="automod", description="Open the AutoMod control panel | admin")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def automod_cmd(interaction: discord.Interaction):
    if not get_feature_flag(bot.data_manager.config, "automod_panel", True):
        await respond_with_error(interaction, "The AutoMod panel is currently turned off in feature settings.", scope=SCOPE_MODERATION)
        return
    await interaction.response.send_message(embed=build_automod_dashboard_embed(interaction.guild), view=AutoModDashboardView(), ephemeral=True)

@tree.command(name="safetypanel", description="Manage anti-nuke immunity settings | owner")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_owner)
async def safety_panel(interaction: discord.Interaction, key: str):
    if key != "saori":
        await interaction.response.send_message("**Access Denied:** Invalid Security Key.", ephemeral=True)
        return
    
    embed = make_embed(
        "Anti-Nuke Safety Panel",
        "> Manage users who are immune to automated anti-nuke enforcement.",
        kind="warning",
        scope=SCOPE_SYSTEM,
        guild=interaction.guild,
    )
    await interaction.response.send_message(embed=embed, view=SafetyView(), ephemeral=True)

@tree.command(name="access", description="Manage role-based access to moderation tools | owner")
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

@tree.command(name="lockdown", description="Emergency: hide all channels from @everyone | owner")
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

@tree.command(name="unlockdown", description="Restore channel visibility after lockdown | owner")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_owner)
async def unlockdown(interaction: discord.Interaction):
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

@tree.command(name="help", description="Guide for creating and managing custom roles")
async def help_cmd(interaction: discord.Interaction):
    embed = make_embed(
        "Custom Role Guide",
        "> Create, edit, and manage your booster custom role from one reusable control panel.",
        kind="warning",
        scope=SCOPE_ROLES,
        guild=interaction.guild,
    )
    embed.add_field(name="Requirement", value="You must be a server booster to unlock this perk.", inline=False)
    embed.add_field(name="1. Open the Studio", value="Run `/role` to open your personal role dashboard.", inline=False)
    embed.add_field(name="2. Create or Edit", value="Set a name, primary color, icon, and advanced style options.", inline=False)
    embed.add_field(name="3. Reopen Anytime", value="Use `/role` again whenever you want to update or remove your role.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

tree.add_command(ModGroup())

# --- Context Menus (Apps) ---
@tree.context_menu(name="Punish User")
@app_commands.default_permissions(moderate_members=True)
async def punish_context(interaction: discord.Interaction, user: discord.User):
    if not is_staff(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    await show_punish_menu(interaction, user)

@tree.context_menu(name="Mod History")
@app_commands.default_permissions(moderate_members=True)
async def history_context(interaction: discord.Interaction, user: discord.Member):
    if not is_staff(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    await show_history_menu(interaction, user)

# ----------------- Bot Events -----------------
@tree.error
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
    await ctx.send(f"Syncing commands to **{guild.name}**...")
    bot.tree.copy_global_to(guild=guild)
    cmds = await bot.tree.sync(guild=guild)
    await ctx.send(f"Synced {len(cmds)} commands! Check console for list.")
    logger.info(f"Synced commands: {[c.name for c in cmds]}")

@tree.command(name="status", description="View bot latency and uptime | mod")
@app_commands.default_permissions(moderate_members=True)
async def status_cmd(interaction: discord.Interaction):
    if not is_staff(interaction):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    embed = build_status_embed(interaction.guild)
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
        is_staff_member = False
        if any(r.id in mod_roles_ids for r in message.author.roles):
            is_staff_member = True
        elif message.author.guild_permissions.administrator:
            is_staff_member = True
            
        if is_staff_member:
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

                files = []
                for att in message.attachments:
                    files.append(await att.to_file())

                try:
                    await thread.send(embed=embed, files=files)
                    ticket["last_user_message_at"] = now_iso()
                    ticket["last_sla_alert_at"] = None
                    await bot.data_manager.save_modmail()
                    if guild:
                        await refresh_modmail_ticket_log(guild, str(message.author.id))
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
                    
                    files = []
                    for att in message.attachments:
                        files.append(await att.to_file())
                        
                    await user.send(embed=embed, files=files)
                    ticket["last_staff_message_at"] = now_iso()
                    await bot.data_manager.save_modmail()
                    await refresh_modmail_ticket_log(message.guild, target_uid)
                    await message.add_reaction("📨")
                except discord.Forbidden:
                    await message.channel.send("❌ Failed to send: User has blocked the bot or DMs are disabled.")
                except Exception as e:
                    await message.channel.send(f"❌ Failed to send message: {e}")
            return

    await bot.process_commands(message)

@bot.event
async def on_ready():
    bot.start_time = time.time()
    logger.info(f"[READY] Logged in as {bot.user} (ID: {bot.user.id}). System operational.")

bot.run(TOKEN)
