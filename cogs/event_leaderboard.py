"""
event_leaderboard.py — Limited-time VC time leaderboard for a single server.

Cross-instance design (see cogs/registry.py for the broader picture):

  CONTROL half  — activated by env TEST_MODE=1 (the test bot).
      Registers the /event command group. Writes event_config.json only.
      Never posts or edits the leaderboard message itself.

  DISPLAY half  — activated by env EVENT_DISPLAY=1 (e.g. bot2, the public instance).
      Tracks voice time, owns the leaderboard message, edits it every
      EVENT_REFRESH_SECONDS. Writes event_runtime.json only.

Exactly ONE instance should have EVENT_DISPLAY=1 or voice time is double-counted.
The two JSON files have a single writer each, so the instances never race.

State files live at <project root>/event_data/ which is shared across all
instances (they all run from the same checkout), independent of BOT_DATA_DIR.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.constants import SCOPE_SYSTEM
from core.utils import create_progress_bar, now_iso
from .shared import make_embed

# How often the display instance edits the leaderboard message.
# Editing one message every 60s is far below Discord's rate limits; lower it
# if you want snappier updates (do not go below ~10s).
EVENT_REFRESH_SECONDS = 60

DEFAULT_GOAL_HOURS = 1000
DEFAULT_TITLE = "1000 Hour VC Event"

# ---------------------------------------------------------------------------
# Shared state files (single writer each)
# ---------------------------------------------------------------------------
EVENT_DIR = Path(__file__).resolve().parent.parent / "event_data"
CONFIG_FILE = EVENT_DIR / "event_config.json"     # writer: control (test bot)
RUNTIME_FILE = EVENT_DIR / "event_runtime.json"   # writer: display (bot2)

_DEFAULT_CONFIG: Dict[str, Any] = {
    "active": False,
    "guild_id": None,
    "channel_id": None,
    "goal_hours": DEFAULT_GOAL_HOURS,
    "title": DEFAULT_TITLE,
    "started_at": None,
    "reset_token": 0,
}

_DEFAULT_RUNTIME: Dict[str, Any] = {
    "message_id": None,
    "totals": {},            # {user_id(str): seconds(int)}
    "last_updated": None,
    "applied_reset_token": 0,
}


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        merged = dict(default)
        merged.update(data)
        return merged
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(default)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(EVENT_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_config() -> Dict[str, Any]:
    return _read_json(CONFIG_FILE, _DEFAULT_CONFIG)


def save_config(cfg: Dict[str, Any]) -> None:
    _write_json(CONFIG_FILE, cfg)


def load_runtime() -> Dict[str, Any]:
    return _read_json(RUNTIME_FILE, _DEFAULT_RUNTIME)


def save_runtime(rt: Dict[str, Any]) -> None:
    _write_json(RUNTIME_FILE, rt)


def format_vc_time(seconds: int) -> str:
    """Render a VC duration as e.g. '5d 3h 12m' / '3h 12m' / '12m' / '45s'."""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# CONTROL half — /event command group (registered only when TEST_MODE=1)
# ---------------------------------------------------------------------------
event_group = app_commands.Group(
    name="event",
    description="Control the limited-time VC leaderboard event.",
    default_permissions=discord.Permissions(administrator=True),
)


def _control_embed(title: str, description: str, kind: str = "info", guild: Optional[discord.Guild] = None) -> discord.Embed:
    return make_embed(title, description, kind=kind, scope=SCOPE_SYSTEM, guild=guild)


@event_group.command(name="setup", description="Set the channel where the leaderboard message will live.")
@app_commands.describe(channel="The channel the display bot will post/edit the leaderboard in.")
async def event_setup(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    cfg = load_config()
    cfg["guild_id"] = interaction.guild_id
    cfg["channel_id"] = channel.id
    save_config(cfg)
    await interaction.response.send_message(
        embed=_control_embed(
            "Event Channel Set",
            f"> Leaderboard will be posted in {channel.mention} by the display instance.\n"
            f"> Run `/event start` to begin tracking. (The message is created by the display bot, not me.)",
            kind="success",
            guild=interaction.guild,
        ),
        ephemeral=True,
    )


@event_group.command(name="start", description="Start (or resume) tracking VC time for the event.")
async def event_start(interaction: discord.Interaction) -> None:
    cfg = load_config()
    if not cfg.get("channel_id"):
        await interaction.response.send_message(
            embed=_control_embed("No Channel", "> Run `/event setup` first to pick a channel.", kind="error", guild=interaction.guild),
            ephemeral=True,
        )
        return
    cfg["active"] = True
    if not cfg.get("started_at"):
        cfg["started_at"] = now_iso()
    save_config(cfg)
    await interaction.response.send_message(
        embed=_control_embed(
            "Event Started",
            f"> Now tracking voice time. The display instance refreshes the board every {EVENT_REFRESH_SECONDS}s.",
            kind="success",
            guild=interaction.guild,
        ),
        ephemeral=True,
    )


@event_group.command(name="stop", description="Pause/stop tracking. The leaderboard freezes at current standings.")
async def event_stop(interaction: discord.Interaction) -> None:
    cfg = load_config()
    cfg["active"] = False
    save_config(cfg)
    await interaction.response.send_message(
        embed=_control_embed("Event Stopped", "> Tracking paused. The leaderboard message will stop updating.", kind="warning", guild=interaction.guild),
        ephemeral=True,
    )


@event_group.command(name="reset", description="Wipe all tracked VC time back to zero.")
async def event_reset(interaction: discord.Interaction) -> None:
    cfg = load_config()
    cfg["reset_token"] = int(cfg.get("reset_token", 0)) + 1
    cfg["started_at"] = now_iso() if cfg.get("active") else None
    save_config(cfg)
    await interaction.response.send_message(
        embed=_control_embed("Event Reset", "> All tracked time will be wiped on the display instance's next refresh.", kind="danger", guild=interaction.guild),
        ephemeral=True,
    )


@event_group.command(name="goal", description="Set the event goal in hours (default 1000).")
@app_commands.describe(hours="Target combined hours for the progress bar.")
async def event_goal(interaction: discord.Interaction, hours: app_commands.Range[int, 1, 1000000]) -> None:
    cfg = load_config()
    cfg["goal_hours"] = int(hours)
    save_config(cfg)
    await interaction.response.send_message(
        embed=_control_embed("Goal Updated", f"> Event goal set to **{hours:,} hours**.", kind="success", guild=interaction.guild),
        ephemeral=True,
    )


@event_group.command(name="status", description="Show the current event configuration and runtime state.")
async def event_status(interaction: discord.Interaction) -> None:
    cfg = load_config()
    rt = load_runtime()
    totals = rt.get("totals", {})
    channel_id = cfg.get("channel_id")
    message_id = rt.get("message_id")
    guild_id = cfg.get("guild_id")

    lines = [
        f"> **Active:** {'Yes' if cfg.get('active') else 'No'}",
        f"> **Channel:** {f'<#{channel_id}>' if channel_id else 'Not set'}",
        f"> **Goal:** {int(cfg.get('goal_hours', DEFAULT_GOAL_HOURS)):,} hours",
        f"> **Participants tracked:** {len(totals)}",
        f"> **Last refresh:** {rt.get('last_updated') or 'never'}",
    ]
    if guild_id and channel_id and message_id:
        lines.append(f"> **Message:** https://discord.com/channels/{guild_id}/{channel_id}/{message_id}")
    else:
        lines.append("> **Message:** not posted yet (waiting on display instance)")

    await interaction.response.send_message(
        embed=_control_embed("Event Status", "\n".join(lines), kind="info", guild=interaction.guild),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# DISPLAY half — voice tracking + refresh loop (only when EVENT_DISPLAY=1)
# ---------------------------------------------------------------------------
class EventLeaderboardCog(commands.Cog):
    """Tracks VC time and edits the leaderboard message on the display instance."""

    def __init__(self, bot_instance: commands.Bot) -> None:
        self.bot = bot_instance
        self._sessions: Dict[int, float] = {}      # user_id -> epoch join time
        self._totals: Dict[str, int] = {}          # user_id(str) -> seconds
        self._active = False
        self._guild_id: Optional[int] = None
        self._channel_id: Optional[int] = None
        self._goal_hours = DEFAULT_GOAL_HOURS
        self._title = DEFAULT_TITLE
        self._message_id: Optional[int] = None
        self._applied_reset_token = 0
        self._loaded = False

    # -- helpers ----------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        rt = load_runtime()
        self._totals = {str(k): int(v) for k, v in rt.get("totals", {}).items()}
        self._message_id = rt.get("message_id")
        self._applied_reset_token = int(rt.get("applied_reset_token", 0))
        self._loaded = True

    def _is_tracked_channel(self, channel: Optional[discord.VoiceChannel], guild: Optional[discord.Guild]) -> bool:
        if channel is None or guild is None:
            return False
        if guild.afk_channel and channel.id == guild.afk_channel.id:
            return False
        return True

    def _flush_sessions(self) -> None:
        """Move elapsed time from open sessions into totals (only while active)."""
        now = time.time()
        for uid, started in list(self._sessions.items()):
            elapsed = now - started
            self._sessions[uid] = now
            if self._active and elapsed > 0:
                self._totals[str(uid)] = self._totals.get(str(uid), 0) + int(elapsed)

    def _sync_sessions(self, guild: discord.Guild) -> None:
        """Open sessions for everyone currently in a tracked VC (restart-safe)."""
        now = time.time()
        present: set[int] = set()
        for vc in guild.voice_channels:
            if not self._is_tracked_channel(vc, guild):
                continue
            for member in vc.members:
                if member.bot:
                    continue
                present.add(member.id)
                self._sessions.setdefault(member.id, now)
        # Drop sessions for people no longer present (e.g. missed events)
        for uid in list(self._sessions):
            if uid not in present:
                del self._sessions[uid]

    def _persist(self) -> None:
        save_runtime({
            "message_id": self._message_id,
            "totals": self._totals,
            "last_updated": now_iso(),
            "applied_reset_token": self._applied_reset_token,
        })

    def _build_embed(self, guild: discord.Guild) -> discord.Embed:
        ranked = sorted(self._totals.items(), key=lambda kv: kv[1], reverse=True)
        top = ranked[:10]

        if top:
            lines = []
            for idx, (uid, seconds) in enumerate(top):
                lines.append(f"`#{idx + 1}` <@{uid}> — **{format_vc_time(seconds)}**")
            board = "\n".join(lines)
        else:
            board = "*No voice time tracked yet. Join a voice channel to get on the board.*"

        total_seconds = sum(self._totals.values())
        total_hours = total_seconds / 3600
        goal = max(1, int(self._goal_hours))
        pct = total_hours / goal
        bar = create_progress_bar(pct, 20)

        description = (
            f"{bar}\n"
            f"**{total_hours:,.1f}** / {goal:,} hours combined ({min(100, pct * 100):.1f}%)\n\n"
            f"{board}"
        )

        embed = make_embed(self._title, description, kind="info", scope=SCOPE_SYSTEM, guild=guild)
        status = "Live" if self._active else "Paused"
        embed.add_field(name="Status", value=f"**{status}**", inline=True)
        embed.add_field(name="Participants", value=str(len(self._totals)), inline=True)
        embed.add_field(name="Refresh", value=f"every {EVENT_REFRESH_SECONDS}s", inline=True)
        return embed

    async def _update_message(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        embed = self._build_embed(guild)
        if self._message_id:
            try:
                msg = await channel.fetch_message(self._message_id)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                self._message_id = None  # message deleted — repost below
            except discord.HTTPException:
                return  # transient; try again next tick
        try:
            msg = await channel.send(embed=embed)
            self._message_id = msg.id
        except discord.HTTPException:
            pass

    # -- listeners --------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or self._guild_id is None or member.guild.id != self._guild_id:
            return
        guild = member.guild
        was = self._is_tracked_channel(before.channel, guild)
        now_tracked = self._is_tracked_channel(after.channel, guild)

        if not was and now_tracked:
            self._sessions[member.id] = time.time()
        elif was and not now_tracked:
            started = self._sessions.pop(member.id, None)
            if started is not None and self._active:
                elapsed = int(time.time() - started)
                if elapsed > 0:
                    self._totals[str(member.id)] = self._totals.get(str(member.id), 0) + elapsed

    # -- refresh loop -----------------------------------------------------
    @tasks.loop(seconds=EVENT_REFRESH_SECONDS)
    async def refresh_loop(self) -> None:
        self._ensure_loaded()
        cfg = load_config()
        self._active = bool(cfg.get("active"))
        self._guild_id = cfg.get("guild_id")
        self._channel_id = cfg.get("channel_id")
        self._goal_hours = int(cfg.get("goal_hours", DEFAULT_GOAL_HOURS))
        self._title = cfg.get("title", DEFAULT_TITLE)

        # Honour a reset issued from the control instance.
        token = int(cfg.get("reset_token", 0))
        if token != self._applied_reset_token:
            self._totals = {}
            self._sessions = {}
            self._applied_reset_token = token

        if not self._guild_id or not self._channel_id:
            return
        guild = self.bot.get_guild(self._guild_id)
        if guild is None:
            return  # this instance is not in the configured server

        self._sync_sessions(guild)
        self._flush_sessions()

        if self._active:
            channel = guild.get_channel(self._channel_id)
            if isinstance(channel, discord.TextChannel):
                await self._update_message(guild, channel)

        self._persist()

    @refresh_loop.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_unload(self) -> None:
        self.refresh_loop.cancel()
        self._flush_sessions()
        self._persist()


async def setup(bot_instance: commands.Bot) -> None:
    # Control commands live only on the test instance.
    if os.environ.get("TEST_MODE"):
        bot_instance.tree.add_command(event_group)
    # Tracking + display runs only on the designated display instance.
    if os.environ.get("EVENT_DISPLAY"):
        cog = EventLeaderboardCog(bot_instance)
        await bot_instance.add_cog(cog)
        cog.refresh_loop.start()
