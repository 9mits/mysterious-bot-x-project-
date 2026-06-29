"""
mbx_data.py — DataManager, AntiAbuseSystem, path constants, and low-level I/O helpers.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
import discord

from core.constants import (
    DEFAULT_GUILD_ID,
    DEFAULT_MAX_UNREAD_PINGS,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ANCHOR_ROLE_ID,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    DEFAULT_ROLE_OWNER,
    DEFAULT_RULES,
    DEFAULT_SPAM_ROLE_ID,
    DEFAULT_ARCHIVE_CAT_ID,
    TOKEN_ENV_VARS,
)
from core.services import (
    DEFAULT_CANNED_REPLIES,
    DEFAULT_NATIVE_AUTOMOD_SETTINGS,
    DEFAULT_SCHEMA_VERSION,
    normalize_case_record,
    run_schema_migrations,
)

logger = logging.getLogger("MGXBot")

# ----------------- PATHS -----------------
# BOT_DATA_DIR can be set per-instance in .env to keep databases separate.
# Defaults to the classic "database/" folder so existing installs are unaffected.
BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = Path(os.environ.get("BOT_DATA_DIR", str(BASE_DIR / "database")))
ROLES_FILE = DB_DIR / "roles.json"
CONFIG_FILE = DB_DIR / "config.json"
PUNISHMENTS_FILE = DB_DIR / "punishments.json"
MOD_STATS_FILE = DB_DIR / "mod_stats.json"
PINGS_FILE = DB_DIR / "pings.json"
LOCKDOWN_FILE = DB_DIR / "lockdown.json"
MODMAIL_FILE = DB_DIR / "modmail.json"
DB_FILE = DB_DIR / "bot.db"
# -----------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS punishments (
    case_id    INTEGER PRIMARY KEY,
    user_id    TEXT    NOT NULL,
    data       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_punishments_user ON punishments(user_id);

CREATE TABLE IF NOT EXISTS roles (
    user_id TEXT PRIMARY KEY,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mod_stats (
    user_id TEXT PRIMARY KEY,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pings (
    user_id TEXT PRIMARY KEY,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS modmail (
    user_id TEXT PRIMARY KEY,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lockdown (
    channel_id TEXT PRIMARY KEY,
    data       TEXT NOT NULL
);
"""


def read_json_file(path: Path, default: Any) -> Any:
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


