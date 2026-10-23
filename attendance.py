"""
Attendance analysis: finds who left after roll call, left and came back, or arrived late.

Independent of Zoom: join/leave events come from the live-watch log (zoom_attendance.py live)
or from a participant report the host exported from Zoom (CSV).
"""

import csv
import datetime as dt
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Event kinds (the 'event' column of the live-watch log)
JOINED = "joined"
LEFT = "left"
ROLL_CALL = "roll_call"
WATCH_STARTED = "watch_started"
WATCH_LOST = "watch_lost"
WATCH_RESUMED = "watch_resumed"
WATCH_ENDED = "watch_ended"

INITIAL_NOTE = "present when watching started"
OUTAGE_NOTE = "may have left while watching was paused"

LOG_FIELDS = ["time", "event", "name", "count", "note"]
SUMMARY_FIELDS = ["name", "status", "at_roll_call", "first_join", "last_leave", "minutes", "percent", "sessions", "note"]
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

Interval = Tuple[dt.datetime, dt.datetime]


@dataclass
class Event:
    """A single line of the attendance log."""
    time: dt.datetime
    kind: str
    name: str = ""
    count: Optional[int] = None
    note: str = ""

    def to_row(self) -> Dict[str, str]:
        return {
            "time": self.time.strftime(TIME_FORMAT),
            "event": self.kind,
            "name": self.name,
            "count": "" if self.count is None else str(self.count),
            "note": self.note,
        }

    @classmethod
    def from_row(cls, row: Dict[str, str]) -> "Event":
        count = row.get("count") or ""
        return cls(
            time=dt.datetime.strptime(row["time"], TIME_FORMAT),
            kind=row["event"],
            name=row.get("name") or "",
            count=int(count) if count else None,
            note=row.get("note") or "",
        )


# ─────────────────────────────────────────────
#  Live watching: successive participant lists → events
# ─────────────────────────────────────────────

class PresenceTracker:
    """Turns successive participant lists into join/leave events.

    Someone must be missing from `confirm_reads` consecutive reads before a leave is recorded
    (one-off UI glitches are ignored); the leave is timestamped at the first missing read.
    Duplicate display names are tracked by count.
    """

    def __init__(self, confirm_reads: int = 2):
        self.confirm_reads = confirm_reads
        self.present: Counter = Counter()
        self.last_roll_call: Optional[Tuple[dt.datetime, Counter]] = None
        self._missing: Dict[str, Tuple[dt.datetime, int, int]] = {}  # name → (first missing, reads, last seen count)
        self._lost = False
        self._resumed_at: Optional[dt.datetime] = None

    @property
    def count(self) -> int:
        return sum(self.present.values())

    def start(self, now: dt.datetime, names: Sequence[str]) -> List[Event]:
        self.present = Counter(names)
        events = [Event(now, WATCH_STARTED, count=self.count)]
        events += [Event(now, JOINED, name, self.count, INITIAL_NOTE) for name in sorted(self.present.elements())]
        return events

    def update(self, now: dt.datetime, names: Optional[Sequence[str]]) -> List[Event]:
        """Process one read; `names` is None when the panel could not be read."""
        if names is None:
            if self._lost:
                return []
            self._lost = True
            return [Event(now, WATCH_LOST, count=self.count)]

        events = []
        if self._lost:
            self._lost = False
            self._resumed_at = now
            events.append(Event(now, WATCH_RESUMED, count=len(names)))

        current = Counter(names)
        for name in sorted(set(current) | set(self.present)):
            have, seen = self.present[name], current[name]
            if seen >= have:
                self._missing.pop(name, None)
                for _ in range(seen - have):
                    self.present[name] += 1
                    events.append(Event(now, JOINED, name, self.count))
                continue

            first_missing, reads, _ = self._missing.get(name, (now, 0, seen))
            reads += 1
            if reads < self.confirm_reads:
                self._missing[name] = (first_missing, reads, seen)
                continue
            self._missing.pop(name, None)
            events += self._leave(name, have - seen, first_missing)

        self.present += Counter()  # drop names whose count reached zero
        return sorted(events, key=lambda e: e.time)

    def roll_call(self, now: dt.datetime) -> Event:
        """Mark a roll call; people whose leave is still being confirmed count as gone."""
        gone = Counter({name: self.present[name] - seen for name, (_, _, seen) in self._missing.items()})
        present = self.present - gone
        self.last_roll_call = (now, present)
        return Event(now, ROLL_CALL, count=sum(present.values()))

    def finish(self, now: dt.datetime) -> List[Event]:
        """Close the watch; leaves still being confirmed are recorded."""
        events = []
        for name, (first_missing, _, seen) in sorted(self._missing.items()):
            events += self._leave(name, self.present[name] - seen, first_missing)
        self._missing.clear()
        self.present += Counter()
        events.append(Event(now, WATCH_ENDED, count=self.count))
        return events

    def _leave(self, name: str, times: int, at: dt.datetime) -> List[Event]:
        note = OUTAGE_NOTE if at == self._resumed_at else ""
        events = []
        for _ in range(times):
            self.present[name] -= 1
            events.append(Event(at, LEFT, name, self.count, note))
        return events


