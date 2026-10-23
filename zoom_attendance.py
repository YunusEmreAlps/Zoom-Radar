"""
Zoom attendance tracking — no Zoom API or env.yaml needed.

  python zoom_attendance.py live              Watch the Participants panel during the meeting
  python zoom_attendance.py report LOG.csv    Analyse a live-watch log or the host's Zoom report
  python zoom_attendance.py check             Test that the Participants panel can be read
"""

import argparse
import csv
import datetime as dt
import sys
import time
from pathlib import Path
from typing import List, Optional

from attendance import (
    INITIAL_NOTE, JOINED, LEFT, LOG_FIELDS, ROLL_CALL, WATCH_ENDED, WATCH_LOST, WATCH_RESUMED, WATCH_STARTED,
    Event, PresenceTracker, Rules, analyze, build_timeline, format_summary, load_events, parse_clock, people,
    write_summary_csv,
)

DEFAULT_LOG_DIR = "logs"
# Panel count mismatch warning is repeated at most this often (seconds)
WARNING_INTERVAL = 60

def setup_output_encoding():
    """Force UTF-8, line-buffered stdout/stderr (emojis, non-ASCII names on redirected Windows output)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', line_buffering=True)

def add_rule_arguments(parser: argparse.ArgumentParser):
    parser.add_argument('--roll-call', action='append', default=[], metavar='HH:MM',
                        help='Roll call time, can be repeated (in live mode it is taken automatically at that time)')
    parser.add_argument('--tolerance', type=int, default=10, metavar='MIN',
                        help='Arriving in the first / leaving in the last N minutes is fine (default: 10)')
    parser.add_argument('--min-gap', type=int, default=2, metavar='MIN',
                        help='Shorter absences count as reconnects (default: 2)')
    parser.add_argument('--min-percent', type=float, default=80, metavar='PCT',
                        help='Attendance below this share of the meeting is "low participation" (default: 80)')
    parser.add_argument('--details', action='store_true', help="Also list everyone's join–leave intervals")

def build_rules(args: argparse.Namespace, reference: dt.datetime, include_roll_calls: bool = True) -> Rules:
    return Rules(
        roll_calls=[parse_clock(text, reference) for text in args.roll_call] if include_roll_calls else [],
        tolerance=dt.timedelta(minutes=args.tolerance),
        min_gap=dt.timedelta(minutes=args.min_gap),
        min_percent=args.min_percent,
    )

def print_summary(events: List[Event], args: argparse.Namespace, save_path: Optional[Path],
                  include_roll_calls: bool = True):
    timeline = build_timeline(events)
    summary = analyze(timeline, build_rules(args, timeline.start, include_roll_calls))
    print()
    print(format_summary(summary, detailed=args.details))
    if save_path is not None:
        write_summary_csv(summary, save_path)
        print(f"\n💾 Summary saved: {save_path}")

# ─────────────────────────────────────────────
#  report
# ─────────────────────────────────────────────

def run_report(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"Error: {path} not found")
        return 1
    try:
        events = load_events(path)
        save_path = path.with_name(f"{path.stem}_summary.csv") if args.save else None
        print_summary(events, args, save_path)
    except (ValueError, KeyError) as e:
        print(f"Error: {e}")
        return 1
    return 0

# ─────────────────────────────────────────────
#  live
# ─────────────────────────────────────────────

class LivePrinter:
    """Prints events as they happen; flags people who leave after the last roll call."""

    def __init__(self, tracker: PresenceTracker):
        self.tracker = tracker
        self.left = set()

    def show(self, event: Event):
        count = f" ({event.count})" if event.count is not None else ""
        if event.kind == WATCH_STARTED:
            line = f"▶️  Watching started · {people(event.count)} (excluding you)"
        elif event.kind == JOINED:
            if event.note == INITIAL_NOTE:
                return
            line = (f"🔁 {event.name} came back{count}" if event.name in self.left
                    else f"🟢 {event.name} joined{count}")
        elif event.kind == LEFT:
            self.left.add(event.name)
            roll_call = self.tracker.last_roll_call
            if roll_call and roll_call[1][event.name] > 0 and event.time >= roll_call[0]:
                minutes = round((event.time - roll_call[0]).total_seconds() / 60)
                line = f"⚠️  {event.name} left — {minutes} min after roll call{count}"
            else:
                line = f"🔴 {event.name} left{count}"
            if event.note:
                line += f" · {event.note}"
        elif event.kind == ROLL_CALL:
            line = f"📋 Roll call taken · {people(event.count)} present"
        elif event.kind == WATCH_LOST:
            line = "⛔ Cannot read the Participants panel — is it open? Waiting until it can be read..."
        elif event.kind == WATCH_RESUMED:
            line = f"▶️  Watching resumed · {people(event.count)}"
        elif event.kind == WATCH_ENDED:
            line = f"⏹️  Watching ended · {people(event.count)} present"
        else:
            line = event.kind
        print(f"{event.time:%H:%M:%S}  {line}")

class CountWarning:
    """Warns (rate-limited) when the panel title count differs from the names that could be read."""

    def __init__(self):
        self._last = -WARNING_INTERVAL

    def check(self, snapshot):
        expected = snapshot.expected_count
        if expected is None or expected == snapshot.read_count:
            return
        if time.monotonic() - self._last < WARNING_INTERVAL:
            return
        self._last = time.monotonic()
        print(f"{dt.datetime.now():%H:%M:%S}  ⚠️  Panel shows {people(expected)} but only {snapshot.read_count} "
              "names could be read — pop out the Participants panel and make it taller")

def _read_key() -> Optional[str]:
    import msvcrt
    if not msvcrt.kbhit():
        return None
    key = msvcrt.getwch()
    if key in ('\x00', '\xe0'):  # function/arrow keys send two codes
        msvcrt.getwch()
        return None
    return key

def _safe_read(reader):
    try:
        return reader.read()
    except Exception:  # transient UI Automation errors while Zoom redraws
        return None

def _wait_for_panel(reader, interval: float):
    """Block until the Participants panel can be read; None if the user quits."""
    hinted = False
    while True:
        snapshot = _safe_read(reader)
        if snapshot is not None:
            return snapshot
        if not hinted:
            print("   Panel not found. Open the 'Participants' panel in the Zoom meeting; waiting... (q: quit)")
            hinted = True
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline:
            if _read_key() in ('q', 'Q'):
                return None
            time.sleep(0.1)

def run_live(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("Error: live watching only works with the Zoom desktop app on Windows")
        return 1
    try:
        from zoom_ui import ParticipantReader
    except ImportError as e:
        print(f"Error: live watching needs uiautomation (pip install uiautomation): {e}")
        return 1

    started = dt.datetime.now()
    scheduled = sorted(parse_clock(text, started) for text in args.roll_call)
    for moment in [m for m in scheduled if m <= started]:
        print(f"⚠️  {moment:%H:%M} is in the past; it will not be taken automatically")
    scheduled = [m for m in scheduled if m > started]

    reader = ParticipantReader()
    tracker = PresenceTracker()
    printer = LivePrinter(tracker)
    warning = CountWarning()

    print("Looking for Zoom's Participants panel...  [Enter] take roll call · [q] finish")
    if scheduled:
        print("   Automatic roll calls: " + ", ".join(f"{m:%H:%M}" for m in scheduled))
    snapshot = _wait_for_panel(reader, args.interval)
    if snapshot is None:
        return 1

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"attendance_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"

    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()

        def emit(events: List[Event]):
            for event in events:
                writer.writerow(event.to_row())
                printer.show(event)
            f.flush()  # the log survives a crash or closed window

        emit(tracker.start(dt.datetime.now(), snapshot.names))
        warning.check(snapshot)
        try:
            while True:
                quit_requested = False
                deadline = time.monotonic() + args.interval
                while time.monotonic() < deadline and not quit_requested:
                    key = _read_key()
                    if key in ('\r', '\n', 'r', 'R'):
                        emit([tracker.roll_call(dt.datetime.now())])
                    elif key in ('q', 'Q'):
                        quit_requested = True
                    while scheduled and scheduled[0] <= dt.datetime.now():
                        emit([tracker.roll_call(scheduled.pop(0))])
                    time.sleep(0.1)
                if quit_requested:
                    break
                snapshot = _safe_read(reader)
                emit(tracker.update(dt.datetime.now(), snapshot.names if snapshot else None))
                if snapshot is not None:
                    warning.check(snapshot)
        except KeyboardInterrupt:
            pass
        emit(tracker.finish(dt.datetime.now()))

    print(f"\n💾 Log: {log_path}")
    # Roll calls (manual and --roll-call) are already in the log
    print_summary(load_events(log_path), args, log_path.with_name(f"{log_path.stem}_summary.csv"),
                  include_roll_calls=False)
    return 0

# ─────────────────────────────────────────────
#  check
# ─────────────────────────────────────────────

def run_check(args: argparse.Namespace) -> int:
    try:
        from zoom_ui import ParticipantReader, dump_tree, find_zoom_windows, mask_text
    except ImportError as e:
        print(f"Error: uiautomation is required (pip install uiautomation): {e}")
        return 1

    show = (lambda text: text) if args.unmasked else mask_text
    windows = find_zoom_windows()
    print(f"Zoom meeting windows: {len(windows)}"
          + (" (" + ", ".join(repr(w.Name) for w in windows) + ")" if windows else ""))
    if not windows:
        print("❌ No Zoom meeting is open.")
        return 1

    reader = ParticipantReader()
    started = time.perf_counter()
    snapshot = reader.read()
    first_read = time.perf_counter() - started
    if snapshot is None:
        print("❌ Participants panel not found. Open the 'Participants' panel in the meeting and try again.")
    else:
        started = time.perf_counter()
        reader.read()
        next_read = time.perf_counter() - started
        expected = snapshot.expected_count
        match = "✓" if expected == snapshot.read_count else "⚠️ mismatch — pop out the panel and make it taller"
        print(f"✅ Participants panel is readable (first search {first_read:.2f} s, later reads {next_read * 1000:.0f} ms)")
        title = people(expected) if expected is not None else "?"
        print(f"   Panel title: {title} · read: {snapshot.read_count} {match}")
        if snapshot.self_name:
            print(f"   You: {show(snapshot.self_name)} (excluded from attendance)")
        for name in snapshot.names:
            print(f"   • {show(name)}")

    if args.tree:
        print()
        print("\n".join(dump_tree(mask=not args.unmasked)))
    return 0 if snapshot is not None else 1

def main() -> int:
    setup_output_encoding()
    parser = argparse.ArgumentParser(description='Zoom attendance tracking (no Zoom API / env.yaml needed)')
    commands = parser.add_subparsers(dest='command', required=True)

    live = commands.add_parser('live', help='Watch the Participants panel during the meeting')
    live.add_argument('--interval', type=float, default=5, metavar='SEC',
                      help='Seconds between panel reads (default: 5)')
    live.add_argument('--log-dir', default=DEFAULT_LOG_DIR, metavar='DIR',
                      help=f'Folder for attendance logs (default: {DEFAULT_LOG_DIR})')
    add_rule_arguments(live)

    report = commands.add_parser('report', help="Analyse a live-watch log or the host's Zoom participant report")
    report.add_argument('file', help='attendance_*.csv log or a participant report exported from Zoom')
    report.add_argument('--save', action='store_true', help='Save the summary as <file>_summary.csv')
    add_rule_arguments(report)

    check = commands.add_parser('check', help="Test that Zoom's Participants panel can be read")
    check.add_argument('--unmasked', action='store_true', help='Show names without masking')
    check.add_argument('--tree', action='store_true', help="Print the UI tree of Zoom's windows (diagnostics)")

    args = parser.parse_args()
    try:
        return {'live': run_live, 'report': run_report, 'check': run_check}[args.command](args)
    except KeyboardInterrupt:
        print("\n⚠️ Cancelled.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
