import asyncio
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import discord

from modules import data
from modules.data import DataManager


class DummyBot:
    def __init__(self):
        self.guilds = []


class MbxDataTests(unittest.TestCase):
    def setUp(self):
        self.manager = DataManager(DummyBot())
        self.manager.config = {"case_counter": 0}

    def test_allocate_case_id_increments_counter(self):
        self.assertEqual(self.manager.allocate_case_id(), 1)
        self.assertEqual(self.manager.config["case_counter"], 1)

    def test_prepare_punishment_record_adds_case_id_and_timestamp(self):
        record = self.manager.prepare_punishment_record({"type": "warn", "reason": "Test"})
        self.assertIn("case_id", record)
        self.assertIn("timestamp", record)
        self.assertFalse(record["active"])

    def test_message_cache_normalization_coerces_ids(self):
        normalized = self.manager._normalize_message_cache_record(
            {"id": "42", "author_id": "7", "channel_id": "9", "created_at": "2026-01-01T00:00:00+00:00"}
        )
        self.assertEqual(normalized["id"], 42)
        self.assertEqual(normalized["author_id"], 7)
        self.assertEqual(normalized["channel_id"], 9)
        self.assertIsInstance(normalized["created_at"], type(discord.utils.utcnow()))

    def test_load_all_initializes_defaults_and_migrations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db_dir = base / "database"
            db_dir.mkdir()
            for name, payload in {
                "config.json": "{}",
                "roles.json": "{}",
                "punishments.json": "{}",
                "mod_stats.json": "{}",
                "message_cache.json": "[]",
                "pings.json": "{}",
                "modmail.json": "{}",
                "lockdown.json": "{}",
            }.items():
                (db_dir / name).write_text(payload, encoding="utf-8")

            with patch.object(data, "CONFIG_FILE", db_dir / "config.json"), \
                patch.object(data, "ROLES_FILE", db_dir / "roles.json"), \
                patch.object(data, "PUNISHMENTS_FILE", db_dir / "punishments.json"), \
                patch.object(data, "MOD_STATS_FILE", db_dir / "mod_stats.json"), \
                patch.object(data, "MESSAGE_CACHE_FILE", db_dir / "message_cache.json"), \
                patch.object(data, "PINGS_FILE", db_dir / "pings.json"), \
                patch.object(data, "MODMAIL_FILE", db_dir / "modmail.json"), \
                patch.object(data, "LOCKDOWN_FILE", db_dir / "lockdown.json"):
                asyncio.run(self.manager.load_all())

        self.assertIn("feature_flags", self.manager.config)
        self.assertIsInstance(self.manager.message_cache, deque)

    def test_resolve_bot_token_prefers_environment_variable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config.json"
            config_file.write_text('{"token_env_var": "CUSTOM_BOT_TOKEN", "bot_token": "config-secret"}', encoding="utf-8")

            with patch.object(data, "CONFIG_FILE", config_file), patch.dict(os.environ, {"CUSTOM_BOT_TOKEN": "env-secret"}, clear=True):
                self.assertEqual(data.resolve_bot_token(), "env-secret")

    def test_resolve_bot_token_rejects_config_json_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config.json"
            config_file.write_text('{"bot_token": "config-secret"}', encoding="utf-8")

            with patch.object(data, "CONFIG_FILE", config_file), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    data.resolve_bot_token()


if __name__ == "__main__":
    unittest.main()
