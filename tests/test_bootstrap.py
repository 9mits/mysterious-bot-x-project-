import asyncio
import importlib
import unittest

from core.bot import DISABLED_APPLICATION_COMMANDS, EXTENSIONS, create_bot


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
            "cogs.modmail",
            "cogs.automod",
            "cogs.config",
            "cogs.analytics",
            "cogs.system",
        ):
            module = importlib.import_module(module_name)
            self.assertTrue(hasattr(module, "setup"))

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
                    "role-guide",
                    "role-settings",
                    "security",
                    "lift-lockdown",
                    "undo",
                }.issubset(command_names)
            )

        asyncio.run(runner())


if __name__ == "__main__":
    unittest.main()
