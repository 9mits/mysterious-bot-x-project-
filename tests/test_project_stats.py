import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import project_stats


def make_snapshot(bot_user_id, name, members, **stat_overrides):
    stats = {
        "total_cases": 0, "bans": 0, "timeouts": 0, "warns": 0,
        "active_cases": 0, "total_issued": 0, "cases_cleared": 0,
        "custom_roles": 0, "open_modmail": 0,
    }
    stats.update(stat_overrides)
    return {
        "bot_user_id": bot_user_id,
        "brand_name": "Test Bot",
        "guild_id": bot_user_id,
        "guild_name": name,
        "guild_icon_url": None,
        "member_count": members,
        "stats": stats,
        "started_at": "2026-06-25T00:00:00+00:00",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


class AggregateSnapshotsTests(unittest.TestCase):
    def test_sums_members_servers_and_actions(self):
        snaps = [
            make_snapshot(1, "Alpha", 100, total_cases=10, bans=2, timeouts=5, warns=3),
            make_snapshot(2, "Bravo", 250, total_cases=4, bans=1, timeouts=1, warns=2),
        ]
        result = project_stats.aggregate_snapshots(snaps)

        self.assertEqual(result["server_count"], 2)
        self.assertEqual(result["total_members"], 350)
        self.assertEqual(result["totals"]["total_cases"], 14)
        self.assertEqual(result["totals"]["bans"], 3)
        self.assertEqual(result["totals"]["timeouts"], 6)
        self.assertEqual(result["totals"]["warns"], 5)

    def test_servers_sorted_by_member_count_desc(self):
        snaps = [
            make_snapshot(1, "Small", 50),
            make_snapshot(2, "Large", 900),
            make_snapshot(3, "Medium", 300),
        ]
        names = [s["guild_name"] for s in project_stats.aggregate_snapshots(snaps)["servers"]]
        self.assertEqual(names, ["Large", "Medium", "Small"])

    def test_empty_input(self):
        result = project_stats.aggregate_snapshots([])
        self.assertEqual(result["server_count"], 0)
        self.assertEqual(result["total_members"], 0)
        self.assertEqual(result["totals"]["total_cases"], 0)


class SnapshotIOTests(unittest.TestCase):
    def test_write_read_round_trip_and_skips_bad_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_dir = Path(temp_dir)
            with patch.object(project_stats, "PROJECT_STATS_DIR", stats_dir):
                project_stats._write_snapshot_sync(make_snapshot(1, "Alpha", 100))
                project_stats._write_snapshot_sync(make_snapshot(2, "Bravo", 200))

                # A corrupt file and a foreign one must be ignored, not crash.
                (stats_dir / "broken.json").write_text("{not json", encoding="utf-8")
                (stats_dir / "foreign.json").write_text(json.dumps({"hello": 1}), encoding="utf-8")

                snaps = project_stats.read_all_snapshots()

            self.assertEqual(len(snaps), 2)
            self.assertEqual({s["guild_name"] for s in snaps}, {"Alpha", "Bravo"})

    def test_read_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "does-not-exist"
            with patch.object(project_stats, "PROJECT_STATS_DIR", missing):
                self.assertEqual(project_stats.read_all_snapshots(), [])

    def test_write_snapshot_async_persists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_dir = Path(temp_dir)
            with patch.object(project_stats, "PROJECT_STATS_DIR", stats_dir), \
                    patch.object(project_stats, "build_snapshot", return_value=make_snapshot(7, "Async", 42)):
                asyncio.run(project_stats.write_snapshot(object()))
                self.assertTrue((stats_dir / "7.json").exists())


class StalenessTests(unittest.TestCase):
    def test_fresh_snapshot_not_stale(self):
        self.assertFalse(project_stats.is_stale(make_snapshot(1, "Alpha", 10)))

    def test_old_snapshot_is_stale(self):
        old = make_snapshot(1, "Alpha", 10)
        old["updated_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=project_stats.STALE_AFTER_SECONDS + 60)
        ).isoformat()
        self.assertTrue(project_stats.is_stale(old))


if __name__ == "__main__":
    unittest.main()
