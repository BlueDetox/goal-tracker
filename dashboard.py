"""Goal Tracker V5 desktop app. Run with Python 3.10+; no pip packages needed.

What V5 changes over V4 (storage is untouched -- V4 saves open as-is):
- The progress slider runs 0-100. Dragging it all the way completes the goal.
  Double XP is impossible: the backend's per-goal xp_awarded flag guards it.
- Level-ups show a non-blocking in-window celebration panel instead of a
  modal dialog that froze the whole app until dismissed.
- Escape only clears the search box when the search box is focused. It no
  longer wipes your search while you're typing a new goal elsewhere.
- Tabs show live counts: "Active (3)".
- Cards get a status accent strip (mint = active, gold = completed/unlocked).
- A Stats tab graphs completions per day over the last 14 days, drawn on a
  plain canvas (no new dependencies), with day-streak and best-day tiles,
  plus a daily check-in strip tracking the app-open streak.
  The backend records a completion event (goal id, title, UTC time) the first
  time a goal hits 100%; it survives goal deletion, so the graph reflects
  what actually happened. Completions that predate this event log have no
  timestamp and are not backdated -- the graph starts recording today.
- A gold economy: opening the app on a new day, completing goals, and
  leveling up all earn gold, shown in the header.
"""
import argparse
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from goal_tracker import DATA_FILE, GoalStore, LEVEL_REWARDS, StorageError, XP_PER_LEVEL

BG = "#10151f"
PANEL = "#192230"
PANEL_DEEP = "#141b29"
FIELD = "#111925"
BORDER = "#2b394b"
TEXT = "#ecf2fa"
MUTED = "#9cacc0"
MINT = "#80edba"
GOLD = "#f5d485"
RED = "#ff9a9f"
FONT = "Segoe UI"


class ProgressSlider(tk.Canvas):
    """Keyboard-accessible 0-100 slider; dragging to 100 completes the goal.

    Reaching 100 cannot award XP twice: the backend records xp_awarded per
    goal, so a second trip to 100 (after a reopen) is a no-op for XP.
    """
    def __init__(self, parent, command):
        super().__init__(parent, height=32, width=180, bg=PANEL, highlightthickness=0,
                         takefocus=True, cursor="hand2")
        self.value = 0
        self.command = command
        self.bind("<Configure>", lambda event: self.draw())
        self.bind("<Button-1>", self.pointer)
        self.bind("<B1-Motion>", self.pointer)
        self.bind("<FocusIn>", lambda event: self.draw())
        self.bind("<FocusOut>", lambda event: self.draw())
        self.bind("<KeyPress>", self.key)

    def get(self):
        return self.value

    def set(self, value):
        self.value = max(0, min(100, int(value)))
        self.command(self.value)
        self.draw()

    def pointer(self, event):
        self.focus_set()
        self.set(round((event.x - 9) * 100 / max(1, self.winfo_width() - 18)))

    def key(self, event):
        if event.keysym in ("Left", "Down"):
            self.set(self.value - 1)
        elif event.keysym in ("Right", "Up"):
            self.set(self.value + 1)
        elif event.keysym == "Home":
            self.set(0)
        elif event.keysym == "End":
            self.set(100)
        else:
            return
        return "break"

    def draw(self):
        self.delete("all")
        width = max(18, self.winfo_width())
        x = 9 + (width - 18) * self.value / 100
        self.create_line(9, 16, width - 9, 16, fill=BORDER, width=6, capstyle="round")
        if self.value:
            self.create_line(9, 16, x, 16, fill=MINT, width=6, capstyle="round")
        self.create_oval(x - 7, 9, x + 7, 23, fill=MINT, outline=MINT)
        if self.focus_get() is self:
            self.create_rectangle(1, 1, width - 1, 31, outline=MUTED, dash=(2, 3))


