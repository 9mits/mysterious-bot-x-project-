import unittest

from modules.services import (
    DEFAULT_ESCALATION_MATRIX,
    DEFAULT_SCHEMA_VERSION,
    get_native_automod_settings,
    has_capability,
    import_config_payload,
    resolve_escalation_duration,
    resolve_native_automod_policy,
    run_schema_migrations,
)


class MbxServicesTests(unittest.TestCase):
    def test_resolve_escalation_duration_uses_matrix(self):
        config = {"escalation_matrix": DEFAULT_ESCALATION_MATRIX}
        duration, escalated, label = resolve_escalation_duration(8, 0, 60, config)
        self.assertEqual(duration, 120)
        self.assertTrue(escalated)
        self.assertIn("x2", label)

    def test_import_config_payload_strips_bot_token(self):
        merged, warnings = import_config_payload({"feature_flags": {}}, {"bot_token": "secret", "modmail_sla_minutes": 45})
        self.assertNotIn("bot_token", merged)
        self.assertEqual(merged["modmail_sla_minutes"], 45)
        self.assertTrue(warnings)

    def test_native_automod_settings_normalize_rule_overrides(self):
        config = {
            "native_automod": {
                "enabled": True,
                "warning_dm_enabled": False,
                "rule_overrides": {
                    "123": {
                        "enabled": True,
                        "threshold": "5",
                        "window_minutes": "60",
                        "duration_minutes": "120",
                        "punishment_type": "timeout",
                    }
                },
            }
        }
        settings = get_native_automod_settings(config)
        policy = resolve_native_automod_policy(config, rule_id=123)
        self.assertTrue(settings["enabled"])
        self.assertFalse(settings["warning_dm_enabled"])
        self.assertTrue(policy["enabled"])
        self.assertEqual(len(policy["steps"]), 1)
        self.assertEqual(policy["steps"][0]["threshold"], 5)
        self.assertEqual(policy["steps"][0]["window_minutes"], 60)
        self.assertEqual(policy["steps"][0]["duration_minutes"], 120)
        self.assertEqual(policy["steps"][0]["punishment_type"], "timeout")

    def test_native_automod_policy_supports_multiple_steps(self):
        config = {
            "native_automod": {
                "rule_overrides": {
                    "123": {
                        "enabled": True,
                        "reason_template": "Repeated slur filter hits",
                        "steps": [
                            {"threshold": 6, "window_minutes": 1440, "punishment_type": "ban"},
                            {"threshold": 3, "window_minutes": 60, "punishment_type": "timeout", "duration_minutes": 60},
                            {"threshold": 5, "window_minutes": 720, "punishment_type": "timeout", "duration_minutes": 720},
                        ],
                    }
                }
            }
        }
        policy = resolve_native_automod_policy(config, rule_id=123)
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["reason_template"], "Repeated slur filter hits")
        self.assertEqual([step["threshold"] for step in policy["steps"]], [3, 5, 6])
        self.assertEqual(policy["steps"][1]["duration_minutes"], 720)
        self.assertEqual(policy["steps"][2]["duration_minutes"], -1)

    def test_native_automod_settings_tolerate_invalid_numeric_values(self):
        config = {
            "native_automod": {
                "default_escalation": {
                    "threshold": "invalid",
                    "window_minutes": None,
                    "duration_minutes": "oops",
                    "punishment_type": "not-real",
                },
                "rule_overrides": {
                    "123": {
                        "enabled": True,
                        "threshold": "invalid",
                        "window_minutes": "bad",
                        "duration_minutes": "oops",
                        "punishment_type": "also-not-real",
                    }
                },
                "immunity_roles": ["1", "bad", 2],
            }
        }

        settings = get_native_automod_settings(config)
        policy = resolve_native_automod_policy(config, rule_id=123)

        self.assertEqual(settings["default_escalation"]["threshold"], 3)
        self.assertEqual(settings["default_escalation"]["window_minutes"], 1440)
        self.assertEqual(settings["default_escalation"]["duration_minutes"], 0)
        self.assertEqual(settings["default_escalation"]["punishment_type"], "warn")
        self.assertEqual(settings["immunity_roles"], [1, 2])
        self.assertEqual(policy["steps"][0]["threshold"], 3)
        self.assertEqual(policy["steps"][0]["window_minutes"], 1440)
        self.assertEqual(policy["steps"][0]["duration_minutes"], 0)
        self.assertEqual(policy["steps"][0]["punishment_type"], "warn")

    def test_has_capability_ignores_invalid_role_ids(self):
        config = {
            "role_admin": "not-a-role",
            "role_owner": "still-bad",
            "role_community_manager": None,
        }
        self.assertFalse(has_capability([123], "config_panel", config))

    def test_run_schema_migrations_initializes_missing_structures(self):
        config = {}
        punishments = {"1": [{"case_id": 1, "type": "warn", "timestamp": "2026-01-01T00:00:00+00:00"}]}
        modmail = {"1": {"status": "open", "created_at": "2026-01-01T00:00:00+00:00"}}
        changed, notes = run_schema_migrations(config, punishments, modmail)
        self.assertTrue(changed)
        self.assertEqual(config["schema_version"], DEFAULT_SCHEMA_VERSION)
        self.assertIn("feature_flags", config)
        self.assertIn("native_automod", config)
        self.assertIn("action_id", punishments["1"][0])
        self.assertIn("priority", modmail["1"])
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()
