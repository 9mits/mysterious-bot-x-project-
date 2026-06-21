import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.bot import DISABLED_APPLICATION_COMMANDS, EXTENSIONS, create_bot
from cogs.config import ConfigChannelSelect, ConfigTypeSelect, SetupDashboardActionSelect
import cogs.config as config_module
import cogs.shared as shared_module


class MbxBootstrapTests(unittest.TestCase):
    def test_create_bot_does_not_require_token(self):
        bot = create_bot()
        self.assertEqual(bot.command_prefix, "!")
        self.assertIsNone(bot.data_manager)

    def test_cogs_import_without_running_bot(self):
        for module_name in (
            "cogs.cases",
            "cogs.moderation",
            "cogs.roles",
            "cogs.derole",
            "cogs.modmail",
            "cogs.automod",
            "cogs.config",
            "cogs.analytics",
            "cogs.admin",
            "cogs.events",
            "cogs.event_leaderboard",
            "cogs.testkit",
        ):
            module = importlib.import_module(module_name)
            self.assertTrue(hasattr(module, "setup"))

    def test_testkit_loads_and_registers_commands(self):
        # testkit is only loaded at runtime under TEST_MODE, so it is not in
        # EXTENSIONS and would otherwise never be exercised by the suite. This
        # guards against import-time crashes and missing command registration.
        async def runner():
            bot = create_bot()
            await bot.load_extension("cogs.testkit")
            names = {command.name for command in bot.tree.get_commands()}
            self.assertIn("test-ping", names)
            self.assertIn("test-sysinfo", names)

        asyncio.run(runner())

    def test_embed_to_panel_renders_components_v2(self):
        # The shared embed->V2 converter underpins all display-only logs/errors.
        async def runner():
            embed = shared_module.make_embed("Title", "> Body", kind="danger")
            embed.add_field(name="Field", value="Value", inline=True)
            panel = shared_module.embed_to_panel(embed)
            components = panel.to_components()
            # A single top-level Container (type 17) holding the rendered embed.
            self.assertEqual(components[0]["type"], 17)
            self.assertTrue(components[0]["components"])

        asyncio.run(runner())

    def test_setup_landing_is_components_v2(self):
        # The /setup dashboard is a Components V2 LayoutView with the nav buttons.
        async def runner():
            from cogs.config import SetupLandingView
            components = SetupLandingView().to_components()
            self.assertEqual(components[0]["type"], 17)  # Container
            labels = [
                button["label"]
                for child in components[0]["components"] if child["type"] == 1
                for button in child["components"]
            ]
            self.assertEqual(labels, ["Roles", "Channels", "Other"])

        asyncio.run(runner())

    def test_setup_exposes_modmail_panel_controls(self):
        channel_select = ConfigTypeSelect("channels")
        self.assertIn(
            "modmail_panel_channel",
            {option.value for option in channel_select.options},
        )

        action_select = SetupDashboardActionSelect()
        self.assertIn(
            "send_modmail_panel",
            {option.value for option in action_select.options},
        )

    def test_extensions_load_on_fresh_bot(self):
        async def runner():
            bot = create_bot()
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            bot._remove_disabled_application_commands()
            self.assertEqual(len(bot.extensions), len(EXTENSIONS))
            command_names = {command.qualified_name for command in bot.tree.walk_commands()}
            self.assertFalse(command_names & DISABLED_APPLICATION_COMMANDS)
            self.assertTrue(
                {
                    "commands",
                    "mod-guide",
                    "role-settings",
                    "derole",
                    "modmail-panel",
                    "security",
                    "lift-lockdown",
                    "undo",
                }.issubset(command_names)
            )
            commands_by_name = {command.qualified_name: command for command in bot.tree.walk_commands()}
            for command_name in ("punish", "history", "undo"):
                command = commands_by_name[command_name]
                user_param = next(param for param in command.parameters if param.name == "user")
                self.assertFalse(user_param.required)
            case_command = commands_by_name["case"]
            self.assertFalse(next(param for param in case_command.parameters if param.name == "case_id").required)
            self.assertFalse(next(param for param in case_command.parameters if param.name == "user").required)

        asyncio.run(runner())


class MbxSetupModmailPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_modmail_panel_embed_uses_local_banner_attachment(self):
        sent_message = SimpleNamespace(id=456)
        destination = SimpleNamespace(send=AsyncMock(return_value=sent_message))
        guild = SimpleNamespace(icon=None)

        result = await shared_module.send_modmail_panel_message(destination, guild)

        self.assertIs(result, sent_message)
        send_kwargs = destination.send.await_args.kwargs
        self.assertEqual(send_kwargs["embed"].image.url, "attachment://modmail_panel_banner.png")
        self.assertEqual(send_kwargs["file"].filename, "modmail_panel_banner.png")
        send_kwargs["file"].close()

    async def test_selecting_modmail_panel_channel_posts_panel(self):
        selected = SimpleNamespace(id=123)
        channel = SimpleNamespace(id=123, mention="<#123>")
        guild = SimpleNamespace(
            get_channel=Mock(return_value=channel),
            fetch_channel=AsyncMock(),
        )
        interaction = SimpleNamespace(guild=guild)
        data_manager = SimpleNamespace(config={}, save_config=AsyncMock())

        select = ConfigChannelSelect("modmail_panel_channel", "Modmail Panel Channel")
        select._values = [selected]

        with patch.object(config_module, "bot", SimpleNamespace(data_manager=data_manager)), patch.object(
            config_module,
            "send_configured_modmail_panel",
            AsyncMock(),
        ) as send_panel:
            await select.callback(interaction)

        self.assertEqual(data_manager.config["modmail_panel_channel"], 123)
        data_manager.save_config.assert_awaited_once()
        send_panel.assert_awaited_once_with(interaction, channel)


if __name__ == "__main__":
    unittest.main()