class Dashboard:
    def __init__(self, root, store):
        self.root, self.store = root, store
        # Daily check-in: opening the app marks today down and earns gold.
        # Best-effort — a stale-save clash here must never block startup.
        try:
            login_gold = self.store.record_login()
        except StorageError:
            login_gold = 0
        self.login_gold = login_gold
        self.mode = "active"
        self.cards = {}
        self.search_job = None
        self.level_overlay = None
        self.tab_names = {"active": "Active", "completed": "Completed",
                          "all": "All goals", "rewards": "Rewards", "stats": "Stats"}
        self.root.title("Goal Tracker · V5.3")
        self.root.geometry("1040x860")
        self.root.minsize(860, 700)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.option_add("*Font", (FONT, 10))
        self.search = tk.StringVar()
        self.sort = tk.StringVar(value="Newest")
        self.new_title = tk.StringVar()
        self.status = tk.StringVar(value=f"Daily login: +{self.login_gold} gold."
            if self.login_gold else "Your changes save automatically.")
        self._style()
        self._build()
        self.refresh(reset=True)
        self.search.trace_add("write", self.schedule_search)
        self.root.bind("<Control-f>", lambda event: self.focus_search())
        self.root.bind("<Control-n>", lambda event: self.focus_new())
        self.root.bind("<Control-z>", self.undo_shortcut)
        self.root.bind("<Escape>", self.escape)
        for key, mode in zip("12345", ("active", "completed", "all", "rewards", "stats")):
            self.root.bind(f"<Alt-Key-{key}>", lambda event, m=mode: self.select_mode(m))
        self.root.bind("<MouseWheel>", self.wheel)
        self.root.bind("<Button-4>", lambda event: self.scroll(-1))
        self.root.bind("<Button-5>", lambda event: self.scroll(1))
        if store.warning:
            self.status.set("Save upgraded safely. Original copy is in the backups folder.")
            self.root.after(200, lambda: messagebox.showinfo("Save recovery / upgrade", store.warning, parent=root))

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=FIELD, background=BORDER,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", TEXT)])
        style.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED)
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

    def label(self, parent, text="", size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=(FONT, size, "bold" if bold else "normal"), **kwargs)

    def button(self, parent, text, command, accent=False, danger=False):
        return tk.Button(parent, text=text, command=command, relief="flat", bd=0,
            bg=MINT if accent else BORDER, fg=BG if accent else RED if danger else TEXT,
            activebackground="#a9f5d0" if accent else "#3b4c62", activeforeground=BG if accent else TEXT,
            font=(FONT, 10, "bold"), padx=13, pady=8, cursor="hand2",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=MINT,
            disabledforeground=MUTED)

    def entry(self, parent, variable):
        return tk.Entry(parent, textvariable=variable, bg=FIELD, fg=TEXT, insertbackground=MINT,
            relief="flat", font=(FONT, 12), highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=MINT, selectbackground="#355b50")

    def _build(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=28, pady=(18, 16))
        self.outer = outer
        top = tk.Frame(outer, bg=BG)
        top.pack(fill="x")
        self.label(top, "GOAL TRACKER", 11, MINT, True).pack(side="left")
        self.button(top, "Export", self.export).pack(side="right", padx=(8, 0))
        self.button(top, "Reload", self.reload).pack(side="right", padx=(8, 0))
        self.undo_button = self.button(top, "Undo", self.undo)
        self.undo_button.pack(side="right")

        hero = tk.Frame(outer, bg=BG)
        hero.pack(fill="x", pady=(12, 14))
        self.label(hero, "Small steps. Real progress.", 22, TEXT, True).pack(anchor="w")

        stats = tk.Frame(outer, bg=BG)
        stats.pack(fill="x", pady=(0, 14))
        stats.columnconfigure((0, 1, 2, 3), weight=1, uniform="stat")
        self.stat_labels = {}
        for col, name in enumerate(("IN PROGRESS", "COMPLETED", "LIFETIME XP", "GOLD")):
            card = tk.Frame(stats, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0 if col == 3 else 6))
            self.label(card, name, 9, MUTED, True).pack(anchor="w", padx=18, pady=(10, 1))
            value = self.label(card, "0", 21, GOLD if name == "GOLD" else (MINT if col == 2 else TEXT), True)
            value.pack(anchor="w", padx=18)
            detail = self.label(card, "", 9, MUTED)
            detail.pack(anchor="w", padx=18, pady=(1, 10))
            self.stat_labels[name] = (value, detail)

        level_row = tk.Frame(outer, bg=BG)
        level_row.pack(fill="x", pady=(0, 13))
        self.level_label = self.label(level_row, "", 10, GOLD, True)
        self.level_label.pack(side="left", padx=(0, 14))
        self.level_canvas = tk.Canvas(level_row, height=8, bg=BG, highlightthickness=0)
        self.level_canvas.pack(side="left", fill="x", expand=True)
        self.level_canvas.bind("<Configure>", lambda event: self.draw_level())
        self.level_detail = self.label(level_row, "", 9, MUTED)
        self.level_detail.pack(side="left", padx=(14, 0))

        nav = tk.Frame(outer, bg=BG)
        nav.pack(fill="x")
        self.tabs = {}
        for mode in ("active", "completed", "all", "rewards", "stats"):
            button = self.button(nav, self.tab_names[mode], lambda m=mode: self.select_mode(m))
            button.pack(side="left", padx=(0, 7))
            self.tabs[mode] = button
        self.count_label = self.label(nav, "", 10, MUTED)
        self.count_label.pack(side="right")

        tools = tk.Frame(outer, bg=BG)
        tools.pack(fill="x", pady=(10, 10))
        self.label(tools, "Search", 10, MUTED).pack(side="left", padx=(0, 10))
        self.search_entry = self.entry(tools, self.search)
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.button(tools, "Clear", lambda: self.search.set("")).pack(side="left", padx=(7, 16))
        self.sort_box = ttk.Combobox(tools, textvariable=self.sort, values=("Newest", "Progress", "Title"), state="readonly", width=11)
        self.sort_box.pack(side="right")
        self.sort_box.bind("<<ComboboxSelected>>", lambda event: self.refresh(reset=True))

        list_area = tk.Frame(outer, bg=BG)
        self.list_area = list_area
        self.canvas = tk.Canvas(list_area, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", padx=(8, 0))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.list_window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.resize_list)

        footer = tk.Frame(outer, bg=BG)
        footer.pack(side="bottom", fill="x")
        add_panel = tk.Frame(footer, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        add_panel.pack(fill="x", pady=(14, 9))
        self.label(add_panel, "WHAT'S YOUR NEXT GOAL?", 9, MUTED, True).pack(anchor="w", padx=14, pady=(12, 8))
        add_row = tk.Frame(add_panel, bg=PANEL)
        add_row.pack(fill="x", padx=14, pady=(0, 14))
        self.new_entry = self.entry(add_row, self.new_title)
        self.new_entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 12))
        self.new_entry.bind("<Return>", lambda event: self.add_goal())
        self.add_button = self.button(add_row, "+ Add goal", self.add_goal, accent=True)
        self.add_button.pack(side="right")
        self.status_label = tk.Label(footer, textvariable=self.status, bg=BG, fg=MUTED,
            font=(FONT, 9), anchor="w", wraplength=900, justify="left")
        self.status_label.pack(fill="x")
        self.label(footer, "Ctrl+N  New goal     Ctrl+F  Search     Alt+1–5  Views     Ctrl+Z  Undo     Esc  Clear search", 9, MUTED).pack(anchor="w", pady=(5, 0))
        list_area.pack(fill="both", expand=True)

    def draw_level(self):
        width = self.level_canvas.winfo_width()
        self.level_canvas.delete("all")
        self.level_canvas.create_rectangle(0, 0, width, 8, fill=BORDER, outline="")
        fill = width * (self.store.total_xp % XP_PER_LEVEL) / XP_PER_LEVEL
        if fill:
            self.level_canvas.create_rectangle(0, 0, fill, 8, fill=MINT, outline="")

    def resize_list(self, event):
        self.canvas.itemconfigure(self.list_window, width=event.width)
        for card in self.cards.values():
            card["title"].configure(wraplength=max(200, event.width - 60))

    def refresh(self, reset=False):
        old_position = self.canvas.yview()[0]
        goals = self.store.goals
        done = sum(g["completed"] for g in goals)
        active = len(goals) - done
        counts = {"active": active, "completed": done, "all": len(goals),
                  "rewards": len(self.store.rewards)}
        self.stat_labels["IN PROGRESS"][0].configure(text=str(active))
        self.stat_labels["IN PROGRESS"][1].configure(text="Ready for your next step")
        self.stat_labels["COMPLETED"][0].configure(text=str(done))
        self.stat_labels["COMPLETED"][1].configure(text=f"Of {len(goals)} saved goals")
        self.stat_labels["LIFETIME XP"][0].configure(text=f"{self.store.total_xp:,}")
        self.stat_labels["LIFETIME XP"][1].configure(text="+50 XP per first completion")
        self.stat_labels["GOLD"][0].configure(text=f"{self.store.gold:,}")
        self.stat_labels["GOLD"][1].configure(text="Daily login · goals · level-ups")
        level, xp = divmod(self.store.total_xp, XP_PER_LEVEL)
        self.level_label.configure(text=f"LEVEL {level}")
        self.level_detail.configure(text=f"{xp} / {XP_PER_LEVEL} XP to level {level + 1}")
        self.draw_level()
        self.undo_button.configure(state="normal" if self.store.can_undo else "disabled")
        for mode, button in self.tabs.items():
            label = self.tab_names[mode]
            if mode != "stats":
                label = f"{label} ({counts[mode]})"
            button.configure(bg=MINT if mode == self.mode else BORDER,
                             fg=BG if mode == self.mode else TEXT,
                             text=label)
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.cards.clear()
        self.sort_box.configure(state="disabled" if self.mode in ("rewards", "stats") else "readonly")
        if self.mode == "rewards":
            self.render_rewards()
        elif self.mode == "stats":
            self.render_stats()
        else:
            items = self.store.query(self.mode, self.search.get(), self.sort.get())
            self.count_label.configure(text=f"{len(items)} goal{'s' if len(items) != 1 else ''}")
            if not items:
                self.empty_state("No matching goals" if self.search.get().strip() else
                    "A fresh start" if self.mode == "active" else "Nothing here yet",
                    "Try a different search or view." if self.search.get().strip() else
                    "Add a goal below. Your next small step starts here." if self.mode != "completed" else
                    "Complete an active goal and it will appear here.")
            for goal in items:
                self.render_goal(goal)
        self.root.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(0 if reset else old_position)

    def empty_state(self, title, detail):
        panel = tk.Frame(self.list_frame, bg=PANEL)
        panel.pack(fill="x", pady=5)
        self.label(panel, title, 18, TEXT, True).pack(pady=(34, 8))
        self.label(panel, detail, 10, MUTED).pack(pady=(0, 34))

    def _card_shell(self, accent_color):
        """Card frame with a 3px status accent strip on the left edge."""
        card = tk.Frame(self.list_frame, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", pady=(0, 10))
        strip = tk.Frame(card, bg=accent_color, width=3)
        strip.pack(side="left", fill="y")
        strip.pack_propagate(False)
        content = tk.Frame(card, bg=PANEL)
        content.pack(side="left", fill="both", expand=True)
        return content

    def render_goal(self, goal):
        gid = goal["id"]
        content = self._card_shell(GOLD if goal["completed"] else MINT)
        title = self.label(content, goal["title"], 13, MUTED if goal["completed"] else TEXT, True,
            anchor="w", justify="left", wraplength=max(200, self.canvas.winfo_width() - 60))
        title.pack(fill="x", padx=17, pady=(10, 3))
        body = tk.Frame(content, bg=PANEL)
        body.pack(fill="x", padx=17, pady=(0, 10))
        progress_text = self.label(body, f"{goal['progress']}%", 10, MINT, True, width=5)
        progress_text.pack(side="left", padx=(0, 8))
        controls = tk.Frame(body, bg=PANEL)
        controls.pack(side="right", padx=(14, 0))
        self.button(controls, "Edit", lambda: self.edit_goal(gid)).pack(side="left", padx=(0, 6))
        complete = self.button(controls, "Reopen" if goal["completed"] else "Complete",
            lambda: self.set_progress(gid, 0 if goal["completed"] else 100), accent=not goal["completed"])
        complete.pack(side="left", padx=(0, 6))
        self.button(controls, "Delete", lambda: self.delete_goal(gid), danger=True).pack(side="left")
        if goal["completed"]:
            self.label(body, "COMPLETED  ·  XP earned", 9, GOLD).pack(side="left", fill="x", expand=True)
            scale = None
        else:
            scale = ProgressSlider(body,
                command=lambda value: progress_text.configure(text=f"{value}%"))
            scale.set(goal["progress"])
            scale.pack(side="left", fill="x", expand=True)
            scale.bind("<ButtonRelease-1>", lambda event: self.set_progress(gid, scale.get()))
            scale.bind("<KeyRelease>", lambda event: self.set_progress(gid, scale.get())
                if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End") else None)
        self.cards[gid] = {"title": title, "scale": scale, "complete": complete}

    def render_rewards(self):
        earned = {r["level"]: r for r in self.store.rewards}
        self.count_label.configure(text=f"{len(earned)} / {len(LEVEL_REWARDS)} earned")
        shown = 0
        for level, title in LEVEL_REWARDS.items():
            if self.search.get().strip().casefold() not in f"{title} level {level}".casefold():
                continue
            shown += 1
            unlocked = level in earned
            content = self._card_shell(GOLD if unlocked else BORDER)
            self.label(content, title, 15, GOLD if unlocked else MUTED, True).pack(anchor="w", padx=18, pady=(15, 5))
            date = earned.get(level, {}).get("earned_at", "")[:10]
            detail = f"Level {level}  ·  Earned" + (f" {date}" if date else "") if unlocked else f"Level {level}  ·  {max(0, level * XP_PER_LEVEL - self.store.total_xp):,} XP to unlock"
            self.label(content, detail, 10, MUTED).pack(anchor="w", padx=18, pady=(0, 16))
        if not shown:
            self.empty_state("No matching rewards", "Try a different search.")

    # ------------------------------------------------------------------
    # Stats: completions-per-day bar chart drawn on a plain canvas, so the
    # app keeps its zero-dependency rule (no matplotlib).
    # ------------------------------------------------------------------
    def render_stats(self):
        days = self.store.completions_by_day(14)
        total = sum(count for _, count in days)
        streak = self.store.completion_streak()
        self.count_label.configure(text=f"{total} completion{'s' if total != 1 else ''} · last 14 days")
        self.label(self.list_frame, "Completions per day", 15, TEXT, True).pack(anchor="w", pady=(4, 8))
        chart = tk.Canvas(self.list_frame, bg=PANEL, highlightthickness=1,
                          highlightbackground=BORDER, height=260)
        chart.pack(fill="x", pady=(0, 12))
        chart.bind("<Configure>", lambda event: self._draw_chart(chart, days))
        self._draw_chart(chart, days)

        tiles = tk.Frame(self.list_frame, bg=BG)
        tiles.pack(fill="x")
        for col in range(3):
            tiles.columnconfigure(col, weight=1, uniform="stats")
        best_day, best_count = max(days, key=lambda pair: pair[1])
        stats = (
            ("DAY STREAK", str(streak), "days in a row with a completion"),
            ("LAST 14 DAYS", str(total), "goals completed"),
            ("BEST DAY", str(best_count),
             best_day.strftime("%a, %b %d") if best_count else "no completions yet"),
        )
        for col, (name, value, detail) in enumerate(stats):
            card = tk.Frame(tiles, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0 if col == 2 else 6))
            self.label(card, name, 9, MUTED, True).pack(anchor="w", padx=18, pady=(12, 2))
            self.label(card, value, 22, MINT, True).pack(anchor="w", padx=18)
            self.label(card, detail, 9, MUTED).pack(anchor="w", padx=18, pady=(2, 12))
        if not total:
            self.label(self.list_frame,
                "The graph starts recording today — finish a goal and it will show up here.",
                10, MUTED).pack(anchor="w", pady=(10, 0))

        # Daily check-in: opening the app marks the day down. Dots show
        # the last 14 days; tiles show the streaks.
        logins = self.store.logins_by_day(14)
        opened_14 = sum(1 for _, opened in logins if opened)
        self.label(self.list_frame, "Daily check-in", 15, TEXT, True).pack(
            anchor="w", pady=(22, 8))
        strip = tk.Canvas(self.list_frame, bg=PANEL, highlightthickness=1,
                          highlightbackground=BORDER, height=96)
        strip.pack(fill="x", pady=(0, 12))
        strip.bind("<Configure>", lambda event: self._draw_login_strip(strip, logins))
        self._draw_login_strip(strip, logins)

        login_tiles = tk.Frame(self.list_frame, bg=BG)
        login_tiles.pack(fill="x")
        for col in range(3):
            login_tiles.columnconfigure(col, weight=1, uniform="loginstats")
        for col, (name, value, detail) in enumerate((
            ("LOGIN STREAK", str(self.store.login_streak()), "days opened in a row"),
            ("BEST STREAK", str(self.store.longest_login_streak()), "longest run ever"),
            ("OPENED · 14 DAYS", str(opened_14), "days the app was opened"),
        )):
            card = tk.Frame(login_tiles, bg=PANEL, highlightthickness=1,
                            highlightbackground=BORDER)
            card.grid(row=0, column=col, sticky="ew",
                      padx=(0 if col == 0 else 6, 0 if col == 2 else 6))
            self.label(card, name, 9, MUTED, True).pack(anchor="w", padx=18, pady=(12, 2))
            self.label(card, value, 22, MINT, True).pack(anchor="w", padx=18)
            self.label(card, detail, 9, MUTED).pack(anchor="w", padx=18, pady=(2, 12))

    def _draw_chart(self, chart, days):
        chart.delete("all")
        width = chart.winfo_width()
        height = int(chart.cget("height"))
        if width < 60:
            return
        pad_l, pad_r, pad_t, pad_b = 30, 14, 20, 34
        plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
        max_count = max([count for _, count in days] + [1])
        for level in range(max_count + 1):
            y = pad_t + plot_h - (level / max_count) * plot_h
            chart.create_line(pad_l, y, width - pad_r, y, fill=BORDER, dash=(2, 4))
            chart.create_text(pad_l - 6, y, text=str(level), anchor="e",
                              fill=MUTED, font=(FONT, 8))
        slot = plot_w / len(days)
        bar_w = min(36, slot * 0.55)
        prev_month = None
        for i, (day, count) in enumerate(days):
            center = pad_l + slot * i + slot / 2
            bar_h = (count / max_count) * plot_h
            top = pad_t + plot_h - bar_h
            chart.create_rectangle(center - bar_w / 2, top, center + bar_w / 2,
                pad_t + plot_h, fill=MINT if count else "#233043",
                outline=GOLD if i == len(days) - 1 else "", width=2)
            if count:
                chart.create_text(center, top - 11, text=str(count), fill=TEXT,
                                  font=(FONT, 9, "bold"))
            month = day.strftime("%b")
            label = f"{month} {day.day}" if i == 0 or month != prev_month else str(day.day)
            prev_month = month
            chart.create_text(center, height - pad_b + 12, text=label, fill=MUTED,
                              font=(FONT, 8))

    def _draw_login_strip(self, strip, logins):
        """14-day dot strip: filled dot = app opened that day, gold ring = today."""
        strip.delete("all")
        width = strip.winfo_width()
        if width < 60:
            return
        height = int(strip.cget("height"))
        slot = width / len(logins)
        for i, (day, opened) in enumerate(logins):
            x = slot * i + slot / 2
            y = 30
            radius = 13
            is_today = day == date.today()
            strip.create_oval(x - radius, y - radius, x + radius, y + radius,
                fill=MINT if opened else "#233043",
                outline=GOLD if is_today else (MINT if opened else BORDER),
                width=2 if is_today else 1)
            if opened:
                strip.create_text(x, y, text="\u2713", fill="#0b0f14",
                                  font=(FONT, 11, "bold"))
            strip.create_text(x, y + 26, text=str(day.day), fill=MUTED, font=(FONT, 8))
            if i == 0 or day.month != logins[i - 1][0].month:
                strip.create_text(x, y + 40, text=day.strftime("%b"),
                                  fill=MUTED, font=(FONT, 8))

    # ------------------------------------------------------------------
    # Level-up celebration: an in-window panel, never a blocking dialog.
    # ------------------------------------------------------------------
    def show_level_up(self, level, new_rewards, gold_earned=0):
        self.dismiss_level_up()
        panel = tk.Frame(self.outer, bg=PANEL_DEEP, highlightthickness=2, highlightbackground=GOLD)
        self.label(panel, "★  ★  ★", 12, GOLD, True).pack(pady=(18, 4))
        self.label(panel, "LEVEL UP!", 22, GOLD, True).pack()
        self.label(panel, f"You reached level {level}.", 11, TEXT).pack(pady=(4, 10))
        if gold_earned:
            self.label(panel, f"+{gold_earned} gold", 14, GOLD, True).pack(pady=(0, 10))
        for title in new_rewards:
            row = tk.Frame(panel, bg="#2a2415", highlightthickness=1, highlightbackground=GOLD)
            row.pack(fill="x", padx=28, pady=(0, 8))
            self.label(row, f"★  {title}", 12, GOLD, True).pack(padx=14, pady=8)
        self.label(panel, "New reward cards are waiting in the Rewards tab.", 9, MUTED).pack(pady=(2, 10))
        self.button(panel, "Continue", self.dismiss_level_up, accent=True).pack(pady=(0, 18))
        panel.place(relx=0.5, rely=0.38, anchor="center")
        self.level_overlay = panel

    def dismiss_level_up(self):
        if self.level_overlay is not None:
            self.level_overlay.destroy()
            self.level_overlay = None

    def run_change(self, action, success):
        self.dismiss_level_up()
        before = self.store.total_xp // XP_PER_LEVEL
        previous_rewards = {r["level"] for r in self.store.rewards}
        try:
            action()
        except (ValueError, StorageError) as exc:
            self.status.set(str(exc))
            messagebox.showerror("Change not saved", str(exc), parent=self.root)
            self.refresh()
            return False
        self.status.set(success)
        self.refresh()
        after = self.store.total_xp // XP_PER_LEVEL
        if after > before:
            cards = [r["title"] for r in self.store.rewards if r["level"] not in previous_rewards]
            level_gold = sum(self.store.level_gold(level) for level in range(before + 1, after + 1))
            self.show_level_up(after, cards, level_gold)
        return True

    def add_goal(self):
        if self.run_change(lambda: self.store.add(self.new_title.get()), "Goal added and saved."):
            self.new_title.set("")
            self.search.set("")
            self.select_mode("active")
            self.new_entry.focus_set()

    def edit_goal(self, gid):
        goal = next(g for g in self.store.goals if g["id"] == gid)
        title = simpledialog.askstring("Edit goal", "Goal title (up to 200 characters):",
            initialvalue=goal["title"], parent=self.root)
        if title is not None:
            self.run_change(lambda: self.store.rename(gid, title), "Goal renamed and saved.")

    def set_progress(self, gid, value):
        keyboard_focus = self.root.focus_get() is self.cards.get(gid, {}).get("scale")
        gold_before = self.store.gold
        ok = self.run_change(lambda: self.store.set_progress(gid, value),
            "Goal completed. Your progress is saved." if value == 100 else "Progress saved.")
        if ok and value == 100:
            earned = self.store.gold - gold_before
            if earned:
                self.status.set(f"Goal completed. +{earned} gold. Your progress is saved.")
        if keyboard_focus and gid in self.cards and self.cards[gid]["scale"] is not None:
            self.cards[gid]["scale"].focus_set()

    def delete_goal(self, gid):
        self.run_change(lambda: self.store.delete(gid), "Goal deleted. Lifetime XP stays. Use Undo to restore it.")

    def undo(self):
        if self.store.can_undo:
            self.run_change(self.store.undo, "Last change undone and saved.")

    def undo_shortcut(self, event):
        if not isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox)):
            self.undo()
            return "break"

    def reload(self):
        self.dismiss_level_up()
        try:
            self.store.load()
        except StorageError as exc:
            messagebox.showerror("Cannot reload", str(exc), parent=self.root)
            return
        self.refresh(reset=True)
        self.status.set(self.store.warning or "Reloaded from disk. Undo history cleared.")

    def export(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="Export a backup copy",
            initialfile="goal-tracker-export.json", defaultextension=".json", filetypes=[("JSON backup", "*.json")])
        if path:
            try:
                self.store.export(path)
            except (ValueError, StorageError) as exc:
                messagebox.showerror("Cannot export", str(exc), parent=self.root)
            else:
                self.status.set(f"Exported to {path}")

    def select_mode(self, mode):
        self.mode = mode
        self.refresh(reset=True)

    def schedule_search(self, *args):
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(150, self.search_now)

    def search_now(self):
        # Safe to call directly (tests do): a pending debounce timer is
        # cancelled first so it can never fire orphaned after teardown.
        if self.search_job:
            self.root.after_cancel(self.search_job)
            self.search_job = None
        self.refresh(reset=True)

    def focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")
        return "break"

    def focus_new(self):
        self.new_entry.focus_set()
        return "break"

    def escape(self, event):
        # Escape is context-aware: close the celebration first, clear the
        # search box only when it has focus, otherwise just defocus.
        if self.level_overlay is not None:
            self.dismiss_level_up()
        elif self.root.focus_get() == self.search_entry and self.search.get():
            self.search.set("")
        else:
            self.root.focus_set()
        return "break"

    def scroll(self, amount):
        if self.canvas.bbox("all") and self.canvas.bbox("all")[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(amount, "units")

    def wheel(self, event):
        if not isinstance(event.widget, (ProgressSlider, ttk.Combobox)):
            self.scroll(-int(event.delta / 120) if abs(event.delta) >= 120 else -1 if event.delta > 0 else 1)

    def close(self):
        self.dismiss_level_up()
        if self.new_title.get().strip() and not messagebox.askyesno("Unsaved goal title",
                "The title in the new-goal box has not been added. Close without adding it?", parent=self.root):
            return
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Goal Tracker V5")
    parser.add_argument("--data", type=Path, default=DATA_FILE, help="Optional alternate save file")
    args = parser.parse_args()
    root = tk.Tk()
    root.withdraw()
    store = GoalStore(args.data)
    try:
        store.load()
    except StorageError as exc:
        messagebox.showerror("Cannot open Goal Tracker", str(exc), parent=root)
        root.destroy()
        return
    root.deiconify()
    Dashboard(root, store)
    root.mainloop()


if __name__ == "__main__":
    main()