# ─────────────────────────────────────────────
#  Events → timeline
# ─────────────────────────────────────────────

@dataclass
class Timeline:
    start: dt.datetime
    end: dt.datetime
    sessions: Dict[str, List[Interval]]
    roll_calls: List[dt.datetime]
    outages: List[Interval]

def build_timeline(events: Iterable[Event]) -> Timeline:
    """Pair join/leave events into presence intervals per person."""
    events = sorted(events, key=lambda e: e.time)
    if not events:
        raise ValueError("The log contains no events")

    start = end = lost_at = None
    open_joins: Dict[str, List[dt.datetime]] = defaultdict(list)
    sessions: Dict[str, List[Interval]] = defaultdict(list)
    roll_calls: List[dt.datetime] = []
    outages: List[Interval] = []

    for event in events:
        if event.kind == WATCH_STARTED and start is None:
            start = event.time
        elif event.kind == WATCH_ENDED:
            end = event.time
        elif event.kind == JOINED:
            open_joins[event.name].append(event.time)
        elif event.kind == LEFT and open_joins[event.name]:
            sessions[event.name].append((open_joins[event.name].pop(0), event.time))
        elif event.kind == ROLL_CALL:
            roll_calls.append(event.time)
        elif event.kind == WATCH_LOST and lost_at is None:
            lost_at = event.time
        elif event.kind == WATCH_RESUMED and lost_at is not None:
            outages.append((lost_at, event.time))
            lost_at = None

    start = start or events[0].time
    end = end or events[-1].time
    for name, joins in open_joins.items():
        sessions[name] += [(joined, end) for joined in joins]
    if lost_at is not None:
        outages.append((lost_at, end))

    return Timeline(start, end, dict(sessions), roll_calls, outages)


# ─────────────────────────────────────────────
#  Attendance verdicts
# ─────────────────────────────────────────────

@dataclass
class Rules:
    roll_calls: List[dt.datetime] = field(default_factory=list)
    # Leaving within the last N minutes / arriving within the first N minutes is fine
    tolerance: dt.timedelta = dt.timedelta(minutes=10)
    # Absences shorter than this are treated as reconnects
    min_gap: dt.timedelta = dt.timedelta(minutes=2)
    min_percent: float = 80.0

class Status(Enum):
    """Attendance verdicts, most serious first."""
    ABSENT = ("❌", "Absent at roll call")
    LEFT_AFTER_ROLL_CALL = ("⚠️", "Left after roll call")
    LEFT_EARLY = ("⚠️", "Left early")
    CAME_BACK = ("🔁", "Left and came back")
    LATE = ("⏰", "Late")
    LOW = ("📉", "Low participation")  # below min_percent with no specific cause above
    OK = ("✅", "Attended")

    @property
    def emoji(self) -> str:
        return self.value[0]

    @property
    def label(self) -> str:
        return self.value[1]

@dataclass
class PersonResult:
    name: str
    status: Status
    sessions: List[Interval]
    gaps: List[Interval]
    present: dt.timedelta
    percent: float
    at_roll_call: Optional[bool]
    note: str

@dataclass
class MeetingSummary:
    start: dt.datetime
    end: dt.datetime
    roll_calls: List[dt.datetime]
    people: List[PersonResult]
    outages: List[Interval]

def merge_sessions(sessions: Iterable[Interval], min_gap: dt.timedelta) -> List[Interval]:
    """Join intervals separated by less than `min_gap` (reconnects)."""
    merged: List[Interval] = []
    for start, end in sorted(sessions):
        if merged and start - merged[-1][1] < min_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged

def _minutes(delta: dt.timedelta) -> int:
    return round(delta.total_seconds() / 60)

def people(count: int) -> str:
    """'1 person', '3 people'."""
    return f"{count} {'person' if count == 1 else 'people'}"