# ----------------- Storage helpers -----------------
class DataManager:
    def __init__(self, bot):
        self.bot = bot
        self.config: dict = {}
        self.roles: dict = {}
        self.punishments: dict = {}
        self.case_index: Dict[int, Tuple[str, dict]] = {}
        self.mod_stats: dict = {}
        self.pings: dict = {}
        self.modmail: dict = {}
        self.modmail_threads: Dict[int, str] = {}
        self.lockdown: dict = {}

        self._dirty_config = False
        self._dirty_roles = False
        self._dirty_punishments = False
        self._dirty_stats = False
        self._dirty_pings = False
        self._dirty_modmail = False
        self._dirty_lockdown = False
        self._save_lock = asyncio.Lock()
        self._db: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Internal: legacy JSON helpers (kept for migration and resolve_bot_token)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Internal: SQLite helpers
    # ------------------------------------------------------------------

    async def _open_db(self) -> aiosqlite.Connection:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(DB_FILE)
        db.row_factory = aiosqlite.Row
        # WAL keeps reads non-blocking during writes; NORMAL avoids an fsync
        # on every commit (durable enough for a bot, much faster on writes).
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.executescript(_CREATE_TABLES_SQL)
        await self._migrate_schema_columns(db)
        await db.commit()
        return db

    async def _migrate_schema_columns(self, db: aiosqlite.Connection):
        """Idempotent column renames for databases created before a rename.

        The roles table is keyed by user_id but the column was historically
        misnamed `role_id`; rename it in place if an old DB still has it.
        """
        async with db.execute("PRAGMA table_info(roles)") as cursor:
            columns = {row["name"] async for row in cursor}
        if "role_id" in columns and "user_id" not in columns:
            await db.execute("ALTER TABLE roles RENAME COLUMN role_id TO user_id")
            logger.info("Migration: renamed roles.role_id column to user_id")

    async def _db_conn(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await self._open_db()
        return self._db

    # ------------------------------------------------------------------
    # Internal: migration from JSON files → SQLite
    # ------------------------------------------------------------------

    async def _migrate_json_to_db(self, db: aiosqlite.Connection):
        """Import legacy JSON files into SQLite once, then rename them to .bak."""

        # config.json → config table (one row per key)
        if CONFIG_FILE.exists():
            try:
                raw = read_json_file(CONFIG_FILE, {})
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        await db.execute(
                            "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)",
                            (k, json.dumps(v)),
                        )
                    logger.info("Migration: imported config.json into SQLite")
                CONFIG_FILE.rename(CONFIG_FILE.with_suffix(".json.bak"))
            except Exception as exc:
                logger.warning("Migration: failed to import config.json: %s", exc)

        # punishments.json → punishments table
        if PUNISHMENTS_FILE.exists():
            try:
                raw = read_json_file(PUNISHMENTS_FILE, {})
                if isinstance(raw, dict):
                    for user_id, records in raw.items():
                        if not isinstance(records, list):
                            continue
                        for record in records:
                            if not isinstance(record, dict):
                                continue
                            case_id = record.get("case_id")
                            if not isinstance(case_id, int) or case_id <= 0:
                                continue
                            await db.execute(
                                "INSERT OR IGNORE INTO punishments(case_id, user_id, data) VALUES (?, ?, ?)",
                                (case_id, str(user_id), json.dumps(record)),
                            )
                    logger.info("Migration: imported punishments.json into SQLite")
                PUNISHMENTS_FILE.rename(PUNISHMENTS_FILE.with_suffix(".json.bak"))
            except Exception as exc:
                logger.warning("Migration: failed to import punishments.json: %s", exc)

        # roles.json → roles table
        if ROLES_FILE.exists():
            try:
                raw = read_json_file(ROLES_FILE, {})
                if isinstance(raw, dict):
                    for user_id, data in raw.items():
                        await db.execute(
                            "INSERT OR IGNORE INTO roles(user_id, data) VALUES (?, ?)",
                            (str(user_id), json.dumps(data)),
                        )
                    logger.info("Migration: imported roles.json into SQLite")
                ROLES_FILE.rename(ROLES_FILE.with_suffix(".json.bak"))
            except Exception as exc:
                logger.warning("Migration: failed to import roles.json: %s", exc)

        # mod_stats.json → mod_stats table
        if MOD_STATS_FILE.exists():
            try:
                raw = read_json_file(MOD_STATS_FILE, {})
                if isinstance(raw, dict):
                    for user_id, data in raw.items():
                        await db.execute(
                            "INSERT OR IGNORE INTO mod_stats(user_id, data) VALUES (?, ?)",
                            (str(user_id), json.dumps(data)),
                        )
                    logger.info("Migration: imported mod_stats.json into SQLite")
                MOD_STATS_FILE.rename(MOD_STATS_FILE.with_suffix(".json.bak"))
            except Exception as exc:
                logger.warning("Migration: failed to import mod_stats.json: %s", exc)

        # pings.json → pings table
        if PINGS_FILE.exists():
            try:
                raw = read_json_file(PINGS_FILE, {})
                if isinstance(raw, dict):
                    for user_id, data in raw.items():
                        await db.execute(
                            "INSERT OR IGNORE INTO pings(user_id, data) VALUES (?, ?)",
                            (str(user_id), json.dumps(data)),
                        )
                    logger.info("Migration: imported pings.json into SQLite")
                PINGS_FILE.rename(PINGS_FILE.with_suffix(".json.bak"))
            except Exception as exc:
                logger.warning("Migration: failed to import pings.json: %s", exc)

        # modmail.json → modmail table
        if MODMAIL_FILE.exists():
            try:
                raw = read_json_file(MODMAIL_FILE, {})
                if isinstance(raw, dict):
                    for user_id, data in raw.items():
                        await db.execute(
                            "INSERT OR IGNORE INTO modmail(user_id, data) VALUES (?, ?)",
                            (str(user_id), json.dumps(data)),
                        )
                    logger.info("Migration: imported modmail.json into SQLite")
                MODMAIL_FILE.rename(MODMAIL_FILE.with_suffix(".json.bak"))
            except Exception as exc:
                logger.warning("Migration: failed to import modmail.json: %s", exc)

        # lockdown.json → lockdown table
        if LOCKDOWN_FILE.exists():
            try:
                raw = read_json_file(LOCKDOWN_FILE, {})
                if isinstance(raw, dict):
                    for channel_id, data in raw.items():
                        await db.execute(
                            "INSERT OR IGNORE INTO lockdown(channel_id, data) VALUES (?, ?)",
                            (str(channel_id), json.dumps(data)),
                        )
                    logger.info("Migration: imported lockdown.json into SQLite")
                LOCKDOWN_FILE.rename(LOCKDOWN_FILE.with_suffix(".json.bak"))
            except Exception as exc:
                logger.warning("Migration: failed to import lockdown.json: %s", exc)

        await db.commit()

    # ------------------------------------------------------------------
    # Internal: in-memory helpers (unchanged from original)
    # ------------------------------------------------------------------

    def _normalize_positive_int(self, value: Any, default: int, *, minimum: int = 1, maximum: Optional[int] = None) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = default
        if maximum is not None:
            normalized = min(normalized, maximum)
        return max(minimum, normalized)

    def _parse_optional_int(self, value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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

    # ------------------------------------------------------------------
    # Internal: load sections from SQLite into memory
    # ------------------------------------------------------------------

    async def _load_config_from_db(self, db: aiosqlite.Connection) -> dict:
        config = {}
        async with db.execute("SELECT key, value FROM config") as cursor:
            async for row in cursor:
                try:
                    config[row["key"]] = json.loads(row["value"])
                except Exception:
                    config[row["key"]] = row["value"]
        return config

    async def _load_punishments_from_db(self, db: aiosqlite.Connection) -> dict:
        punishments: dict = {}
        async with db.execute("SELECT user_id, data FROM punishments") as cursor:
            async for row in cursor:
                try:
                    record = json.loads(row["data"])
                except Exception:
                    continue
                uid = row["user_id"]
                punishments.setdefault(uid, []).append(record)
        return punishments

    async def _load_simple_dict_from_db(self, db: aiosqlite.Connection, table: str, key_col: str) -> dict:
        result = {}
        async with db.execute(f"SELECT {key_col}, data FROM {table}") as cursor:
            async for row in cursor:
                try:
                    result[row[key_col]] = json.loads(row["data"])
                except Exception:
                    continue
        return result

    # ------------------------------------------------------------------
    # Internal: write sections from memory → SQLite
    # ------------------------------------------------------------------

    async def _save_config_to_db(self, db: aiosqlite.Connection):
        await db.execute("DELETE FROM config")
        rows = [(k, json.dumps(v)) for k, v in self.config.items()]
        if rows:
            await db.executemany("INSERT INTO config(key, value) VALUES (?, ?)", rows)

    async def _save_roles_to_db(self, db: aiosqlite.Connection):
        await db.execute("DELETE FROM roles")
        rows = [(str(user_id), json.dumps(data)) for user_id, data in self.roles.items()]
        if rows:
            await db.executemany("INSERT INTO roles(user_id, data) VALUES (?, ?)", rows)

    async def _save_punishments_to_db(self, db: aiosqlite.Connection):
        await db.execute("DELETE FROM punishments")
        rows = []
        for user_id, records in self.punishments.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                case_id = record.get("case_id")
                if not isinstance(case_id, int) or case_id <= 0:
                    continue
                rows.append((case_id, str(user_id), json.dumps(record)))
        if rows:
            await db.executemany(
                "INSERT OR REPLACE INTO punishments(case_id, user_id, data) VALUES (?, ?, ?)",
                rows,
            )

    async def _save_mod_stats_to_db(self, db: aiosqlite.Connection):
        await db.execute("DELETE FROM mod_stats")
        rows = [(str(user_id), json.dumps(data)) for user_id, data in self.mod_stats.items()]
        if rows:
            await db.executemany("INSERT INTO mod_stats(user_id, data) VALUES (?, ?)", rows)

    async def _save_pings_to_db(self, db: aiosqlite.Connection):
        await db.execute("DELETE FROM pings")
        rows = [(str(user_id), json.dumps(data)) for user_id, data in self.pings.items()]
        if rows:
            await db.executemany("INSERT INTO pings(user_id, data) VALUES (?, ?)", rows)

    async def _save_modmail_to_db(self, db: aiosqlite.Connection):
        await db.execute("DELETE FROM modmail")
        rows = [(str(user_id), json.dumps(data)) for user_id, data in self.modmail.items()]
        if rows:
            await db.executemany("INSERT INTO modmail(user_id, data) VALUES (?, ?)", rows)

    async def _save_lockdown_to_db(self, db: aiosqlite.Connection):
        await db.execute("DELETE FROM lockdown")
        rows = [(str(channel_id), json.dumps(data)) for channel_id, data in self.lockdown.items()]
        if rows:
            await db.executemany("INSERT INTO lockdown(channel_id, data) VALUES (?, ?)", rows)

    # ------------------------------------------------------------------
    # Public: load / save
    # ------------------------------------------------------------------

    async def load_all(self):
        db = await self._open_db()
        self._db = db

        # Run JSON → SQLite migration for any legacy files that still exist
        any_legacy = any(
            p.exists()
            for p in (
                CONFIG_FILE, PUNISHMENTS_FILE, ROLES_FILE, MOD_STATS_FILE,
                PINGS_FILE, MODMAIL_FILE, LOCKDOWN_FILE,
            )
        )
        if any_legacy:
            await self._migrate_json_to_db(db)

        # ---- config ----
        self.config = await self._load_config_from_db(db)

        had_general_log_channel = "general_log_channel_id" in self.config
        legacy_log_channel_id = self.config.get("log_channel_id")

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
            "max_unread_pings_per_user": DEFAULT_MAX_UNREAD_PINGS,
            "feature_flags": {},
            "modmail_canned_replies": DEFAULT_CANNED_REPLIES,
            "modmail_sla_minutes": 60,
            "dm_modmail_panel_cooldown_minutes": 30,
            "escalation_matrix": [],
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
            "role_mention_spam_target": DEFAULT_SPAM_ROLE_ID,
        }
        for k, v in defaults.items():
            if k not in self.config:
                self.config[k] = copy.deepcopy(v)
                self._dirty_config = True

        if not had_general_log_channel and legacy_log_channel_id:
            self.config["general_log_channel_id"] = legacy_log_channel_id
            self._dirty_config = True

        # ---- other sections ----
        raw_roles = await self._load_simple_dict_from_db(db, "roles", "user_id")
        # Migrate single-dict entries to lists
        self.roles = {
            uid: (v if isinstance(v, list) else [v])
            for uid, v in raw_roles.items()
        }
        self.punishments = await self._load_punishments_from_db(db)
        self._normalize_punishments()
        self.mod_stats = await self._load_simple_dict_from_db(db, "mod_stats", "user_id")
        self.pings = await self._load_simple_dict_from_db(db, "pings", "user_id")
        self.modmail = await self._load_simple_dict_from_db(db, "modmail", "user_id")

        migrated, migration_notes = run_schema_migrations(self.config, self.punishments, self.modmail)
        if migrated:
            self._dirty_config = True
            self._dirty_punishments = True
            self._dirty_modmail = True
            for note in migration_notes:
                logger.info("Migration: %s", note)

        self.lockdown = await self._load_simple_dict_from_db(db, "lockdown", "channel_id")
        self._rebuild_case_index()
        self._rebuild_modmail_index()

        # Flush any defaults / migrations written during load
        if any(
            [
                self._dirty_config,
                self._dirty_roles,
                self._dirty_punishments,
                self._dirty_stats,
                self._dirty_pings,
                self._dirty_modmail,
                self._dirty_lockdown,
            ]
        ):
            await self.save_all(force=False)

    async def save_all(self, force=False):
        async with self._save_lock:
            db = await self._db_conn()
            needs_commit = False

            if self._dirty_config or force:
                await self._save_config_to_db(db)
                self._dirty_config = False
                needs_commit = True

            if self._dirty_roles or force:
                await self._save_roles_to_db(db)
                self._dirty_roles = False
                needs_commit = True

            if self._dirty_punishments or force:
                self._rebuild_case_index()
                await self._save_punishments_to_db(db)
                self._dirty_punishments = False
                needs_commit = True

            if self._dirty_stats or force:
                await self._save_mod_stats_to_db(db)
                self._dirty_stats = False
                needs_commit = True

            if self._dirty_pings or force:
                await self._save_pings_to_db(db)
                self._dirty_pings = False
                needs_commit = True

            if self._dirty_modmail or force:
                self._rebuild_modmail_index()
                await self._save_modmail_to_db(db)
                self._dirty_modmail = False
                needs_commit = True

            if self._dirty_lockdown or force:
                await self._save_lockdown_to_db(db)
                self._dirty_lockdown = False
                needs_commit = True

            if needs_commit:
                await db.commit()

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

    def get_all_cases(self) -> List[Tuple[str, dict]]:
        """Every case on record across all users as (user_id, record) pairs,
        sorted by case id descending (newest first)."""
        cases: List[Tuple[str, dict]] = []
        for user_id, records in self.punishments.items():
            for record in records:
                if isinstance(record, dict):
                    cases.append((user_id, record))
        cases.sort(key=lambda item: item[1].get("case_id", 0), reverse=True)
        return cases

    def allocate_case_id(self) -> int:
        current = self._normalize_positive_int(self.config.get("case_counter", 0), 0, minimum=0)
        next_case_id = current + 1
        self.config["case_counter"] = next_case_id
        self._dirty_config = True
        return next_case_id

    def prepare_punishment_record(self, record: dict) -> dict:
        from core.utils import now_iso
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
                        issued_at = parse_iso_datetime(record.get("timestamp"))
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


# ----------------- Security -----------------
class AntiAbuseSystem:
    def __init__(self):
        self._tracker = defaultdict(lambda: deque(maxlen=15))
        self.cooldowns: Dict[str, float] = {}
        self.mention_spam_tracker = defaultdict(lambda: deque(maxlen=10))
        self.smart_automod_tracker = defaultdict(lambda: deque(maxlen=8))

    def check_rate_limit(self, user_id: int, config: dict) -> bool:
        now = time.time()
        limit = config.get("security", {}).get("max_actions_per_min", 10)
        while self._tracker[user_id] and now - self._tracker[user_id][0] > 60:
            self._tracker[user_id].popleft()
        self._tracker[user_id].append(now)
        return len(self._tracker[user_id]) > limit
