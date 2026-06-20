"""
testkit.py — Test-only cog loaded exclusively when TEST_MODE=1.

Commands are registered globally (they only exist on the test bot, which is
loaded only under TEST_MODE) and require admin/owner role. Run !sync on the
test bot to make them appear in a guild immediately.
"""
from __future__ import annotations

import json
import platform
import time
import discord
from discord import app_commands
from discord.ext import commands

from core.constants import (
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_OWNER,
)
from core.context import bot, tree
from core.utils import format_duration, now_iso
from .shared import make_embed


# ---------------------------------------------------------------------------
# Guard: every testkit command requires admin or owner role
# ---------------------------------------------------------------------------

def _is_test_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    role_ids = {r.id for r in interaction.user.roles}
    cfg = bot.data_manager.config if bot.data_manager else {}
    admin_id = cfg.get("role_admin", DEFAULT_ROLE_ADMIN)
    owner_id = cfg.get("role_owner", DEFAULT_ROLE_OWNER)
    return bool(role_ids & {admin_id, owner_id}) or interaction.user.guild_permissions.administrator


# ---------------------------------------------------------------------------
# Testkit Cog
# ---------------------------------------------------------------------------

class TestkitCog(commands.Cog):
    """Diagnostic and testing commands — only loaded in TEST_MODE."""

    def __init__(self, bot_instance: commands.Bot) -> None:
        self.bot = bot_instance