def _judge(name: str, sessions: List[Interval], start: dt.datetime, end: dt.datetime,
           roll_calls: List[dt.datetime], rules: Rules) -> PersonResult:
    merged = [(max(s, start), min(e, end)) for s, e in merge_sessions(sessions, rules.min_gap) if e >= start and s <= end]
    present = sum((e - s for s, e in merged), dt.timedelta())
    percent = 100 * present / max(end - start, dt.timedelta(seconds=1))
    gaps = [(a[1], b[0]) for a, b in zip(merged, merged[1:])]
    first_join = merged[0][0] if merged else end
    last_leave = merged[-1][1] if merged else start

    roll_hits = [r for r in roll_calls if any(s <= r <= e for s, e in merged)]
    at_roll_call = bool(roll_hits) if roll_calls else None
    left_early = last_leave < end - rules.tolerance
    late = first_join > start + rules.tolerance or bool(roll_calls and first_join > roll_calls[0])

    if roll_calls and not roll_hits:
        status = Status.ABSENT
    elif left_early:
        status = Status.LEFT_AFTER_ROLL_CALL if roll_hits else Status.LEFT_EARLY
    elif gaps:
        status = Status.CAME_BACK
    elif late:
        status = Status.LATE
    elif percent < rules.min_percent:
        status = Status.LOW
    else:
        status = Status.OK

    notes = []
    if late:
        notes.append(f"arrived {first_join:%H:%M}")
    notes += [f"away {a:%H:%M}–{b:%H:%M} ({_minutes(b - a)} min)" for a, b in gaps]
    if left_early:
        note = f"left {last_leave:%H:%M}, did not return"
        if roll_hits:
            note += f" ({_minutes(last_leave - max(roll_hits))} min after roll call)"
        notes.append(note)

    return PersonResult(name, status, merged, gaps, present, percent, at_roll_call, "; ".join(notes))

def analyze(timeline: Timeline, rules: Rules) -> MeetingSummary:
    roll_calls = sorted(set(rules.roll_calls) | set(timeline.roll_calls))
    people = [_judge(name, sessions, timeline.start, timeline.end, roll_calls, rules)
              for name, sessions in timeline.sessions.items()]
    order = list(Status)
    people.sort(key=lambda p: (order.index(p.status), p.name.lower()))
    return MeetingSummary(timeline.start, timeline.end, roll_calls, people, timeline.outages)

def parse_clock(text: str, reference: dt.datetime) -> dt.datetime:
    """'14:45' → that time on the meeting's day (next day if the meeting crossed midnight)."""
    clock = None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            clock = dt.datetime.strptime(text.strip(), fmt).time()
            break
        except ValueError:
            continue
    if clock is None:
        raise ValueError(f"Could not read time {text!r} (example: 14:45)")
    moment = dt.datetime.combine(reference.date(), clock)
    if moment < reference - dt.timedelta(hours=1):
        moment += dt.timedelta(days=1)
    return moment


# ─────────────────────────────────────────────
#  Output
# ─────────────────────────────────────────────

def format_summary(summary: MeetingSummary, detailed: bool = False) -> str:
    total = _minutes(summary.end - summary.start)
    header = f"Meeting: {summary.start:%H:%M}–{summary.end:%H:%M} ({total} min)"
    if summary.roll_calls:
        header += " · Roll call: " + ", ".join(f"{r:%H:%M}" for r in summary.roll_calls)
    header += f" · {people(len(summary.people))}"
    lines = [header]
    lines += [f"⛔ Watching paused {a:%H:%M:%S}–{b:%H:%M:%S}; join/leave times in this range are approximate"
              for a, b in summary.outages]

    width = max((len(p.name) for p in summary.people), default=0)
    for status in Status:
        group = [p for p in summary.people if p.status is status]
        if not group:
            continue
        lines.append(f"\n{status.emoji} {status.label} ({len(group)})")
        for person in group:
            lines.append(f"   {person.name:<{width}}  {_minutes(person.present):>3} min ({person.percent:.0f}%)"
                         f"  {person.note}".rstrip())
            if detailed:
                lines.append(f"   {'':<{width}}  {_format_sessions(person.sessions)}")
    return "\n".join(lines)

def _format_sessions(sessions: List[Interval]) -> str:
    return ", ".join(f"{a:%H:%M:%S}–{b:%H:%M:%S}" for a, b in sessions)

