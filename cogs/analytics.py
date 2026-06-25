"""Staff analytics, moderation stats, and /stats, /directory commands."""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta
from typing import Optional
from collections import Counter

from core.constants import (
    BRAND_NAME,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_OWNER,
    SCOPE_ANALYTICS,
)
from core.context import bot, tree
from core.project_stats import (
    aggregate_snapshots,
    is_stale,
    read_all_snapshots,
    write_snapshot,
)
from core.utils import iso_to_dt, create_progress_bar
from .shared import (
    truncate_text,
    format_duration,
    format_log_quote,
    make_embed,
    format_user_ref,
    check_admin,
)
from .cases import (
    get_case_label,
    is_record_active,
)

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

    async def callback(self, interaction: discord.Interaction) -> None:
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
    async def back_stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=self.stats_embed, view=self)

    @discord.ui.button(label="Back to Directory", style=discord.ButtonStyle.primary, row=1)
    async def back_dir(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = StaffView(self.staff_members)
        await interaction.response.edit_message(embed=self.directory_embed, view=view)

class StaffSelect(discord.ui.Select):
    def __init__(self, staff_members):
        self.staff_members = staff_members
        options = []
        for m in staff_members[:25]:
            options.append(discord.SelectOption(label=m.display_name, value=str(m.id)))
        super().__init__(placeholder="Select a staff member to view stats...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
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
            await interaction.response.send_message(embed=make_embed("User Not Found", "> User not found.", kind="error", scope=SCOPE_ANALYTICS, guild=interaction.guild), ephemeral=True)

class StaffView(discord.ui.View):
    def __init__(self, staff_members):
        super().__init__(timeout=180)
        self.add_item(StaffSelect(staff_members))

@tree.command(name="stats", description="View moderation analytics.")
@app_commands.default_permissions(manage_guild=True)
async def stats(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    conf = bot.data_manager.config
    allowed = {
        conf.get("role_admin", DEFAULT_ROLE_ADMIN),
        conf.get("role_owner", DEFAULT_ROLE_OWNER),
        conf.get("role_community_manager", DEFAULT_ROLE_COMMUNITY_MANAGER)
    }
    if not interaction.user.guild_permissions.administrator and not any(r.id in allowed for r in interaction.user.roles):
        await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have the required Admin role.", kind="error", scope=SCOPE_ANALYTICS, guild=interaction.guild), ephemeral=True)
        return

    if target:
        uid = str(target.id)
        cases = get_mod_cases(uid)
        
        # Check if user is currently staff or has history
        is_target_staff = False
        if target.guild_permissions.administrator:
            is_target_staff = True
        else:
            mod_role_ids = bot.data_manager.config.get("mod_roles", [])
            if mod_role_ids:
                if any(r.id in mod_role_ids for r in target.roles):
                    is_target_staff = True
            elif target.guild_permissions.moderate_members:
                is_target_staff = True
        
        if not is_target_staff and not cases:
            await interaction.response.send_message(embed=make_embed("No Data", f"> {target.mention} is not a staff member and has no recorded history.", kind="info", scope=SCOPE_ANALYTICS, guild=interaction.guild), ephemeral=True)
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

@tree.command(name="directory", description="View the staff directory.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def directory(interaction: discord.Interaction):
    conf = bot.data_manager.config
    allowed = {
        conf.get("role_admin", DEFAULT_ROLE_ADMIN),
        conf.get("role_owner", DEFAULT_ROLE_OWNER),
        conf.get("role_community_manager", DEFAULT_ROLE_COMMUNITY_MANAGER)
    }
    if not interaction.user.guild_permissions.administrator and not any(r.id in allowed for r in interaction.user.roles):
        await interaction.response.send_message(embed=make_embed("Access Denied", "> You do not have the required Admin role.", kind="error", scope=SCOPE_ANALYTICS, guild=interaction.guild), ephemeral=True)
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


MAX_SERVER_LINES = 15


@tree.command(name="about", description=f"View project-wide stats across every server {BRAND_NAME} runs.")
async def about(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # Refresh this instance's own snapshot first so its line is current.
    await write_snapshot(bot)
    summary = aggregate_snapshots(read_all_snapshots())

    if summary["server_count"] == 0:
        embed = make_embed(
            f"About {BRAND_NAME}",
            "> Project stats are still warming up. Try again in a minute.",
            kind="info",
            scope=SCOPE_ANALYTICS,
            guild=interaction.guild,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    totals = summary["totals"]
    embed = make_embed(
        f"About {BRAND_NAME}",
        f"> Combined reach and moderation activity across every server {BRAND_NAME} protects.",
        kind="analytics",
        scope=SCOPE_ANALYTICS,
        guild=interaction.guild,
    )

    embed.add_field(
        name="Network Overview",
        value=(
            f">>> Servers: **{summary['server_count']:,}**\n"
            f"Total Members: **{summary['total_members']:,}**\n"
            f"Moderation Actions: **{totals['total_cases']:,}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Action Breakdown",
        value=(
            f">>> Bans: **{totals['bans']:,}**\n"
            f"Timeouts: **{totals['timeouts']:,}**\n"
            f"Warnings: **{totals['warns']:,}**"
        ),
        inline=True,
    )

    server_lines = []
    for snap in summary["servers"][:MAX_SERVER_LINES]:
        name = truncate_text(snap.get("guild_name", "Unknown Server"), 60)
        members = int(snap.get("member_count", 0) or 0)
        actions = int((snap.get("stats", {}) or {}).get("total_cases", 0) or 0)
        line = f"**{name}** — {members:,} members · {actions:,} actions"
        if is_stale(snap):
            updated = iso_to_dt(snap.get("updated_at"))
            seen = discord.utils.format_dt(updated, "R") if updated else "a while ago"
            line += f" · last seen {seen}"
        server_lines.append(line)

    remaining = summary["server_count"] - len(server_lines)
    if remaining > 0:
        server_lines.append(f"...and **{remaining}** more server{'s' if remaining != 1 else ''}")

    embed.add_field(name="Servers", value=">>> " + "\n".join(server_lines), inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


class AnalyticsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(AnalyticsCog(bot))
    bot.tree.add_command(stats)
    bot.tree.add_command(directory)
    bot.tree.add_command(about)