@tree.command(
    name="test-ping",
    description="[TEST] Show bot latency and uptime.",
)
async def test_ping(interaction: discord.Interaction) -> None:
    """Latency and uptime snapshot."""
    if not _is_test_admin(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    latency_ms = round(bot.latency * 1000, 2)
    uptime_s = int(time.time() - bot.start_time)
    uptime_str = format_duration(uptime_s // 60)

    embed = make_embed(
        "Test · Ping",
        f"> **Latency:** {latency_ms} ms\n> **Uptime:** {uptime_str}",
        kind="info",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(
    name="test-dbstats",
    description="[TEST] Show in-memory data counts for each store.",
)
async def test_dbstats(interaction: discord.Interaction) -> None:
    """Row counts for every in-memory data store."""
    if not _is_test_admin(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    dm = bot.data_manager
    if not dm:
        await interaction.response.send_message("DataManager not ready.", ephemeral=True)
        return

    lines = [
        f"> **punishments:** {sum(len(v) for v in dm.punishments.values())} records across {len(dm.punishments)} users",
        f"> **modmail:** {len(dm.modmail)} tickets",
        f"> **roles:** {len(dm.roles)} entries",
        f"> **mod_stats:** {len(dm.mod_stats)} entries",
        f"> **lockdown:** {len(dm.lockdown)} channels",
        f"> **pings:** {len(dm.pings)} entries",
        f"> **case_index:** {len(dm.case_index)} cases",
        f"> **config keys:** {len(dm.config)}",
    ]
    embed = make_embed("Test · DB Stats", "\n".join(lines), kind="info")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(
    name="test-config-dump",
    description="[TEST] Dump the current in-memory config as JSON (ephemeral).",
)
async def test_config_dump(interaction: discord.Interaction) -> None:
    """Full config dump — ephemeral so it doesn't leak in chat."""
    if not _is_test_admin(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    dm = bot.data_manager
    if not dm:
        await interaction.response.send_message("DataManager not ready.", ephemeral=True)
        return

    raw = json.dumps(dm.config, indent=2, default=str)
    # Discord message limit is 2000; truncate if needed
    if len(raw) > 1900:
        raw = raw[:1900] + "\n… (truncated)"
    await interaction.response.send_message(f"```json\n{raw}\n```", ephemeral=True)


@tree.command(
    name="test-simulate-punishment",
    description="[TEST] Dry-run punishment logic for a user without applying it.",
)
@app_commands.describe(
    member="Target member",
    rule="Rule name (e.g. Spamming, NSFW)",
)
async def test_simulate_punishment(
    interaction: discord.Interaction,
    member: discord.Member,
    rule: str,
) -> None:
    """Show what punishment would be issued without executing it."""
    if not _is_test_admin(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    from core.services import resolve_escalation_duration
    dm = bot.data_manager
    if not dm:
        await interaction.response.send_message("DataManager not ready.", ephemeral=True)
        return

    uid = str(member.id)
    records = dm.punishments.get(uid, [])
    active = [r for r in records if r.get("active")]
    points = sum(r.get("points", 1) for r in active)

    rules = dm.config.get("rules", {})
    rule_data = rules.get(rule, {})
    base = rule_data.get("base", 0)
    escalated = rule_data.get("escalated", 0)

    duration, is_escalated, step_label = resolve_escalation_duration(points, base, escalated, dm.config)
    if duration == -1:
        outcome = "Ban (permanent)"
    elif duration == 0:
        outcome = "Warn (no timeout)"
    else:
        outcome = f"Timeout — {format_duration(duration)}"

    lines = [
        f"> **Target:** {member.mention} (`{uid}`)",
        f"> **Rule:** {rule}",
        f"> **Active points:** {points}",
        f"> **Escalation step:** {step_label}",
        f"> **Would issue:** {outcome}",
        "",
        "*No action was taken. This is a dry-run.*",
    ]
    embed = make_embed("Test · Simulate Punishment", "\n".join(lines), kind="warning")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(
    name="test-flush-cache",
    description="[TEST] Clear the native automod event cache and DM cooldowns.",
)
async def test_flush_cache(interaction: discord.Interaction) -> None:
    """Wipe runtime caches so you can re-trigger automod flows cleanly."""
    if not _is_test_admin(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    before_automod = len(bot.native_automod_event_cache)
    before_dm = len(bot.dm_modmail_prompt_cooldowns)
    bot.native_automod_event_cache.clear()
    bot.dm_modmail_prompt_cooldowns.clear()

    embed = make_embed(
        "Test · Cache Flushed",
        f"> Cleared **{before_automod}** automod cache entries\n"
        f"> Cleared **{before_dm}** DM cooldown entries",
        kind="success",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(
    name="test-user-history",
    description="[TEST] Show raw punishment records for a user.",
)
@app_commands.describe(member="Target member")
async def test_user_history(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    """Dumps raw punishment records — useful for verifying case logic."""
    if not _is_test_admin(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    dm = bot.data_manager
    if not dm:
        await interaction.response.send_message("DataManager not ready.", ephemeral=True)
        return

    records = dm.punishments.get(str(member.id), [])
    if not records:
        await interaction.response.send_message(
            f"No punishment records found for {member.mention}.", ephemeral=True
        )
        return

    raw = json.dumps(records, indent=2, default=str)
    if len(raw) > 1900:
        raw = raw[:1900] + "\n… (truncated)"
    await interaction.response.send_message(
        f"**{member}** — {len(records)} record(s):\n```json\n{raw}\n```",
        ephemeral=True,
    )


@tree.command(
    name="test-sysinfo",
    description="[TEST] Show Python version, discord.py version, and host info.",
)
async def test_sysinfo(interaction: discord.Interaction) -> None:
    """Runtime environment info — handy when debugging version-specific issues."""
    if not _is_test_admin(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return

    lines = [
        f"> **Python:** {platform.python_version()}",
        f"> **discord.py:** {discord.__version__}",
        f"> **Platform:** {platform.system()} {platform.release()}",
        f"> **Bot user:** {bot.user} (`{bot.user.id if bot.user else '?'}`)",
        f"> **Guilds:** {len(bot.guilds)}",
        f"> **Timestamp:** {now_iso()}",
    ]
    embed = make_embed("Test · Sysinfo", "\n".join(lines), kind="info")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot_instance: commands.Bot) -> None:
    await bot_instance.add_cog(TestkitCog(bot_instance))
    for command in (
        test_ping,
        test_dbstats,
        test_config_dump,
        test_simulate_punishment,
        test_flush_cache,
        test_user_history,
        test_sysinfo,
    ):
        bot_instance.tree.add_command(command)
