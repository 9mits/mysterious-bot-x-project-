"""Cross-instance fleet stats via shared snapshot files.

Each bot instance serves a single guild and runs as its own process with its own
database, so no instance can see a sibling's live numbers (member_count in
particular lives in the gateway cache, not the DB). To answer "how big is the
whole project?" each instance periodically writes a small JSON snapshot of its own
stats into a shared ``project_stats/`` folder; ``/about`` reads every snapshot and
sums them. On the panel bot1 and bot2 share a working directory, so a plain shared
folder is enough — no IPC or cross-process DB locking.

This module imports only stdlib + discord; it must not import from ``cogs/``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.constants import BRAND_NAME, DEFAULT_GUILD_ID
from core.utils import now_iso

logger = logging.getLogger("MGXBot")

# Shared across instances on the same machine; overridable per deployment.
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_STATS_DIR = Path(os.environ.get("PROJECT_STATS_DIR", str(BASE_DIR / "project_stats")))

# A snapshot older than this is treated as a possibly-offline instance: still
# counted in totals (last-known numbers), but flagged in the per-server list.
STALE_AFTER_SECONDS = 15 * 60


def build_snapshot(bot) -> Optional[dict]:
    """Snapshot this instance's live stats, or None if it isn't ready yet."""
    data_manager = getattr(bot, "data_manager", None)
    if data_manager is None or bot.user is None:
        return None

    config = data_manager.config
    guild = bot.get_guild(int(config.get("guild_id", DEFAULT_GUILD_ID) or 0))
    if guild is None:
        guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return None

    all_records = [r for records in data_manager.punishments.values() for r in records]
    bans = sum(1 for r in all_records if r.get("type") == "ban")
    timeouts = sum(1 for r in all_records if r.get("type") == "timeout")
    warns = sum(1 for r in all_records if r.get("type") == "warn")
    active_cases = sum(1 for r in all_records if r.get("active"))

    config_stats = config.get("stats", {}) or {}
    custom_roles = sum(
        len(v) if isinstance(v, list) else 1 for v in data_manager.roles.values()
    )
    open_modmail = sum(
        1 for t in data_manager.modmail.values()
        if isinstance(t, dict) and t.get("status") == "open"
    )

    return {
        "bot_user_id": bot.user.id,
        "brand_name": BRAND_NAME,
        "guild_id": guild.id,
        "guild_name": guild.name,
        "guild_icon_url": guild.icon.url if guild.icon else None,
        "member_count": guild.member_count or 0,
        "stats": {
            "total_cases": len(all_records),
            "bans": bans,
            "timeouts": timeouts,
            "warns": warns,
            "active_cases": active_cases,
            "total_issued": int(config_stats.get("total_issued", 0) or 0),
            "cases_cleared": int(config_stats.get("cases_cleared", 0) or 0),
            "custom_roles": custom_roles,
            "open_modmail": open_modmail,
        },
        "started_at": datetime.fromtimestamp(
            getattr(bot, "start_time", 0) or 0, timezone.utc
        ).isoformat(),
        "updated_at": now_iso(),
    }


def _write_snapshot_sync(snapshot: dict) -> None:
    PROJECT_STATS_DIR.mkdir(parents=True, exist_ok=True)
    path = PROJECT_STATS_DIR / f"{snapshot['bot_user_id']}.json"
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=PROJECT_STATS_DIR, delete=False,
        ) as temp_file:
            json.dump(snapshot, temp_file, ensure_ascii=False)
            temp_name = temp_file.name
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass


async def write_snapshot(bot) -> None:
    """Build and atomically persist this instance's snapshot (no-op if not ready)."""
    snapshot = build_snapshot(bot)
    if snapshot is None:
        return
    try:
        await asyncio.to_thread(_write_snapshot_sync, snapshot)
    except Exception as exc:
        logger.warning("Failed to write project stats snapshot: %s", exc)


def read_all_snapshots() -> List[dict]:
    """Read every snapshot in the shared folder, skipping missing/corrupt ones."""
    if not PROJECT_STATS_DIR.exists():
        return []
    snapshots = []
    for path in PROJECT_STATS_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict) and "stats" in data:
                snapshots.append(data)
        except Exception:
            continue
    return snapshots


def _snapshot_age_seconds(snapshot: dict, now: Optional[datetime] = None) -> Optional[float]:
    raw = snapshot.get("updated_at")
    if not isinstance(raw, str):
        return None
    try:
        updated = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return (reference - updated).total_seconds()


def is_stale(snapshot: dict, now: Optional[datetime] = None) -> bool:
    age = _snapshot_age_seconds(snapshot, now)
    return age is not None and age > STALE_AFTER_SECONDS


def aggregate_snapshots(snapshots: List[dict]) -> Dict[str, Any]:
    """Sum fleet-wide totals across snapshots (pure; no Discord/IO)."""
    totals = {
        "total_cases": 0, "bans": 0, "timeouts": 0, "warns": 0,
        "active_cases": 0, "total_issued": 0, "cases_cleared": 0,
        "custom_roles": 0, "open_modmail": 0,
    }
    total_members = 0
    for snap in snapshots:
        total_members += int(snap.get("member_count", 0) or 0)
        stats = snap.get("stats", {}) or {}
        for key in totals:
            totals[key] += int(stats.get(key, 0) or 0)

    servers = sorted(
        snapshots,
        key=lambda s: int(s.get("member_count", 0) or 0),
        reverse=True,
    )
    return {
        "server_count": len(snapshots),
        "total_members": total_members,
        "totals": totals,
        "servers": servers,
    }
