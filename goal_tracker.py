"""Goal Tracker storage (V5). No filesystem or UI side effects on import.

V5 keeps the V4 storage design unchanged: the format, schema version, locking,
migration, and undo semantics are identical, so V4 saves open as-is.
The V5 improvements live in the UI layer (dashboard.py). V5.1 adds a
completion event log (goal id, title, UTC timestamp) recorded when a goal
first reaches 100%; it survives deletion and drives the Stats tab graph.
Completions that predate the log have no event and are not backdated.
V5.2 adds a daily login history (local ISO dates, one per day the app is
opened) driving the Stats tab check-in streak. Older saves start empty.
V5.3 adds a gold economy: opening the app on a new day, completing goals,
and leveling up all earn gold (see GOLD_* constants). Older saves start at
0 gold; nothing is backdated."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import uuid

XP_PER_GOAL = 50
XP_PER_LEVEL = 100
SCHEMA_VERSION = 4
DATA_FILE = Path(__file__).with_name("goals_data.json")
LEVEL_REWARDS = {1: "First Steps", 3: "Goal Novice", 5: "Goal Adept",
                 10: "Goal Veteran", 20: "Goal Master", 50: "Goal Legend"}
# Gold economy (V5.3). All amounts are ints; all awards are once-only and
# never backdated — a migrated save starts at 0 gold.
GOLD_PER_LOGIN = 10        # base gold for opening the app on a new day
GOLD_STREAK_BONUS = 2     # extra gold per consecutive login day
GOLD_STREAK_BONUS_CAP = 20  # the streak bonus never exceeds this
GOLD_PER_GOAL = 25        # completing a goal (same once-only guard as XP)
GOLD_PER_LEVEL = 25       # leveling up awards 25 × the new level reached


class StorageError(Exception):
    """A safe-to-display persistence error."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def title_value(value):
    if not isinstance(value, str):
        raise ValueError("Goal titles must be text.")
    value = " ".join(value.split())
    if not value:
        raise ValueError("Enter a goal title first.")
    if len(value) > 200:
        raise ValueError("Keep goal titles to 200 characters or fewer.")
    return value


def progress_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Progress must be a finite number.")
    return max(0, min(100, int(value)))