def write_summary_csv(summary: MeetingSummary, path: Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for person in summary.people:
            writer.writerow({
                "name": person.name,
                "status": person.status.label,
                "at_roll_call": {True: "yes", False: "no", None: ""}[person.at_roll_call],
                "first_join": f"{person.sessions[0][0]:%H:%M:%S}" if person.sessions else "",
                "last_leave": f"{person.sessions[-1][1]:%H:%M:%S}" if person.sessions else "",
                "minutes": _minutes(person.present),
                "percent": f"{person.percent:.0f}",
                "sessions": _format_sessions(person.sessions),
                "note": person.note,
            })


# ─────────────────────────────────────────────
#  Input files: live-watch log or the host's Zoom report
# ─────────────────────────────────────────────

ZOOM_TIME_FORMATS = [
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%d.%m.%Y %H:%M",
]

# Column headers of English and Turkish Zoom exports
ZOOM_COLUMNS = {
    "name": re.compile(r"^(name|ad\b|isim|katılımcı adı)", re.IGNORECASE),
    "email": re.compile(r"e-?mail|e-posta", re.IGNORECASE),
    "join": re.compile(r"join|katılma|giriş", re.IGNORECASE),
    "leave": re.compile(r"leave|ayrılma|çıkış", re.IGNORECASE),
    "waiting": re.compile(r"waiting|bekleme", re.IGNORECASE),
    "start": re.compile(r"^start time|başlangıç", re.IGNORECASE),
    "end": re.compile(r"^end time|bitiş", re.IGNORECASE),
}

def _find_columns(header: Sequence[str]) -> Dict[str, int]:
    columns: Dict[str, int] = {}
    for index, title in enumerate(header):
        for key, pattern in ZOOM_COLUMNS.items():
            if key not in columns and pattern.search(title.strip()):
                columns[key] = index
                break
    return columns

def _detect_time_format(values: Iterable[str]) -> str:
    values = [v.strip() for v in values if v.strip()]
    for fmt in ZOOM_TIME_FORMATS:
        try:
            for value in values:
                dt.datetime.strptime(value, fmt)
            return fmt
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format in the Zoom report (example: {values[0]!r})" if values
                     else "The Zoom report has no participant rows")

def read_zoom_report(path: Path) -> List[Event]:
    """Participant report exported by the host (zoom.us → Reports → Usage → Participants → Export)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [row for row in csv.reader(f)]

    meeting_times: Tuple[str, str] = ("", "")
    participants: List[List[str]] = []
    columns: Dict[str, int] = {}
    for index, row in enumerate(rows):
        found = _find_columns(row)
        if "join" in found and "leave" in found and "name" in found:
            columns = found
            participants = [r for r in rows[index + 1:] if any(cell.strip() for cell in r)]
            break
        if "start" in found and "end" in found and index + 1 < len(rows):
            values = rows[index + 1]
            meeting_times = (values[found["start"]], values[found["end"]])
    if not columns:
        raise ValueError("The file is neither a live-watch log nor a Zoom participant report")

    def cell(row: List[str], key: str) -> str:
        return row[columns[key]].strip() if key in columns and columns[key] < len(row) else ""

    participants = [r for r in participants if cell(r, "join") and cell(r, "leave")
                    and cell(r, "waiting").lower() not in ("yes", "evet", "true")]
    fmt = _detect_time_format([cell(r, "join") for r in participants] + [cell(r, "leave") for r in participants]
                              + [t for t in meeting_times if t])

    # Same display name with different e-mails → keep them apart
    emails_by_name: Dict[str, set] = defaultdict(set)
    for row in participants:
        if cell(row, "email"):
            emails_by_name[cell(row, "name")].add(cell(row, "email").lower())

    events: List[Event] = []
    for row in participants:
        name = cell(row, "name")
        if len(emails_by_name[name]) > 1 and cell(row, "email"):
            name = f"{name} ({cell(row, 'email')})"
        events.append(Event(dt.datetime.strptime(cell(row, "join"), fmt), JOINED, name))
        events.append(Event(dt.datetime.strptime(cell(row, "leave"), fmt), LEFT, name))

    joins = [e.time for e in events if e.kind == JOINED]
    leaves = [e.time for e in events if e.kind == LEFT]
    start = dt.datetime.strptime(meeting_times[0], fmt) if meeting_times[0] else min(joins)
    end = dt.datetime.strptime(meeting_times[1], fmt) if meeting_times[1] else max(leaves)
    return [Event(start, WATCH_STARTED)] + events + [Event(max(end, max(leaves)), WATCH_ENDED)]

def load_events(path: Path) -> List[Event]:
    """Read a live-watch log (zoom_attendance.py live) or a host's Zoom participant report."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    if header == LOG_FIELDS:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [Event.from_row(row) for row in csv.DictReader(f)]
    return read_zoom_report(path)
