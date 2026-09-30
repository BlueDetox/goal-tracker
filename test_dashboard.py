"""Real Tk widget tests. Run on a desktop: python -m unittest -v test_dashboard"""
from datetime import date
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from dashboard import Dashboard
from goal_tracker import GoalStore, StorageError


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = GoalStore(Path(self.temp.name) / "goals.json")
        self.store.load()
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.info = patch("dashboard.messagebox.showinfo").start()
        self.error = patch("dashboard.messagebox.showerror").start()
        self.confirm = patch("dashboard.messagebox.askyesno", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.app = Dashboard(self.root, self.store)
        self.addCleanup(self.app.close)
        self.root.update()

    def tearDown(self):
        self.assertEqual(self.errors, [], "Tk callback raised an error")

    def test_add_button_saves_and_clears_input(self):
        self.app.new_title.set("Learn Python")
        self.app.add_button.invoke()
        self.assertEqual(self.store.goals[0]["title"], "Learn Python")
        self.assertEqual(self.app.new_title.get(), "")
        self.assertEqual(len(self.app.cards), 1)

    def test_invalid_add_retains_input_and_shows_error(self):
        self.app.new_title.set("x" * 201)
        self.app.add_button.invoke()
        self.assertEqual(self.store.goals, [])
        self.assertEqual(len(self.app.new_title.get()), 201)
        self.error.assert_called_once()

    def test_slider_reaches_100_and_awards_xp_once(self):
        # V5: dragging the slider to 100 completes the goal. The backend's
        # xp_awarded flag makes a second trip to 100 (after a reopen) a
        # no-op for XP.
        gid = self.store.add("A")
        self.app.refresh()
        scale = self.app.cards[gid]["scale"]
        scale.set(150)
        self.assertEqual(scale.get(), 100)
        self.app.set_progress(gid, scale.get())
        self.assertTrue(self.store.goals[0]["completed"])
        self.assertEqual(self.store.total_xp, 50)
        self.store.set_progress(gid, 0)    # reopen
        self.store.set_progress(gid, 100)  # complete again
        self.assertEqual(self.store.total_xp, 50)

    def test_complete_button_and_undo(self):
        gid = self.store.add("A")
        self.app.refresh()
        self.app.cards[gid]["complete"].invoke()
        self.assertEqual(self.store.total_xp, 50)
        self.assertEqual(len(self.app.cards), 0)
        self.app.undo_button.invoke()
        self.assertEqual(self.store.total_xp, 0)
        self.assertIn(gid, self.app.cards)

    def test_search_and_filter_controls(self):
        a = self.store.add("Study Python")
        b = self.store.add("Read book")
        self.store.set_progress(b,100)
        self.app.tabs["all"].invoke()
        self.assertEqual(len(self.app.cards),2)
        self.app.search.set("python")
        self.app.search_now()
        self.assertEqual(list(self.app.cards),[a])
        self.app.search.set("")
        self.app.tabs["completed"].invoke()
        self.assertEqual(list(self.app.cards),[b])

    def test_rewards_and_level_up(self):
        # V5: level-ups show a non-blocking in-window celebration panel,
        # not a modal dialog.
        for title in ("A","B"):
            gid = self.store.add(title)
            self.app.set_progress(gid,100)
        self.info.assert_not_called()
        overlay = self.app.level_overlay
        self.assertIsNotNone(overlay)
        labels = [w.cget("text") for w in overlay.winfo_children() if w.winfo_class() == "Label"]
        self.assertIn("LEVEL UP!", labels)
        self.app.dismiss_level_up()
        self.assertIsNone(self.app.level_overlay)
        self.app.tabs["rewards"].invoke()
        self.assertEqual(self.app.count_label.cget("text"), "1 / 6 earned")

    def test_escape_only_clears_search_when_focused(self):
        # V5: Escape no longer wipes the search box while typing elsewhere.
        self.root.deiconify()
        self.root.update()
        self.app.search.set("hello")
        self.app.new_entry.focus_force()
        self.root.update()
        self.app.new_entry.event_generate("<Escape>")
        self.root.update()
        self.assertEqual(self.app.search.get(), "hello")
        self.app.search_entry.focus_force()
        self.root.update()
        self.app.search_entry.event_generate("<Escape>")
        self.root.update()
        self.assertEqual(self.app.search.get(), "")

    def test_escape_dismisses_level_up_overlay(self):
        # The root must be visible for synthetic key events to dispatch.
        self.root.deiconify()
        self.root.update()
        gid = self.store.add("A")
        self.store.add("B")
        self.app.set_progress(gid, 100)
        self.app.set_progress(self.store.goals[1]["id"], 100)
        self.assertIsNotNone(self.app.level_overlay)
        self.root.event_generate("<Escape>")
        self.root.update()
        self.assertIsNone(self.app.level_overlay)

    def test_tab_labels_show_counts(self):
        b = self.store.add("B")
        self.store.add("A")
        self.store.set_progress(b, 100)
        self.app.refresh()
        self.assertEqual(self.app.tabs["active"].cget("text"), "Active (1)")
        self.assertEqual(self.app.tabs["completed"].cget("text"), "Completed (1)")
        self.assertEqual(self.app.tabs["all"].cget("text"), "All goals (2)")
        self.assertEqual(self.app.tabs["rewards"].cget("text"), "Rewards (0)")
        self.assertEqual(self.app.tabs["stats"].cget("text"), "Stats")

    def test_edit_goal_dialog(self):
        gid = self.store.add("Old")
        with patch("dashboard.simpledialog.askstring", return_value="New"):
            self.app.edit_goal(gid)
        self.assertEqual(self.store.goals[0]["title"],"New")

    def test_save_error_is_displayed_and_input_retained(self):
        self.app.new_title.set("Keep this title")
        with patch.object(self.store,"_save",side_effect=StorageError("Disk full")):
            self.app.add_goal()
        self.assertEqual(self.app.new_title.get(),"Keep this title")
        self.assertEqual(self.store.goals,[])
        self.error.assert_called_once()

    def test_long_title_and_window_resize(self):
        gid = self.store.add("A long goal title " * 11)
        self.root.deiconify()
        self.root.geometry("860x700")
        self.app.refresh()
        self.root.update()
        title = self.app.cards[gid]["title"]
        self.assertGreater(title.winfo_height(),20)
        self.assertLessEqual(title.winfo_width(),self.app.canvas.winfo_width())
        self.assertTrue(self.app.new_entry.winfo_ismapped())
        self.assertLess(self.app.new_entry.winfo_rooty() + self.app.new_entry.winfo_height(),
                        self.root.winfo_rooty() + self.root.winfo_height())

    def test_scroll_recovers_when_list_shrinks(self):
        for i in range(15): self.store.add(f"Goal {i}")
        self.root.deiconify()
        self.app.refresh()
        self.root.update()
        self.app.canvas.yview_moveto(1)
        for goal in self.store.goals[1:]: self.store.delete(goal["id"])
        self.app.refresh()
        self.root.update()
        self.assertEqual(self.app.canvas.yview()[0],0)

    def test_close_keeps_unsaved_title_when_cancelled(self):
        self.app.new_title.set("Unfinished draft")
        self.confirm.return_value = False
        self.app.close()
        self.assertTrue(self.root.winfo_exists())
        self.assertEqual(self.app.new_title.get(), "Unfinished draft")
        self.confirm.return_value = True

    def test_keyboard_progress_keeps_focus_after_save(self):
        gid = self.store.add("Keyboard goal")
        self.root.deiconify()
        self.app.refresh()
        self.root.update()
        self.app.cards[gid]["scale"].focus_force()
        self.root.update()
        self.app.set_progress(gid, 25)
        self.root.update()
        self.assertIs(self.root.focus_get(), self.app.cards[gid]["scale"])

    def test_keyboard_slider_events_update_and_save(self):
        gid = self.store.add("Keyboard goal")
        self.root.deiconify()
        self.app.refresh()
        self.root.update()
        self.app.cards[gid]["scale"].focus_force()
        self.root.update()
        self.app.cards[gid]["scale"].event_generate("<KeyPress-Right>")
        self.app.cards[gid]["scale"].event_generate("<KeyRelease-Right>")
        self.root.update()
        self.assertEqual(self.store.goals[0]["progress"], 1)

    def test_stats_tab_renders_completion_chart(self):
        gid = self.store.add("A")
        self.store.set_progress(gid, 100)
        self.app.tabs["stats"].invoke()
        self.root.update()
        self.assertEqual(self.app.tabs["stats"].cget("text"), "Stats")
        self.assertIn("1 completion", self.app.count_label.cget("text"))
        canvases = [w for w in self.app.list_frame.winfo_children()
                    if w.winfo_class() == "Canvas"]
        self.assertEqual(len(canvases), 2)  # bar chart + check-in dot strip

    def test_stats_tab_empty_state(self):
        self.app.tabs["stats"].invoke()
        self.root.update()
        self.assertIn("0 completions", self.app.count_label.cget("text"))

    def test_opening_app_records_daily_login(self):
        # setUp already constructed the Dashboard once: today must be marked.
        self.assertIn(date.today().isoformat(), self.store.data["login_history"])

    def test_stats_tab_shows_check_in_section(self):
        self.app.tabs["stats"].invoke()
        self.root.update()
        labels = [w.cget("text") for w in self.app.list_frame.winfo_children()
                  if w.winfo_class() == "Label"]
        self.assertIn("Daily check-in", labels)
        self.assertEqual(self.store.login_streak(), 1)  # opened today

    def test_login_gold_shown_in_status(self):
        # setUp opened the app on a new day: 10 base + 2 streak bonus.
        self.assertEqual(self.app.status.get(), "Daily login: +12 gold.")
        self.assertEqual(self.store.gold, 12)

    def test_gold_tile_updates_on_completion(self):
        gid = self.store.add("A")
        self.app.set_progress(gid, 100)
        self.root.update()
        self.assertEqual(self.app.stat_labels["GOLD"][0].cget("text"), "37")

    def test_level_up_panel_shows_gold(self):
        gid = self.store.add("A")
        self.store.add("B")
        self.app.set_progress(gid, 100)
        self.app.set_progress(self.store.goals[1]["id"], 100)
        self.root.update()
        self.assertIsNotNone(self.app.level_overlay)
        texts = [w.cget("text") for w in self.app.level_overlay.winfo_children()
                 if w.winfo_class() == "Label"]
        self.assertIn("+25 gold", texts)  # 25 x level 1


if __name__ == "__main__":
    unittest.main()
