from __future__ import annotations

from collections.abc import Iterable

import discord
from discord import app_commands
from discord.ext import commands


MAX_ROLE_SELECT = 25


def _format_roles(roles: Iterable[discord.Role]) -> str:
    return ", ".join(role.mention for role in roles)


class DeroleRoleSelect(discord.ui.RoleSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Select role(s) to remove from everyone",
            min_values=1,
            max_values=MAX_ROLE_SELECT,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, DeroleView):
            await interaction.response.send_message(
                "This derole menu is no longer available.",
                ephemeral=True,
            )
            return

        await view.select_roles(interaction, list(self.values))


class DeroleView(discord.ui.View):
    def __init__(self, cog: Derole, requester_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.selected_roles: list[discord.Role] = []

        self.role_select = DeroleRoleSelect()
        self.confirm_button = discord.ui.Button(
            label="Remove from everyone",
            style=discord.ButtonStyle.danger,
            disabled=True,
        )
        self.cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )

        self.confirm_button.callback = self.confirm
        self.cancel_button.callback = self.cancel

        self.add_item(self.role_select)
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True

        await interaction.response.send_message(
            "Only the user who started `/derole` can use this menu.",
            ephemeral=True,
        )
        return False

    async def select_roles(
        self,
        interaction: discord.Interaction,
        roles: list[discord.Role],
    ) -> None:
        errors = await self.cog.validate_roles(interaction, roles)
        if errors:
            self.selected_roles = []
            self.confirm_button.disabled = True
            await interaction.response.edit_message(
                content="I cannot derole that selection:\n"
                + "\n".join(f"- {error}" for error in errors),
                view=self,
            )
            return

        self.selected_roles = roles
        self.confirm_button.disabled = False
        await interaction.response.edit_message(
            content=(
                f"Selected: {_format_roles(roles)}\n"
                "Click **Remove from everyone** to remove the selected role(s) "
                "from every matching member."
            ),
            view=self,
        )

    async def confirm(self, interaction: discord.Interaction) -> None:
        if not self.selected_roles:
            await interaction.response.send_message(
                "Select at least one role first.",
                ephemeral=True,
            )
            return

        errors = await self.cog.validate_roles(interaction, self.selected_roles)
        if errors:
            self.confirm_button.disabled = True
            await interaction.response.edit_message(
                content="I cannot derole that selection:\n"
                + "\n".join(f"- {error}" for error in errors),
                view=self,
            )
            return

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content=(
                f"Removing {_format_roles(self.selected_roles)} from matching "
                "members..."
            ),
            view=self,
        )

        result = await self.cog.remove_roles_from_everyone(
            interaction,
            self.selected_roles,
        )
        await interaction.edit_original_response(content=result, view=None)
        self.stop()

    async def cancel(self, interaction: discord.Interaction) -> None:
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="Cancelled `/derole`.",
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class Derole(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="derole",
        description="Remove selected role(s) from every member who has them.",
    )
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def derole(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(
            interaction.user,
            discord.Member,
        ):
            await interaction.response.send_message(
                "`/derole` can only be used in a server.",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "You need **Manage Roles** to use `/derole`.",
                ephemeral=True,
            )
            return

        view = DeroleView(self, interaction.user.id)
        await interaction.response.send_message(
            "Select one or more roles to remove from every member who has them.",
            view=view,
            ephemeral=True,
        )

    async def validate_roles(
        self,
        interaction: discord.Interaction,
        roles: list[discord.Role],
    ) -> list[str]:
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            return ["This command can only be used in a server."]

        bot_member = await self._get_bot_member(guild)
        if bot_member is None:
            return ["I could not find my member profile in this server."]

        errors: list[str] = []
        if not bot_member.guild_permissions.manage_roles:
            errors.append("I need **Manage Roles**.")

        for role in roles:
            if role == guild.default_role:
                errors.append("`@everyone` cannot be removed.")
                continue
            if role.managed:
                errors.append(f"{role.mention} is managed by an integration.")
            if role >= bot_member.top_role:
                errors.append(f"{role.mention} is above or equal to my top role.")
            if actor.id != guild.owner_id and role >= actor.top_role:
                errors.append(f"{role.mention} is above or equal to your top role.")

        return errors

    async def remove_roles_from_everyone(
        self,
        interaction: discord.Interaction,
        roles: list[discord.Role],
    ) -> str:
        guild = interaction.guild
        if guild is None:
            return "`/derole` can only be used in a server."

        reason = (
            f"/derole by {interaction.user} "
            f"({interaction.user.id}): {', '.join(role.name for role in roles)}"
        )
        members_updated = 0
        assignments_removed = 0
        failures: list[str] = []

        try:
            async for member in guild.fetch_members(limit=None):
                member_role_ids = {role.id for role in member.roles}
                member_roles = [
                    role for role in roles if role.id in member_role_ids
                ]
                if not member_roles:
                    continue

                try:
                    await member.remove_roles(*member_roles, reason=reason)
                except discord.Forbidden:
                    failures.append(f"{member} ({member.id}): missing role hierarchy")
                except discord.HTTPException as exc:
                    failures.append(f"{member} ({member.id}): HTTP {exc.status}")
                else:
                    members_updated += 1
                    assignments_removed += len(member_roles)
        except discord.ClientException:
            return (
                "I cannot scan every server member because the bot is missing "
                "the members intent. Enable **Server Members Intent** in the "
                "Discord Developer Portal and in the bot startup intents."
            )
        except discord.Forbidden:
            return (
                "Discord blocked the member scan. Enable **Server Members "
                "Intent** and make sure I have permission to view this server."
            )
        except discord.HTTPException as exc:
            return (
                "Discord returned an error while scanning members: "
                f"HTTP {exc.status}."
            )

        summary = [
            f"Finished removing {_format_roles(roles)}.",
            f"Members updated: **{members_updated}**",
            f"Role assignments removed: **{assignments_removed}**",
        ]
        if failures:
            summary.append(f"Failures: **{len(failures)}**")
            summary.extend(f"- {failure}" for failure in failures[:5])
            if len(failures) > 5:
                summary.append(f"- ...and {len(failures) - 5} more.")

        return "\n".join(summary)

    async def _get_bot_member(self, guild: discord.Guild) -> discord.Member | None:
        if guild.me is not None:
            return guild.me
        if self.bot.user is None:
            return None
        return guild.get_member(self.bot.user.id) or await guild.fetch_member(
            self.bot.user.id,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Derole(bot))
