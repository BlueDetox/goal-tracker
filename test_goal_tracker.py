"""Run: python -m unittest -v test_goal_tracker"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from goal_tracker import (GoalStore, StorageError, SCHEMA_VERSION,
                          GOLD_PER_GOAL, GOLD_PER_LEVEL)


class GoalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "goals_data.json"
        self.store = GoalStore(self.path)
        self.store.load()

    def load_data(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.store.load()

    def test_first_run_has_no_fake_achievements(self):
        self.assertEqual(self.store.goals, [])
        self.assertEqual(self.store.total_xp, 0)
        self.assertFalse(self.path.exists())

    def test_completion_and_reopen_award_once_across_restarts(self):
        gid = self.store.add("Learn Python")
        self.store.set_progress(gid, 100)
        self.store.set_progress(gid, 20)
        self.store.load()
        self.store.set_progress(gid, 100)
        self.assertEqual(self.store.total_xp, 50)

    def test_lifetime_xp_survives_deletion_and_restart(self):
        gid = self.store.add("A")
        self.store.set_progress(gid, 100)
        self.store.delete(gid)
        self.store.load()
        self.assertEqual(self.store.total_xp, 50)
        self.assertEqual(self.store.goals, [])

    def test_stale_legacy_award_flag_is_repaired(self):
        self.load_data({"total_xp": 0, "goals": [{"id":"a", "title":"A", "progress":50, "completed":True, "xp_awarded":True}]})
        self.assertFalse(self.store.goals[0]["completed"])
        self.store.set_progress("a", 100)
        self.assertEqual(self.store.total_xp, 50)

    def test_legacy_list_rebuilds_xp(self):
        self.load_data([{"title":"A", "progress":100}, {"title":"B", "progress":50}])
        self.assertEqual(self.store.total_xp, 50)
        self.assertEqual(sum(g["completed"] for g in self.store.goals), 1)

    def test_original_muse_lifetime_xp_is_preserved(self):
        self.load_data({"total_xp":500, "goals":[{"title":"A", "progress":100}], "rewards":[]})
        self.assertEqual(self.store.total_xp, 500)
        self.assertEqual([r["level"] for r in self.store.rewards], [1,3,5])

    def test_duplicate_ids_and_normalization_collisions_are_repaired(self):
        self.load_data([{"id":"same", "title":"A"}, {"id":"'same'", "title":"B"}, {"title":"C"}])
        self.assertEqual(len({g["id"] for g in self.store.goals}), 3)
        self.store.delete("same")
        self.assertEqual(len(self.store.goals), 2)

    def test_bad_titles_are_backed_up_before_dropping(self):
        self.load_data([{"title":None}, {"title":123}, {"title":" "}, {"title":"Valid"}, None])
        self.assertEqual([g["title"] for g in self.store.goals], ["Valid"])
        backups = list((self.path.parent / "backups").glob("*.json"))
        self.assertEqual(len(json.loads(backups[0].read_text())), 5)

    def test_nan_and_infinite_updates_are_rejected(self):
        gid = self.store.add("A")
        for value in (math.nan, math.inf, -math.inf, True, "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.set_progress(gid, value)
        self.assertEqual(self.store.total_xp, 0)

    def test_loaded_progress_is_finite_and_clamped(self):
        self.load_data([{"title":"A", "progress":150}, {"title":"B", "progress":-1}, {"title":"C", "progress":math.nan}])
        self.assertEqual([g["progress"] for g in self.store.goals], [100,0,0])

    def test_malformed_rewards_do_not_crash(self):
        for value in (None, 42, "bad", {}, [None, {"level":True}, {"level":-1}]):
            with self.subTest(value=value):
                self.load_data({"goals":[], "rewards":value})
                self.assertEqual(self.store.rewards, [])

    def test_bad_xp_is_repaired(self):
        for value in (math.nan, math.inf, -100, True, "bad"):
            with self.subTest(value=value):
                self.load_data({"goals":[], "total_xp":value})
                self.assertEqual(self.store.total_xp, 0)

    def test_reward_duplicates_are_removed(self):
        self.load_data({"goals":[], "total_xp":100, "rewards":[{"level":1},{"level":1}]})
        self.assertEqual(len(self.store.rewards), 1)

    def test_empty_and_broken_saves_preserved_uniquely(self):
        for content in (b"", b"{broken", b"\xff\xfe"):
            self.path.write_bytes(content)
            self.store.load()
            self.assertEqual(self.store.goals, [])
            self.assertTrue(any(p.read_bytes() == content for p in (self.path.parent / "backups").glob("*.json")))
        self.assertEqual(len(list((self.path.parent / "backups").glob("*.json"))), 3)

    def test_second_load_does_not_recreate_samples(self):
        self.path.write_bytes(b"")
        self.store.load()
        self.store.load()
        self.assertEqual(self.store.goals, [])
        self.assertEqual(self.store.warning, "")

    def test_newer_schema_is_left_untouched(self):
        raw = json.dumps({"schema_version":999, "goals":[]}).encode()
        self.path.write_bytes(raw)
        with self.assertRaises(StorageError): self.store.load()
        self.assertEqual(self.path.read_bytes(), raw)

    def test_failed_save_preserves_memory_and_disk(self):
        self.store.add("Existing")
        before_data, before_bytes = deepcopy(self.store.data), self.path.read_bytes()
        with patch("goal_tracker.os.replace", side_effect=PermissionError("simulated")):
            with self.assertRaises(StorageError): self.store.add("Unsaved")
        self.assertEqual(self.store.data, before_data)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertFalse(any(p.name.startswith("tmp") for p in self.path.parent.iterdir()))

    def test_failed_final_replace_preserves_memory(self):
        self.store.add("Existing")
        before = deepcopy(self.store.data)
        from goal_tracker import atomic_write
        def failing_write(path, raw):
            if path == self.path: raise PermissionError("simulated")
            return atomic_write(path, raw)
        with patch("goal_tracker.atomic_write", side_effect=failing_write):
            with self.assertRaises(StorageError): self.store.add("Unsaved")
        self.assertEqual(self.store.data, before)

    def test_read_errors_do_not_reset_or_overwrite_data(self):
        self.store.add("Existing")
        before = deepcopy(self.store.data)
        with patch.object(self.store, "_read", side_effect=PermissionError("simulated")):
            with self.assertRaises(StorageError): self.store.load()
        self.assertEqual(self.store.data, before)

    def test_external_edit_is_not_overwritten(self):
        self.store.add("A")
        second = GoalStore(self.path)
        second.load()
        second.add("B")
        with self.assertRaises(StorageError): self.store.add("C")
        self.store.load()
        self.assertEqual(len(self.store.goals), 2)

    def test_undo_restores_delete_and_preserves_lifetime_xp(self):
        gid = self.store.add("A")
        self.store.set_progress(gid,100)
        self.store.delete(gid)
        self.store.undo()
        self.assertEqual(self.store.goals[0]["id"],gid)
        self.assertEqual(self.store.total_xp,50)

    def test_undo_completion_restores_xp_and_reward_state(self):
        a,b = self.store.add("A"),self.store.add("B")
        self.store.set_progress(a,100)
        self.store.set_progress(b,100)
        self.assertEqual(len(self.store.rewards),1)
        self.store.undo()
        self.assertEqual(self.store.total_xp,50)
        self.assertEqual(self.store.rewards,[])
        self.store.set_progress(b,100)
        self.assertEqual(self.store.total_xp,100)

    def test_failed_undo_keeps_history(self):
        self.store.add("A")
        with patch.object(self.store,"_save",side_effect=StorageError("simulated")):
            with self.assertRaises(StorageError): self.store.undo()
        self.assertTrue(self.store.can_undo)
        self.assertEqual(len(self.store.goals),1)

    def test_blank_or_oversized_titles_rejected(self):
        for value in ("", "  ", "x"*201, None):
            with self.subTest(value=value), self.assertRaises(ValueError): self.store.add(value)

    def test_unicode_round_trip_and_whitespace(self):
        self.store.add("  Study   café 日本語  ")
        self.store.load()
        self.assertEqual(self.store.goals[0]["title"],"Study café 日本語")

    def test_search_sort_and_filter(self):
        a,b = self.store.add("Build Python app"),self.store.add("Read book")
        self.store.set_progress(a,50)
        self.assertEqual(self.store.query(search="python BUILD")[0]["id"],a)
        self.assertEqual(self.store.query(sort="Progress")[0]["id"],a)
        self.store.set_progress(b,100)
        self.assertEqual(len(self.store.query("completed")),1)
        self.assertEqual(len(self.store.query("active")),1)

    def test_export_and_rolling_backup(self):
        self.store.add("A")
        first = self.path.read_bytes()
        self.store.add("B")
        self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(),first)
        export = self.path.parent / "export.json"
        self.store.export(export)
        self.assertEqual(json.loads(export.read_text()), self.store.data)
        with self.assertRaises(ValueError): self.store.export(self.path)

    def test_query_results_cannot_mutate_store(self):
        self.store.add("A")
        self.store.goals[0]["title"] = "Changed"
        self.assertEqual(self.store.goals[0]["title"], "A")

    def test_missing_id_does_not_change_state(self):
        with self.assertRaises(ValueError): self.store.delete("missing")
        self.assertFalse(self.store.can_undo)

    def test_noop_does_not_add_undo_step(self):
        gid = self.store.add("A")
        self.store.set_progress(gid,0)
        self.store.undo()
        self.assertEqual(self.store.goals,[])

    def test_other_window_cannot_write_while_lock_is_held(self):
        self.store.add("A")
        second = GoalStore(self.path)
        second.load()
        with self.store._locked():
            with self.assertRaises(StorageError): second.add("B")
        second.add("B")
        self.assertEqual(len(second.goals), 2)


class CompletionHistoryTests(unittest.TestCase):
    """The completion event log that drives the Stats tab graph."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "goals_data.json"
        self.store = GoalStore(self.path)
        self.store.load()

    def load_data(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.store.load()

    def event(self, days_ago, gid="g", title="T"):
        at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
        return {"goal_id": gid, "title": title, "at": at}

    def test_complete_logs_event_with_timestamp(self):
        gid = self.store.add("Read daily")
        self.store.set_progress(gid, 100)
        events = self.store.data["completion_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["goal_id"], gid)
        self.assertEqual(events[0]["title"], "Read daily")
        # Timestamp parses and is roughly now (UTC).
        when = datetime.fromisoformat(events[0]["at"])
        self.assertLess(abs((datetime.now(timezone.utc) - when).total_seconds()), 60)

    def test_reopen_withdraws_event_recomplete_adds_fresh_one(self):
        gid = self.store.add("A")
        self.store.set_progress(gid, 100)
        first_at = self.store.data["completion_events"][0]["at"]
        self.store.set_progress(gid, 50)
        self.assertEqual(self.store.data["completion_events"], [])
        self.store.set_progress(gid, 100)
        events = self.store.data["completion_events"]
        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(events[0]["at"], first_at)
        self.assertEqual(self.store.total_xp, 50)  # XP still awarded once

    def test_delete_keeps_completion_history(self):
        gid = self.store.add("A")
        self.store.set_progress(gid, 100)
        self.store.delete(gid)
        self.assertEqual(len(self.store.data["completion_events"]), 1)
        self.assertEqual(self.store.total_xp, 50)

    def test_rename_updates_event_title(self):
        gid = self.store.add("Old name")
        self.store.set_progress(gid, 100)
        self.store.rename(gid, "New name")
        self.assertEqual(self.store.data["completion_events"][0]["title"], "New name")

    def test_undo_of_completion_removes_event(self):
        gid = self.store.add("A")
        self.store.set_progress(gid, 100)
        self.store.undo()
        self.assertEqual(self.store.data["completion_events"], [])
        self.assertEqual(self.store.total_xp, 0)

    def test_events_survive_reload(self):
        gid = self.store.add("A")
        self.store.set_progress(gid, 100)
        second = GoalStore(self.path)
        second.load()
        self.assertEqual(len(second.data["completion_events"]), 1)

    def test_completions_by_day_buckets_last_14_days(self):
        for title in ("A", "B"):
            gid = self.store.add(title)
            self.store.set_progress(gid, 100)
        days = self.store.completions_by_day(14)
        self.assertEqual(len(days), 14)
        self.assertEqual(days[-1][1], 2)  # today
        self.assertTrue(all(count == 0 for _, count in days[:-1]))
        self.assertLess(days[0][0], days[-1][0])  # oldest first

    def test_completions_by_day_ignores_events_outside_window(self):
        self.load_data({"schema_version": SCHEMA_VERSION, "total_xp": 0,
                        "goals": [], "rewards": [],
                        "completion_events": [self.event(0), self.event(20)]})
        days = self.store.completions_by_day(14)
        self.assertEqual(days[-1][1], 1)
        self.assertEqual(sum(count for _, count in days), 1)

    def test_streak_counts_consecutive_days(self):
        self.load_data({"schema_version": SCHEMA_VERSION, "total_xp": 0,
                        "goals": [], "rewards": [],
                        "completion_events": [self.event(0), self.event(1),
                                              self.event(2), self.event(4)]})
        self.assertEqual(self.store.completion_streak(), 3)

    def test_streak_survives_missing_today(self):
        self.load_data({"schema_version": SCHEMA_VERSION, "total_xp": 0,
                        "goals": [], "rewards": [],
                        "completion_events": [self.event(1), self.event(2)]})
        self.assertEqual(self.store.completion_streak(), 2)

    def test_streak_is_zero_with_no_events(self):
        self.assertEqual(self.store.completion_streak(), 0)

    def test_bad_events_are_dropped_on_load(self):
        self.load_data({"schema_version": SCHEMA_VERSION, "total_xp": 0,
                        "goals": [], "rewards": [],
                        "completion_events": [self.event(0), {"at": "not-a-date"},
                                              "junk", {"goal_id": "x"}]})
        self.assertEqual(len(self.store.data["completion_events"]), 1)


class LoginStreakTests(unittest.TestCase):
    """The daily login history that drives the Stats tab check-in strip."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "goals_data.json"
        self.store = GoalStore(self.path)
        self.store.load()

    def day(self, days_ago):
        return (date.today() - timedelta(days=days_ago)).isoformat()

    def load_data(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.store.load()

    def test_record_login_marks_today_once(self):
        self.assertEqual(self.store.record_login(), 12)  # 10 base + 2 streak bonus
        self.assertEqual(self.store.record_login(), 0)
        self.assertEqual(self.store.data["login_history"],
                         [date.today().isoformat()])

    def test_record_login_never_creates_undo_step(self):
        self.store.record_login()
        self.assertFalse(self.store.can_undo)

    def test_record_login_persists_across_reload(self):
        self.store.record_login(self.day(2))
        fresh = GoalStore(self.path)
        fresh.load()
        self.assertIn(self.day(2), fresh.data["login_history"])

    def test_record_login_rejects_garbage_date(self):
        with self.assertRaises(ValueError):
            self.store.record_login("not-a-date")

    def test_login_streak_counts_consecutive_days(self):
        for ago in (2, 1, 0):
            self.store.record_login(self.day(ago))
        self.assertEqual(self.store.login_streak(), 3)

    def test_login_streak_breaks_on_gap(self):
        for ago in (3, 1, 0):
            self.store.record_login(self.day(ago))
        self.assertEqual(self.store.login_streak(), 2)

    def test_login_streak_counts_from_yesterday_when_today_missing(self):
        for ago in (2, 1):
            self.store.record_login(self.day(ago))
        self.assertEqual(self.store.login_streak(), 2)

    def test_login_streak_is_zero_with_no_history(self):
        self.assertEqual(self.store.login_streak(), 0)

    def test_longest_login_streak(self):
        for ago in (10, 9, 8, 7, 3, 2, 0):  # runs of 4, 2 and 1
            self.store.record_login(self.day(ago))
        self.assertEqual(self.store.longest_login_streak(), 4)

    def test_logins_by_day_flags_last_14_days(self):
        self.store.record_login(self.day(0))
        self.store.record_login(self.day(13))
        days = self.store.logins_by_day(14)
        self.assertEqual(len(days), 14)
        self.assertEqual(days[0][0], date.today() - timedelta(days=13))
        self.assertEqual(days[-1][0], date.today())
        self.assertTrue(days[0][1])    # 13 days ago: opened
        self.assertTrue(days[-1][1])   # today: opened
        self.assertFalse(days[1][1])   # 12 days ago: not opened

    def test_migration_defaults_empty_login_history(self):
        self.load_data({"schema_version": SCHEMA_VERSION, "total_xp": 0,
                        "goals": [], "rewards": [], "completion_events": []})
        self.assertEqual(self.store.data["login_history"], [])
        self.assertEqual(self.store.record_login(), 12)
        self.assertEqual(self.store.login_streak(), 1)

    def test_bad_login_rows_are_dropped_and_sorted_on_load(self):
        self.load_data({"schema_version": SCHEMA_VERSION, "total_xp": 0,
                        "goals": [], "rewards": [], "completion_events": [],
                        "login_history": [self.day(0), "not-a-date", 42,
                                          self.day(5), self.day(0)]})
        self.assertEqual(self.store.data["login_history"],
                         [self.day(5), self.day(0)])


class GoldTests(unittest.TestCase):
    """The V5.3 gold economy: login gold, goal gold, level-up gold."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "goals_data.json"
        self.store = GoalStore(self.path)
        self.store.load()

    def day(self, days_ago):
        return (date.today() - timedelta(days=days_ago)).isoformat()

    def load_data(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.store.load()

    def test_new_save_starts_at_zero_gold(self):
        self.assertEqual(self.store.gold, 0)

    def test_login_awards_base_plus_streak_bonus(self):
        self.assertEqual(self.store.record_login(self.day(1)), 12)  # streak 1
        self.assertEqual(self.store.record_login(self.day(0)), 14)  # streak 2
        self.assertEqual(self.store.gold, 26)

    def test_login_streak_bonus_caps(self):
        earned = [self.store.record_login(self.day(ago)) for ago in range(11, -1, -1)]
        self.assertEqual(earned,
            [12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 30, 30])
        self.assertEqual(self.store.gold, 270)

    def test_broken_streak_resets_login_bonus(self):
        self.store.record_login(self.day(3))  # streak 1 -> 12
        self.store.record_login(self.day(0))  # gap: streak 1 again -> 12
        self.assertEqual(self.store.gold, 24)

    def test_same_day_login_earns_nothing_more(self):
        self.store.record_login()
        gold = self.store.gold
        self.assertEqual(self.store.record_login(), 0)
        self.assertEqual(self.store.gold, gold)

    def test_goal_completion_awards_gold_once(self):
        gid = self.store.add("A")
        self.store.set_progress(gid, 100)
        self.assertEqual(self.store.gold, GOLD_PER_GOAL)
        self.store.set_progress(gid, 50)   # reopen
        self.store.set_progress(gid, 100)  # complete again: no double gold
        self.assertEqual(self.store.gold, GOLD_PER_GOAL)

    def test_level_up_awards_scaled_gold(self):
        for title in ("A", "B"):
            self.store.set_progress(self.store.add(title), 100)
        # 25 per goal + 25 x level 1  (100 XP = level 1)
        self.assertEqual(self.store.total_xp, 100)
        self.assertEqual(self.store.gold, 2 * GOLD_PER_GOAL + GOLD_PER_LEVEL * 1)

    def test_level_gold_scales_with_level(self):
        self.assertEqual(GoalStore.level_gold(1), 25)
        self.assertEqual(GoalStore.level_gold(5), 125)

    def test_migration_starts_at_zero_with_no_backdating(self):
        self.load_data({"schema_version": SCHEMA_VERSION, "total_xp": 200,
                        "goals": [], "rewards": [], "completion_events": []})
        self.assertEqual(self.store.gold, 0)  # level 2 reached pre-gold: nothing

    def test_bad_gold_values_reset_to_zero(self):
        for bad in ("lots", -5, True, 12.5, None):
            self.load_data({"schema_version": SCHEMA_VERSION, "total_xp": 0,
                            "goals": [], "rewards": [], "completion_events": [],
                            "gold": bad})
            self.assertEqual(self.store.gold, 0)

    def test_undo_restores_gold(self):
        gid = self.store.add("A")
        self.store.set_progress(gid, 100)
        self.assertEqual(self.store.gold, GOLD_PER_GOAL)
        self.store.undo()
        self.assertEqual(self.store.gold, 0)

    def test_gold_survives_reload(self):
        self.store.record_login()
        fresh = GoalStore(self.path)
        fresh.load()
        self.assertEqual(fresh.gold, 12)


if __name__ == "__main__":
    unittest.main()
