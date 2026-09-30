# Goal Tracker V5

A desktop goal tracker with lifetime XP, reward cards, safe saves, and undo. V5 keeps V4's storage engine exactly as-is and upgrades the experience layer.

## Start

1. Double-click **Start Goal Tracker.cmd** on Windows, or run `python dashboard.py`.

Requires Python 3.10+ with Tkinter (included in standard Windows Python). No pip packages needed. V4 save files open as-is; nothing is migrated or rewritten on first launch beyond V4's own upgrade path.

## What's new in V5

- **Drag to complete.** The progress slider runs 0–100 — dragging it all the way finishes the goal. Double XP is impossible: the backend's per-goal `xp_awarded` flag guards it, so reopen → complete again awards nothing extra.
- **Level-ups don't freeze the app anymore.** The blocking "Level up!" dialog is replaced by an in-window gold celebration panel. Dismiss it with Continue, Escape, or just keep working — your next change replaces it.
- **Escape behaves.** It closes the celebration first, clears the search box only when the search box is focused, and otherwise just defocuses. It no longer wipes your search while you're typing a new goal.
- **Tabs show live counts** — "Active (3)", "Rewards (1)".
- **Status accent strips** on cards: mint for active goals, gold for completed ones and unlocked reward cards.
- **Stats tab.** A bar chart of completions per day over the last 14 days, drawn with plain Tkinter (still zero dependencies), plus day-streak, 14-day total, and best-day tiles. **Alt+5** jumps to it. Note: the graph starts recording on first launch — completions from before this update have no timestamp and are not backdated (their XP is untouched).

## Everyday use

- Add a title at the bottom and press Enter or **+ Add goal** (up to 200 characters).
- Drag a slider to record progress; release to save. Drag to 100% to complete — each goal earns 50 XP on its first completion only.
- **Complete** finishes a goal instantly; **Reopen** returns it to 0% (keeps its earned XP).
- **Edit** renames, **Delete** removes the goal but keeps its completion in the Stats history (lifetime XP and rewards stay too).
- **Undo** reverses the most recent saved change, including its XP/reward effects — 30 deep, this session only.
- **Active / Completed / All goals / Rewards / Stats** tabs, search with live filtering, sort by newest / progress / title.
- Rewards unlock at levels 1, 3, 5, 10, 20, 50 (100 XP per level). Locked cards show the XP remaining.
- **Gold economy.** Opening the app on a new day earns 10 gold + 2 per consecutive login day (bonus capped at +20). Completing a goal earns 25 gold (once-only, like XP). Leveling up earns 25 × the new level. Your gold shows in the header. Old saves start at 0 gold — nothing is backdated.
- **Export** saves a separate JSON backup. **Reload** picks up changes made outside the window.

Keyboard: **Ctrl+N** new goal, **Ctrl+F** search, **Alt+1–5** views, **Ctrl+Z** undo (outside text fields), **Esc** context-aware, arrows/Home/End on a focused slider. Slider keyboard: release an arrow key to save.

## Saves and recovery

`goals_data.json` lives beside `goal_tracker.py` regardless of launch directory (`python dashboard.py --data "C:\path\goals_data.json"` overrides it).

- Atomic writes (temp file → flush → replace), previous save kept as `goals_data.json.bak`.
- Changes reach memory only after the save succeeds; failures show an error and keep prior state.
- Corrupt/migrated saves preserve the original bytes in `backups/` before repair.
- App windows cooperate through an OS file lock; a stale window must Reload before editing.
- Do not edit the save in a text editor mid-save.

## Verification

Run `python -m unittest test_goal_tracker test_dashboard` (UI tests need a desktop session; dialogs are mocked).

31 backend tests + 16 UI tests covering: migration, damaged saves, duplicate IDs, failed writes, stale windows, locking, lifetime XP, the xp_awarded once-only guarantee, undo, export, search, sorting, keyboard controls, slider-to-100 completion, the non-modal level-up panel, context-aware Escape, tab counts, and unsaved-input retention.

V5.1 adds 12 backend tests + 2 UI tests for the completion event log (43 backend + 18 UI total): event recording on first completion, withdrawal on reopen, survival of deletion and reload, rename propagation, undo integration, per-day bucketing, streak counting, and rejection of malformed events.

V5.2 adds a **daily check-in streak**: opening the app marks the day down (one entry per day, local date; never creates an undo step). The Stats tab gains a 14-day dot strip plus login-streak, best-streak, and opened-days tiles.

V5.3 adds a **gold economy** (12 backend + 3 UI tests, 67 backend + 23 UI total): daily login gold with a capped streak bonus, per-goal gold on first completion, scaled level-up gold, a header gold tile, gold in the level-up celebration, and clean undo behavior (gold restores with the snapshot; logins never enter undo).

Files: `dashboard.py` (UI), `goal_tracker.py` (storage), `test_*.py` (tests), `Start_Goal_Tracker.cmd` (Windows launcher).
