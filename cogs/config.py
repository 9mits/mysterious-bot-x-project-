"""Server configuration views and /setup, /config commands."""

import discord
from discord import app_commands
from discord.ext import commands
import json
from typing import Optional
import io

from core.constants import (
    DEFAULT_ANCHOR_ROLE_ID,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    DEFAULT_ROLE_OWNER,
    SCOPE_SYSTEM,
    THEME_ORANGE,
)
from core.services import (
    DEFAULT_ESCALATION_MATRIX,
    export_config_payload,
    get_feature_flag,
    import_config_payload,
    validate_guild_configuration,
)
from core.context import bot, tree
from .shared import (
    make_embed,
    make_confirmation_embed,
    respond_with_error,
    build_config_dashboard_embed,
    build_escalation_matrix_embed,
    build_setup_validation_embed,
    check_admin,
    send_modmail_panel_message,
)


class ConfigRoleSelect(discord.ui.RoleSelect):
    def __init__(self, config_key: str, config_name: str):
        super().__init__(placeholder=f"Select {config_name}...", min_values=1, max_values=1)
        self.config_key = config_key
        self.config_name = config_name

    async def callback(self, interaction: discord.Interaction) -> None:
        role = self.values[0]
        bot.data_manager.config[self.config_key] = role.id
        await bot.data_manager.save_config()
        await interaction.response.send_message(embed=make_embed("Setting Updated", f"> **{self.config_name}** updated to {role.mention}.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)

class ConfigChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, config_key: str, config_name: str, channel_types=None):
        super().__init__(placeholder=f"Select {config_name}...", min_values=1, max_values=1, channel_types=channel_types)
        self.config_key = config_key
        self.config_name = config_name

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        channel = interaction.guild.get_channel(selected.id) or await interaction.guild.fetch_channel(selected.id)
        bot.data_manager.config[self.config_key] = channel.id
        if self.config_key == "general_log_channel_id":
            bot.data_manager.config["log_channel_id"] = channel.id
        await bot.data_manager.save_config()

        if self.config_key == "modmail_panel_channel":
            await send_configured_modmail_panel(interaction, channel)
            return

        await interaction.response.send_message(embed=make_embed("Setting Updated", f"> **{self.config_name}** updated to {channel.mention}.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)

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
            ]
        elif category == "channels":
            options = [
                discord.SelectOption(label="General Bot Log Channel", value="general_log_channel_id", description="Fallback log channel for general actions."),
                discord.SelectOption(label="Punishment Log Channel", value="punishment_log_channel_id", description="Primary punishment history log channel."),
                discord.SelectOption(label="Appeal Log Channel", value="appeal_channel_id", description="Where punishment appeals should go."),
                discord.SelectOption(label="AutoMod Log Channel", value="automod_log_channel_id", description="Where AutoMod bridge events should be logged."),
                discord.SelectOption(label="AutoMod Report Channel", value="automod_report_channel_id", description="Where user AutoMod reports should be sent."),
                discord.SelectOption(label="Archive Category", value="category_archive", description="Category for archive or storage channels."),
                discord.SelectOption(label="Modmail Inbox Channel", value="modmail_inbox_channel", description="Where incoming modmail tickets appear for staff."),
                discord.SelectOption(label="Modmail Action Log Channel", value="modmail_action_log_channel", description="Where modmail staff actions are logged."),
                discord.SelectOption(label="Modmail Panel Channel", value="modmail_panel_channel", description="Where the public support panel is posted."),
            ]
        super().__init__(
            placeholder=f"Select {category[:-1]} to configure...",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        key = self.values[0]
        name = next(o.label for o in self.options if o.value == key)

        row = discord.ui.ActionRow()
        if self.category == "roles":
            row.add_item(ConfigRoleSelect(key, name))
        elif self.category == "channels":
            c_types = [discord.ChannelType.category] if "category" in key else [discord.ChannelType.text]
            row.add_item(ConfigChannelSelect(key, name, channel_types=c_types))

        view = discord.ui.LayoutView(timeout=180)
        container = discord.ui.Container(accent_colour=THEME_ORANGE)
        container.add_item(discord.ui.TextDisplay(f"## Configure {name}"))
        container.add_item(discord.ui.TextDisplay(f"> Select the new **{name}** below."))
        container.add_item(row)
        view.add_item(container)
        await interaction.response.send_message(view=view, ephemeral=True)

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

    async def on_submit(self, interaction: discord.Interaction) -> None:
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
    async def edit_matrix(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(EscalationMatrixModal())

    @discord.ui.button(label="Reset Defaults", style=discord.ButtonStyle.secondary)
    async def reset_matrix(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        bot.data_manager.config["escalation_matrix"] = json.loads(json.dumps(DEFAULT_ESCALATION_MATRIX))
        await bot.data_manager.save_config()
        await interaction.response.edit_message(embed=build_escalation_matrix_embed(interaction.guild), view=self)


class ConfigImportModal(discord.ui.Modal, title="Paste Settings Backup"):
    config_json = discord.ui.TextInput(
        label="Settings JSON",
        style=discord.TextStyle.paragraph,
        placeholder='{"role_mod": 1234567890, "punishment_log_channel_id": 1234567890}',
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
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
            discord.SelectOption(label="Punishment Scaling", value="scaling", description="Edit the escalation matrix used by punishments."),
        ]
        super().__init__(
            placeholder="Choose a settings action...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
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
        if action == "scaling":
            await interaction.response.send_message(embed=build_escalation_matrix_embed(interaction.guild), view=EscalationMatrixView(), ephemeral=True)
            return


class ConfigDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ConfigDashboardActionSelect())


class GuildIdModal(discord.ui.Modal, title="Set Guild ID"):
    guild_id = discord.ui.TextInput(label="Guild ID", max_length=25)

    def __init__(self, current_guild_id: int):
        super().__init__()
        self.guild_id.default = str(current_guild_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.guild_id.value.isdigit():
            await interaction.response.send_message(embed=make_embed("Invalid ID", "> Invalid guild ID.", kind="error", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)
            return
        bot.data_manager.config["guild_id"] = int(self.guild_id.value)
        await bot.data_manager.save_config()
        await interaction.response.send_message(embed=make_embed("Guild ID Updated", f"> Guild ID set to `{self.guild_id.value}`.", kind="success", scope=SCOPE_SYSTEM, guild=interaction.guild), ephemeral=True)


async def _fetch_configured_modmail_panel_channel(interaction: discord.Interaction, channel_id: int):
    channel = interaction.guild.get_channel(channel_id)
    if channel is not None:
        return channel

    try:
        return await interaction.guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def _channel_can_receive_panel(channel) -> bool:
    return channel is not None and hasattr(channel, "send")


async def _resolve_modmail_panel_channel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
):
    if interaction.guild is None:
        return None, "The modmail panel can only be posted from a server.", False

    if channel is not None:
        return channel, None, False

    channel_id = bot.data_manager.config.get("modmail_panel_channel")
    if channel_id:
        try:
            normalized_channel_id = int(channel_id)
        except (TypeError, ValueError):
            return None, "The configured modmail panel channel ID is invalid.", False

        configured_channel = await _fetch_configured_modmail_panel_channel(interaction, normalized_channel_id)
        if not _channel_can_receive_panel(configured_channel):
            return None, "The configured modmail panel channel could not be found.", False
        return configured_channel, None, False

    current_channel = getattr(interaction, "channel", None)
    if _channel_can_receive_panel(current_channel):
        return current_channel, None, True

    return None, "Set **Modmail Panel Channel** under setup channels first.", False


def _missing_panel_permissions(channel, guild: discord.Guild) -> list[str]:
    if bot.user is None:
        return []

    me = guild.me or guild.get_member(bot.user.id)
    if me is None or not isinstance(channel, discord.abc.GuildChannel):
        return []

    permissions = channel.permissions_for(me)
    missing = []
    if not permissions.view_channel:
        missing.append("View Channel")
    if not permissions.send_messages:
        missing.append("Send Messages")
    if not permissions.embed_links:
        missing.append("Embed Links")
    if not permissions.attach_files:
        missing.append("Attach Files")
    return missing


async def send_configured_modmail_panel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    target_channel, error, used_current_channel = await _resolve_modmail_panel_channel(interaction, channel)
    if error:
        await respond_with_error(interaction, error, scope=SCOPE_SYSTEM)
        return

    missing_permissions = _missing_panel_permissions(target_channel, interaction.guild)
    if missing_permissions:
        await respond_with_error(
            interaction,
            "I am missing these permissions in the modmail panel channel: "
            + ", ".join(f"**{permission}**" for permission in missing_permissions)
            + ".",
            scope=SCOPE_SYSTEM,
        )
        return

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        message = await send_modmail_panel_message(
            target_channel,
            interaction.guild,
            require_embed=True,
        )
    except discord.Forbidden:
        await respond_with_error(
            interaction,
            "I cannot send the modmail panel embed in that channel. Make sure I have **View Channel**, **Send Messages**, **Embed Links**, and **Attach Files**.",
            scope=SCOPE_SYSTEM,
        )
        return
    except discord.HTTPException as exc:
        await respond_with_error(interaction, f"Discord rejected the modmail panel message: HTTP {exc.status}.", scope=SCOPE_SYSTEM)
        return

    if used_current_channel:
        bot.data_manager.config["modmail_panel_channel"] = target_channel.id
        await bot.data_manager.save_config()

    channel_mention = getattr(target_channel, "mention", f"`{target_channel.id}`")
    await interaction.followup.send(
        embed=make_confirmation_embed(
            "Modmail Panel Sent",
            f"> Posted the support panel in {channel_mention}.\n> [Jump to message]({message.jump_url})",
            scope=SCOPE_SYSTEM,
            guild=interaction.guild,
        ),
        ephemeral=True,
    )


class SetupDashboardActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Set Guild ID", value="guild_id", description="Change the guild ID used by the bot."),
            discord.SelectOption(label="Send Modmail Panel", value="send_modmail_panel", description="Post the support panel to the configured channel."),
            discord.SelectOption(label="Validate Setup", value="validate", description="Run the configuration validation checks."),
        ]
        super().__init__(
            placeholder="Choose another setup action...",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        action = self.values[0]
        if action == "guild_id":
            await interaction.response.send_modal(GuildIdModal(interaction.guild.id))
            return
        if action == "send_modmail_panel":
            await send_configured_modmail_panel(interaction)
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


def _roles_summary(config) -> str:
    return (
        f"**Owner** · <@&{config.get('role_owner', DEFAULT_ROLE_OWNER)}>\n"
        f"**Admin** · <@&{config.get('role_admin', DEFAULT_ROLE_ADMIN)}>\n"
        f"**Moderator** · <@&{config.get('role_mod', DEFAULT_ROLE_MOD)}>\n"
        f"**Community Manager** · <@&{config.get('role_community_manager', DEFAULT_ROLE_COMMUNITY_MANAGER)}>\n"
        f"**Anchor** · <@&{config.get('role_anchor', DEFAULT_ANCHOR_ROLE_ID)}>"
    )


def _channels_summary(config) -> str:
    def _ch(key: str) -> str:
        return f"<#{config[key]}>" if config.get(key) else "*Not set*"
    return (
        f"**General Log** · {_ch('general_log_channel_id')}\n"
        f"**Punishment Log** · {_ch('punishment_log_channel_id')}\n"
        f"**Appeal** · {_ch('appeal_channel_id')}\n"
        f"**AutoMod Log** · {_ch('automod_log_channel_id')}\n"
        f"**AutoMod Reports** · {_ch('automod_report_channel_id')}\n"
        f"**Archive Category** · {_ch('category_archive')}\n"
        f"**Modmail Inbox** · {_ch('modmail_inbox_channel')}\n"
        f"**Modmail Action Log** · {_ch('modmail_action_log_channel')}\n"
        f"**Modmail Panel** · {_ch('modmail_panel_channel')}"
    )


class SetupRolesView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        container = discord.ui.Container(accent_colour=THEME_ORANGE)
        container.add_item(discord.ui.TextDisplay("## Configure Roles"))
        container.add_item(discord.ui.TextDisplay(_roles_summary(bot.data_manager.config)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("-# Use the dropdown to update a role."))
        row = discord.ui.ActionRow()
        row.add_item(ConfigTypeSelect("roles"))
        container.add_item(row)
        self.add_item(container)


class SetupChannelsView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        container = discord.ui.Container(accent_colour=THEME_ORANGE)
        container.add_item(discord.ui.TextDisplay("## Configure Channels"))
        container.add_item(discord.ui.TextDisplay(_channels_summary(bot.data_manager.config)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("-# Use the dropdown to update a channel."))
        row = discord.ui.ActionRow()
        row.add_item(ConfigTypeSelect("channels"))
        container.add_item(row)
        self.add_item(container)


class SetupOtherView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        container = discord.ui.Container(accent_colour=THEME_ORANGE)
        container.add_item(discord.ui.TextDisplay("## Other Settings"))
        container.add_item(discord.ui.TextDisplay(
            "> Set the Guild ID, send the modmail panel, or run a full configuration validation check."
        ))
        container.add_item(discord.ui.Separator())
        row = discord.ui.ActionRow()
        row.add_item(SetupDashboardActionSelect())
        container.add_item(row)
        self.add_item(container)


class SetupLandingButtons(discord.ui.ActionRow):
    @discord.ui.button(label="Roles", style=discord.ButtonStyle.primary)
    async def roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(view=SetupRolesView(), ephemeral=True)

    @discord.ui.button(label="Channels", style=discord.ButtonStyle.primary)
    async def channels_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(view=SetupChannelsView(), ephemeral=True)

    @discord.ui.button(label="Other", style=discord.ButtonStyle.secondary)
    async def other_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(view=SetupOtherView(), ephemeral=True)


class SetupLandingView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        container = discord.ui.Container(accent_colour=THEME_ORANGE)
        container.add_item(discord.ui.TextDisplay("## Server Configuration"))
        container.add_item(discord.ui.TextDisplay(
            "> Configure your server's roles, channels, and guild-wide settings using the buttons below."
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(SetupLandingButtons())
        self.add_item(container)


@tree.command(name="setup", description="Configure server roles and channels.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def setup_slash(interaction: discord.Interaction):
    await interaction.response.send_message(view=SetupLandingView(), ephemeral=True)

@tree.command(name="config", description="Manage bot settings and backups.")
@app_commands.default_permissions(administrator=True)
@app_commands.check(check_admin)
async def config_cmd(interaction: discord.Interaction):
    if not get_feature_flag(bot.data_manager.config, "config_panel", True):
        await respond_with_error(interaction, "The bot settings panel is currently turned off in the feature settings.", scope=SCOPE_SYSTEM)
        return
    embed = build_config_dashboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, view=ConfigDashboardView(), ephemeral=True)


@tree.command(name="modmail-panel", description="Post the public modmail support panel.")
@app_commands.describe(channel="Channel to post in. Defaults to the configured panel channel or the current channel.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.check(check_admin)
async def modmail_panel_cmd(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await send_configured_modmail_panel(interaction, channel)




class ConfigCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(ConfigCog(bot))
    bot.tree.add_command(setup_slash)
    bot.tree.add_command(config_cmd)
    bot.tree.add_command(modmail_panel_cmd)