def fingerprint(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def atomic_write(path, raw):
    """Flush a unique temporary file and atomically replace the destination."""
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


class GoalStore:
    def __init__(self, path=DATA_FILE):
        self.path = Path(path).resolve()
        self.data = self.empty()
        self.warning = ""
        self._fingerprint = None
        self._history = []

    @staticmethod
    def empty():
        return {"schema_version": SCHEMA_VERSION, "total_xp": 0, "goals": [],
                "rewards": [], "completion_events": [], "login_history": [],
                "gold": 0}

    @property
    def goals(self):
        return deepcopy(self.data["goals"])

    @property
    def rewards(self):
        return deepcopy(self.data["rewards"])

    @property
    def total_xp(self):
        return self.data["total_xp"]

    @property
    def gold(self):
        return self.data["gold"]

    @staticmethod
    def level_gold(level):
        """Gold awarded for reaching `level` (higher levels pay more)."""
        return GOLD_PER_LEVEL * level

    @property
    def can_undo(self):
        return bool(self._history)

    def completions_by_day(self, days=14):
        """[(date, count)] for the last `days` days, oldest first."""
        today = datetime.now().date()
        counts = {today - timedelta(days=days - 1 - i): 0 for i in range(days)}
        for event in self.data["completion_events"]:
            try:
                day = datetime.fromisoformat(event["at"]).astimezone().date()
            except (ValueError, KeyError, TypeError):
                continue
            if day in counts:
                counts[day] += 1
        return sorted(counts.items())

    def completion_streak(self):
        """Consecutive days with >= 1 completion, ending today or yesterday."""
        done = set()
        for event in self.data["completion_events"]:
            try:
                done.add(datetime.fromisoformat(event["at"]).astimezone().date())
            except (ValueError, KeyError, TypeError):
                continue
        today = datetime.now().date()
        day = today if today in done else today - timedelta(days=1)
        streak = 0
        while day in done:
            streak += 1
            day -= timedelta(days=1)
        return streak

    # ------------------------------------------------------------------
    # Daily login streak: one local-date entry per day the app is opened.
    # Recorded at startup by the dashboard; never part of undo history.
    # ------------------------------------------------------------------
    @staticmethod
    def _login_day(today):
        """Normalize `today` (None, date, or ISO string) to an ISO date."""
        if today is None:
            return date.today().isoformat()
        if isinstance(today, date):
            return today.isoformat()
        day = str(today)
        date.fromisoformat(day)  # raises ValueError on garbage
        return day

    def record_login(self, today=None):
        """Mark `today` as a day the app was opened and award gold for it:
        a base amount plus a streak bonus (capped). One entry per day —
        opening twice earns nothing more. Saves directly to disk without
        creating an undo step. Returns the gold earned (0 if already logged)."""
        day = self._login_day(today)
        if day in self.data.setdefault("login_history", []):
            return 0
        candidate = deepcopy(self.data)
        history = candidate.setdefault("login_history", [])
        history.append(day)
        history.sort()
        streak = self._streak_from(set(history), day)
        earned = GOLD_PER_LOGIN + min(GOLD_STREAK_BONUS * streak,
                                      GOLD_STREAK_BONUS_CAP)
        candidate["gold"] += earned
        self._save(candidate)
        self.data = candidate
        return earned

    def login_days(self):
        """Set of ISO local dates the app was opened."""
        return set(self.data.get("login_history", []))

    def logins_by_day(self, days=14):
        """[(date, opened)] for the last `days` days, oldest first."""
        today = date.today()
        opened = self.login_days()
        result = []
        for i in range(days):
            day = today - timedelta(days=days - 1 - i)
            result.append((day, day.isoformat() in opened))
        return result

    def login_streak(self, today=None):
        """Consecutive days the app was opened, ending today or yesterday."""
        return self._streak_from(self.login_days(), self._login_day(today))

    @staticmethod
    def _streak_from(opened, ref_iso):
        """Walk-back counter shared by login_streak and record_login."""
        ref = date.fromisoformat(ref_iso)
        day = ref if ref_iso in opened else ref - timedelta(days=1)
        streak = 0
        while day.isoformat() in opened:
            streak += 1
            day -= timedelta(days=1)
        return streak

    def longest_login_streak(self):
        """Longest unbroken run of opened days, ever."""
        best = current = 0
        prev = None
        for iso in sorted(self.login_days()):
            day = date.fromisoformat(iso)
            current = current + 1 if prev and day == prev + timedelta(days=1) else 1
            best = max(best, current)
            prev = day
        return best

    def _read(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _preserve(self, raw, reason):
        folder = self.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        backup = folder / f"{self.path.stem}-{reason}-{uuid.uuid4().hex}.json"
        atomic_write(backup, raw)
        return backup

    @contextmanager
    def _locked(self):
        """Serialize cooperating app instances; OS releases locks after crashes."""
        try:
            stream = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        except OSError as exc:
            raise StorageError(f"Cannot access the save folder: {exc}") from exc
        with stream:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StorageError("Another Goal Tracker window is saving. Try again shortly.") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def load(self):
        """Migrate old saves, preserving original bytes before any repair."""
        with self._locked():
            self._load()

    def _load(self):
        try:
            raw = self._read()
            warning = ""
            if raw is None:
                cleaned = self.empty()
            else:
                try:
                    original = json.loads(raw.decode("utf-8-sig"))
                    cleaned, repaired = self._normalize(original)
                except (ValueError, UnicodeError, TypeError, OverflowError):
                    backup = self._preserve(raw, "invalid")
                    cleaned, repaired = self.empty(), True
                    warning = f"Unreadable save preserved as {backup.name}. Starting empty."
                else:
                    if repaired:
                        backup = self._preserve(raw, "migration")
                        warning = f"Save upgraded or repaired. Original preserved as {backup.name}."
                if repaired:
                    # Refuse to replace data changed while the migration was running.
                    if self._read() != raw:
                        raise StorageError("The save changed during loading. Reload and try again.")
                    encoded = self._encode(cleaned)
                    atomic_write(self.path, encoded)
                    raw = encoded
            self.data = cleaned
            self.warning = warning
            self._fingerprint = fingerprint(raw)
            self._history.clear()
        except OSError as exc:
            raise StorageError(f"Cannot read or repair the save: {exc}") from exc

    @staticmethod
    def _award(data):
        earned = {r["level"] for r in data["rewards"]}
        for level, title in LEVEL_REWARDS.items():
            if level <= data["total_xp"] // XP_PER_LEVEL and level not in earned:
                data["rewards"].append({"level": level, "title": title,
                    "subtitle": f"Reach Level {level}", "earned_at": now()})

    @classmethod
    def _normalize(cls, original):
        if not isinstance(original, (dict, list)):
            raise ValueError("Expected a save object or legacy list")
        legacy_list = isinstance(original, list)
        source = {"goals": original} if legacy_list else original
        version = source.get("schema_version", 0)
        if isinstance(version, int) and version > SCHEMA_VERSION:
            raise StorageError("This save comes from a newer app. Use that version to open it.")
        rows = source.get("goals", [])
        if not isinstance(rows, list):
            raise ValueError("Invalid goal list")
        data = cls.empty()
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                title = title_value(row.get("title"))
            except ValueError:
                continue
            try:
                progress = progress_value(row.get("progress", 0))
            except ValueError:
                progress = 0
            gid = row.get("id")
            gid = gid.strip("'\" ") if isinstance(gid, str) else ""
            if not gid or gid in seen:
                gid = str(uuid.uuid4())
            seen.add(gid)
            # V4 supports reopening goals without earning their XP twice.
            # Earlier versions incorrectly marked unfinished goals as awarded.
            awarded = progress == 100 or (version == SCHEMA_VERSION and row.get("xp_awarded") is True)
            data["goals"].append({"id": gid, "title": title, "progress": progress,
                "completed": progress == 100, "xp_awarded": awarded,
                "created_at": row.get("created_at") if isinstance(row.get("created_at"), str) else now()})
        xp = source.get("total_xp", 0)
        if isinstance(xp, bool) or not isinstance(xp, (float, int)) or not math.isfinite(xp):
            xp = 0
        # Preserve lifetime XP (including deleted goals), with a floor for earned goals.
        data["total_xp"] = max(0, int(xp), XP_PER_GOAL * sum(g["xp_awarded"] for g in data["goals"]))
        reward_rows = source.get("rewards", [])
        earned = set()
        if isinstance(reward_rows, list):
            for row in reward_rows:
                if not isinstance(row, dict):
                    continue
                level = row.get("level")
                if type(level) is not int or level not in LEVEL_REWARDS or level in earned:
                    continue
                earned.add(level)
                data["rewards"].append({"level": level, "title": LEVEL_REWARDS[level],
                    "subtitle": f"Reach Level {level}",
                    "earned_at": row.get("earned_at") if isinstance(row.get("earned_at"), str) else ""})
        # Completion event history: {"goal_id", "title", "at"}. Survives goal
        # deletion and renames, so the stats graph reflects what actually
        # happened. Completions that predate this log have no event and are
        # not backdated.
        for row in source.get("completion_events", []):
            if not isinstance(row, dict):
                continue
            at = row.get("at")
            if not isinstance(at, str) or not at:
                continue
            try:
                datetime.fromisoformat(at)
            except ValueError:
                continue
            gid = row.get("goal_id")
            title = row.get("title")
            data["completion_events"].append({
                "goal_id": gid if isinstance(gid, str) else "",
                "title": title if isinstance(title, str) else "",
                "at": at})
        # Daily login history: sorted unique ISO local dates, one per day
        # the app was opened. Added in V5.2; older saves simply start empty
        # and begin recording on first run.
        seen = set()
        for row in source.get("login_history", []):
            if not isinstance(row, str):
                continue
            try:
                date.fromisoformat(row)
            except ValueError:
                continue
            seen.add(row)
        data["login_history"] = sorted(seen)
        # Gold (V5.3): must be a genuine int >= 0; anything else resets to 0.
        # Migrated saves start at 0 — login and level-up gold are never
        # backdated, same rule as the completion event log.
        raw_gold = source.get("gold", 0)
        data["gold"] = raw_gold if type(raw_gold) is int and raw_gold >= 0 else 0
        cls._award(data)
        return data, data != original

    @staticmethod
    def _encode(data):
        return (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")

    def _save(self, candidate):
        with self._locked():
            self._save_locked(candidate)

    def _save_locked(self, candidate):
        try:
            current = self._read()
            if fingerprint(current) != self._fingerprint:
                raise StorageError("The save changed outside this window. Use Reload before editing.")
            if current is not None:
                atomic_write(self.path.with_suffix(".json.bak"), current)
            encoded = self._encode(candidate)
            atomic_write(self.path, encoded)
            self._fingerprint = fingerprint(encoded)
        except OSError as exc:
            raise StorageError(f"Save failed; your change was not applied. {exc}") from exc

    def _change(self, action):
        candidate = deepcopy(self.data)
        result = action(candidate)
        self._award(candidate)
        # Level-up gold: higher levels pay more. Computed from the XP
        # transition so it fires exactly once per level gained, survives
        # undo via the snapshot, and never backdates migrated saves.
        old_level = self.data["total_xp"] // XP_PER_LEVEL
        new_level = candidate["total_xp"] // XP_PER_LEVEL
        for level in range(old_level + 1, new_level + 1):
            candidate["gold"] += self.level_gold(level)
        if candidate != self.data:
            self._save(candidate)
            self._history.append(deepcopy(self.data))
            self._history = self._history[-30:]
            self.data = candidate
        return result

    @staticmethod
    def _goal(data, gid):
        for goal in data["goals"]:
            if goal["id"] == gid:
                return goal
        raise ValueError("That goal no longer exists. Reload the list.")

    def add(self, title):
        title = title_value(title)
        gid = str(uuid.uuid4())
        def action(data):
            data["goals"].append({"id": gid, "title": title, "progress": 0,
                "completed": False, "xp_awarded": False, "created_at": now()})
        self._change(action)
        return gid

    def rename(self, gid, title):
        title = title_value(title)
        def action(data):
            self._goal(data, gid).update(title=title)
            for event in data["completion_events"]:
                if event["goal_id"] == gid:
                    event["title"] = title
        self._change(action)

    def set_progress(self, gid, value):
        value = progress_value(value)
        def action(data):
            goal = self._goal(data, gid)
            was_completed = goal["completed"]
            goal.update(progress=value, completed=value == 100)
            if value == 100 and not goal["xp_awarded"]:
                goal["xp_awarded"] = True
                data["total_xp"] += XP_PER_GOAL
                data["gold"] += GOLD_PER_GOAL
            # Completion events are history: logged when a goal first
            # finishes, withdrawn if reopened, and kept if the goal is
            # deleted. Undo restores the previous snapshot, events included.
            if value == 100 and not was_completed:
                data["completion_events"].append(
                    {"goal_id": gid, "title": goal["title"], "at": now()})
            elif value != 100 and was_completed:
                data["completion_events"] = [
                    e for e in data["completion_events"] if e["goal_id"] != gid]
        self._change(action)

    def delete(self, gid):
        def action(data):
            self._goal(data, gid)
            data["goals"] = [g for g in data["goals"] if g["id"] != gid]
        self._change(action)

    def undo(self):
        if self._history:
            candidate = deepcopy(self._history[-1])
            self._save(candidate)
            self._history.pop()
            self.data = candidate

    def export(self, path):
        path = Path(path).resolve()
        if path == self.path or path == self.path.with_suffix(".json.bak"):
            raise ValueError("Choose a different filename for the exported copy.")
        try:
            atomic_write(path, self._encode(self.data))
        except OSError as exc:
            raise StorageError(f"Export failed: {exc}") from exc

    def query(self, mode="active", search="", sort="Newest"):
        items = self.goals
        if mode == "active":
            items = [g for g in items if not g["completed"]]
        elif mode == "completed":
            items = [g for g in items if g["completed"]]
        words = search.casefold().split()
        items = [g for g in items if all(w in g["title"].casefold() for w in words)]
        if sort == "Title":
            items.sort(key=lambda g: g["title"].casefold())
        elif sort == "Progress":
            items.sort(key=lambda g: g["progress"], reverse=True)
        else:
            items.reverse()
        return items
