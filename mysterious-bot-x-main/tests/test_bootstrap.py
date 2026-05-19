import asyncio
import importlib
import unittest

from modules.bot import EXTENSIONS, create_bot


class MbxBootstrapTests(unittest.TestCase):
    def test_create_bot_does_not_require_token(self):
        bot = create_bot()
        self.assertEqual(bot.command_prefix, "!")
        self.assertIsNone(bot.data_manager)

    def test_cogs_import_without_running_bot(self):
        for module_name in (
            "cogs.roles",
            "cogs.moderation",
            "cogs.modmail",
            "cogs.automod",
            "cogs.system",
        ):
            module = importlib.import_module(module_name)
            self.assertTrue(hasattr(module, "setup"))

    def test_extensions_load_on_fresh_bot(self):
        async def runner():
            bot = create_bot()
            for extension in EXTENSIONS:
                await bot.load_extension(extension)
            self.assertEqual(len(bot.extensions), len(EXTENSIONS))

        asyncio.run(runner())


if __name__ == "__main__":
    unittest.main()
