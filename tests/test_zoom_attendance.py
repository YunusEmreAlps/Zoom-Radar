"""
Attendance tracking tests — no Zoom needed, fully offline.
Run: python -m unittest test_zoom_attendance
"""

import csv
import datetime as dt
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from attendance import (
    INITIAL_NOTE, JOINED, LEFT, LOG_FIELDS, OUTAGE_NOTE, ROLL_CALL, WATCH_ENDED, WATCH_LOST, WATCH_RESUMED,
    WATCH_STARTED, Event, PresenceTracker, Rules, Status, analyze, build_timeline, format_summary,
    load_events, merge_sessions, parse_clock, read_zoom_report,
)
from zoom_attendance import LivePrinter, main, setup_output_encoding

# Keep non-ASCII output from raising UnicodeEncodeError when redirected (CI, pipes)
setup_output_encoding()

DAY = dt.date(2026, 10, 23)


def t(hour, minute, second=0):
    return dt.datetime.combine(DAY, dt.time(hour, minute, second))


def meeting(sessions, start=t(14, 0), end=t(15, 0), roll_calls=()):
    """Events for a meeting: {name: [(join, leave), ...]}."""
    events = [Event(start, WATCH_STARTED), Event(end, WATCH_ENDED)]
    events += [Event(r, ROLL_CALL) for r in roll_calls]
    for name, intervals in sessions.items():
        for join, leave in intervals:
            events += [Event(join, JOINED, name), Event(leave, LEFT, name)]
    return events


def results_by_name(summary):
    return {person.name: person for person in summary.people}


# ─────────────────────────────────────────────
#  PresenceTracker
# ─────────────────────────────────────────────

class TestPresenceTracker(unittest.TestCase):

    def setUp(self):
        self.tracker = PresenceTracker()
        self.start_events = self.tracker.start(t(14, 0), ["Ali", "Veli"])

    def kinds(self, events):
        return [(e.kind, e.name) for e in events]

    def test_start_lists_everyone(self):
        self.assertEqual(self.kinds(self.start_events),
                         [(WATCH_STARTED, ""), (JOINED, "Ali"), (JOINED, "Veli")])
        self.assertTrue(all(e.note == INITIAL_NOTE for e in self.start_events[1:]))

    def test_join(self):
        events = self.tracker.update(t(14, 1), ["Ali", "Veli", "Ayşe"])
        self.assertEqual(self.kinds(events), [(JOINED, "Ayşe")])
        self.assertEqual(events[0].count, 3)

    def test_leave_needs_two_reads_and_uses_first_missing_time(self):
        """A person missing from one read is not 'left' yet; once confirmed, the first missing time is used."""
        self.assertEqual(self.tracker.update(t(14, 5, 0), ["Ali"]), [])
        events = self.tracker.update(t(14, 5, 5), ["Ali"])
        self.assertEqual(self.kinds(events), [(LEFT, "Veli")])
        self.assertEqual(events[0].time, t(14, 5, 0))

    def test_single_glitch_ignored(self):
        """A one-read UI glitch produces no join/leave."""
        self.assertEqual(self.tracker.update(t(14, 5, 0), ["Ali"]), [])
        self.assertEqual(self.tracker.update(t(14, 5, 5), ["Ali", "Veli"]), [])

    def test_duplicate_names_counted(self):
        """When one of two people with the same name leaves, a single 'left' event is produced."""
        tracker = PresenceTracker()
        tracker.start(t(14, 0), ["Ali", "Ali"])
        tracker.update(t(14, 1), ["Ali"])
        events = tracker.update(t(14, 2), ["Ali"])
        self.assertEqual(self.kinds(events), [(LEFT, "Ali")])
        self.assertEqual(tracker.present["Ali"], 1)

    def test_outage_and_resume(self):
        """An unreadable panel gives one 'lost' event; on resume, leaves during the gap are flagged."""
        self.assertEqual(self.kinds(self.tracker.update(t(14, 5), None)), [(WATCH_LOST, "")])
        self.assertEqual(self.tracker.update(t(14, 6), None), [])
        events = self.tracker.update(t(14, 7), ["Ali"])
        self.assertEqual(self.kinds(events), [(WATCH_RESUMED, "")])
        events = self.tracker.update(t(14, 8), ["Ali"])
        self.assertEqual(self.kinds(events), [(LEFT, "Veli")])
        self.assertEqual(events[0].note, OUTAGE_NOTE)

    def test_roll_call_excludes_pending_leaves(self):
        self.tracker.update(t(14, 5), ["Ali"])  # Veli disappeared, not confirmed yet
        event = self.tracker.roll_call(t(14, 5, 2))
        self.assertEqual(event.count, 1)
        self.assertEqual(self.tracker.last_roll_call[1]["Veli"], 0)

    def test_finish_records_pending_leaves(self):
        self.tracker.update(t(14, 59), ["Ali"])
        events = self.tracker.finish(t(15, 0))
        self.assertEqual(self.kinds(events), [(LEFT, "Veli"), (WATCH_ENDED, "")])
        self.assertEqual(events[0].time, t(14, 59))


# ─────────────────────────────────────────────
#  Analysis
# ─────────────────────────────────────────────

class TestAnalyze(unittest.TestCase):
    """Meeting 14:00–15:00, roll call 14:15."""

    SESSIONS = {
        "Ali": [(t(14, 0), t(15, 0))],
        "Veli": [(t(14, 0), t(14, 20))],
        "Ayşe": [(t(14, 0), t(14, 30)), (t(14, 45), t(15, 0))],
        "Fatma": [(t(14, 25), t(15, 0))],
        "Can": [(t(14, 0), t(14, 10)), (t(14, 11), t(15, 0))],
        "Deniz": [(t(14, 0), t(14, 52))],
        "Ece": [(t(14, 12), t(15, 0))],
    }

    def analyze(self, roll_calls=(t(14, 15),), **rules):
        return analyze(build_timeline(meeting(self.SESSIONS)), Rules(roll_calls=list(roll_calls), **rules))

    def test_statuses_with_roll_call(self):
        people = results_by_name(self.analyze())
        self.assertEqual(people["Ali"].status, Status.OK)
        self.assertEqual(people["Veli"].status, Status.LEFT_AFTER_ROLL_CALL)
        self.assertEqual(people["Ayşe"].status, Status.CAME_BACK)
        self.assertEqual(people["Fatma"].status, Status.ABSENT)
        self.assertEqual(people["Can"].status, Status.OK, "a 1-minute drop should be merged")
        self.assertEqual(people["Deniz"].status, Status.OK, "leaving in the last 10 minutes is fine")
        self.assertEqual(people["Ece"].status, Status.LATE)

    def test_notes(self):
        people = results_by_name(self.analyze())
        self.assertEqual(people["Veli"].note, "left 14:20, did not return (5 min after roll call)")
        self.assertEqual(people["Ayşe"].note, "away 14:30–14:45 (15 min)")
        self.assertEqual(people["Fatma"].note, "arrived 14:25")
        self.assertFalse(people["Fatma"].at_roll_call)
        self.assertTrue(people["Ali"].at_roll_call)

    def test_without_roll_call(self):
        people = results_by_name(self.analyze(roll_calls=()))
        self.assertEqual(people["Veli"].status, Status.LEFT_EARLY)
        self.assertEqual(people["Fatma"].status, Status.LATE)
        self.assertIsNone(people["Ali"].at_roll_call)

    def test_low_participation(self):
        """Arriving and leaving within the tolerance but staying too little is 'low participation'."""
        summary = analyze(build_timeline(meeting({"Gül": [(t(14, 9), t(14, 51))]})), Rules())
        person = summary.people[0]
        self.assertEqual(person.status, Status.LOW)
        self.assertAlmostEqual(person.percent, 70, places=0)

    def test_people_sorted_by_severity(self):
        statuses = [p.status for p in self.analyze().people]
        order = list(Status)
        self.assertEqual(statuses, sorted(statuses, key=order.index))

    def test_format_summary(self):
        text = format_summary(self.analyze())
        self.assertIn("Meeting: 14:00–15:00 (60 min) · Roll call: 14:15 · 7 people", text)
        self.assertIn("⚠️ Left after roll call (1)", text)
        self.assertIn("❌ Absent at roll call (1)", text)

    def test_merge_sessions(self):
        merged = merge_sessions([(t(14, 0), t(14, 10)), (t(14, 11), t(14, 20)), (t(14, 30), t(14, 40))],
                                dt.timedelta(minutes=2))
        self.assertEqual(merged, [(t(14, 0), t(14, 20)), (t(14, 30), t(14, 40))])


class TestTimeline(unittest.TestCase):

    def test_open_sessions_closed_at_end_and_outages(self):
        events = [
            Event(t(14, 0), WATCH_STARTED), Event(t(14, 0), JOINED, "Ali"),
            Event(t(14, 20), WATCH_LOST), Event(t(14, 22), WATCH_RESUMED),
            Event(t(15, 0), WATCH_ENDED),
        ]
        timeline = build_timeline(events)
        self.assertEqual(timeline.sessions["Ali"], [(t(14, 0), t(15, 0))])
        self.assertEqual(timeline.outages, [(t(14, 20), t(14, 22))])
        self.assertIn("Watching paused 14:20:00–14:22:00", format_summary(analyze(timeline, Rules())))

    def test_empty_log_rejected(self):
        with self.assertRaises(ValueError):
            build_timeline([])

    def test_parse_clock(self):
        self.assertEqual(parse_clock("14:45", t(14, 0)), t(14, 45))
        self.assertEqual(parse_clock("14:45:30", t(14, 0)), t(14, 45, 30))
        # Meeting that crosses midnight
        self.assertEqual(parse_clock("00:15", t(23, 30)), t(0, 15) + dt.timedelta(days=1))
        with self.assertRaises(ValueError):
            parse_clock("14.45", t(14, 0))


# ─────────────────────────────────────────────
#  Files
# ─────────────────────────────────────────────

ZOOM_REPORT_EN = """Meeting ID,Topic,Start Time,End Time,User Email,Duration (Minutes),Participants
123 4567 8901,Weekly Meeting,10/23/2026 02:00:00 PM,10/23/2026 03:00:00 PM,host@company.com,60,5

Name (Original Name),User Email,Join Time,Leave Time,Duration (Minutes),Guest,In Waiting Room
Ali Kaya,ali@company.com,10/23/2026 02:00:10 PM,10/23/2026 03:00:00 PM,60,No,No
Veli Can,veli@company.com,10/23/2026 01:58:00 PM,10/23/2026 02:00:05 PM,3,No,Yes
Veli Can,veli@company.com,10/23/2026 02:00:05 PM,10/23/2026 02:20:00 PM,20,No,No
Ayşe Demir,ayse@company.com,10/23/2026 02:01:00 PM,10/23/2026 02:30:00 PM,29,No,No
Ayşe Demir,ayse@company.com,10/23/2026 02:45:00 PM,10/23/2026 03:00:00 PM,15,No,No
Mehmet Ak,mehmet1@company.com,10/23/2026 02:00:00 PM,10/23/2026 03:00:00 PM,60,No,No
Mehmet Ak,mehmet2@company.com,10/23/2026 02:00:00 PM,10/23/2026 02:10:00 PM,10,No,No
"""

# Same report exported from a Zoom account set to Turkish
ZOOM_REPORT_TR = """Ad (Orijinal Ad),Kullanıcı E-postası,Katılma Saati,Ayrılma Saati,Süre (Dakika),Misafir
Ali Kaya,ali@company.com,23.10.2026 14:00:10,23.10.2026 15:00:00,60,Hayır
Veli Can,veli@company.com,23.10.2026 14:00:05,23.10.2026 14:20:00,20,Hayır
"""


class TestFiles(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content):
        path = self.folder / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_zoom_report_english(self):
        events = read_zoom_report(self.write("participants.csv", ZOOM_REPORT_EN))
        timeline = build_timeline(events)
        self.assertEqual((timeline.start, timeline.end), (t(14, 0), t(15, 0)))
        people = results_by_name(analyze(timeline, Rules(roll_calls=[t(14, 15)])))
        self.assertEqual(people["Ali Kaya"].status, Status.OK)
        self.assertEqual(people["Veli Can"].status, Status.LEFT_AFTER_ROLL_CALL)
        self.assertEqual(people["Ayşe Demir"].status, Status.CAME_BACK)
        # Waiting room rows are ignored
        self.assertEqual(people["Veli Can"].sessions[0][0], t(14, 0, 5))
        # Same name, different e-mail → two people
        self.assertIn("Mehmet Ak (mehmet1@company.com)", people)
        self.assertIn("Mehmet Ak (mehmet2@company.com)", people)

    def test_zoom_report_turkish_headers(self):
        events = read_zoom_report(self.write("katilimcilar.csv", ZOOM_REPORT_TR))
        people = results_by_name(analyze(build_timeline(events), Rules(roll_calls=[t(14, 15)])))
        self.assertEqual(people["Veli Can"].status, Status.LEFT_AFTER_ROLL_CALL)

    def test_unknown_file_rejected(self):
        with self.assertRaises(ValueError):
            read_zoom_report(self.write("x.csv", "a,b,c\n1,2,3\n"))

    def test_live_log_round_trip(self):
        original = meeting({"Ali": [(t(14, 0), t(15, 0))]}, roll_calls=[t(14, 15)])
        path = self.folder / "attendance_test.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
            writer.writerows(e.to_row() for e in original)
        loaded = load_events(path)
        self.assertEqual([(e.time, e.kind, e.name) for e in loaded], [(e.time, e.kind, e.name) for e in original])

    def test_report_command(self):
        """'zoom_attendance.py report' prints the summary and --save writes a CSV."""
        path = self.write("participants.csv", ZOOM_REPORT_EN)
        output = io.StringIO()
        argv = ["zoom_attendance.py", "report", str(path), "--roll-call", "14:15", "--save"]
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn("⚠️ Left after roll call (1)", output.getvalue())
        self.assertTrue((self.folder / "participants_summary.csv").exists())

    def test_report_missing_file(self):
        with patch.object(sys, "argv", ["zoom_attendance.py", "report", str(self.folder / "missing.csv")]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)


# ─────────────────────────────────────────────
#  Live output and Zoom labels
# ─────────────────────────────────────────────

class TestLivePrinter(unittest.TestCase):

    def test_leave_after_roll_call_flagged(self):
        tracker = PresenceTracker()
        printer = LivePrinter(tracker)
        tracker.start(t(14, 0), ["Ali", "Veli"])
        tracker.roll_call(t(14, 15))
        output = io.StringIO()
        with redirect_stdout(output):
            tracker.update(t(14, 17), ["Ali"])
            for event in tracker.update(t(14, 17, 5), ["Ali"]):
                printer.show(event)
            for event in tracker.update(t(14, 20), ["Ali", "Veli"]):
                printer.show(event)
        self.assertIn("⚠️  Veli left — 2 min after roll call (1)", output.getvalue())
        self.assertIn("🔁 Veli came back (2)", output.getvalue())


try:
    import zoom_ui
except ImportError:  # no uiautomation / not Windows
    zoom_ui = None


@unittest.skipIf(zoom_ui is None, "uiautomation is not installed")
class TestZoomLabels(unittest.TestCase):
    """Participant list item labels of Zoom Workplace 7.1.9."""

    def test_parse_item(self):
        cases = {
            "Yunus Emre Alpu,(Host, me), Computer audio unmuted,Video off, Press tab for more options":
                ("Yunus Emre Alpu", ["host", "me"]),
            "Ahmet Yılmaz, Computer audio muted,Video off, Press tab for more options": ("Ahmet Yılmaz", []),
            "Ayşe Kaya (Co-host), Computer audio muted": ("Ayşe Kaya", ["co-host"]),
            "Mehmet (IT), Computer audio muted": ("Mehmet (IT)", []),
            "Ali,(Ben), Bilgisayar sesi kapalı": ("Ali", ["ben"]),
        }
        for label, expected in cases.items():
            self.assertEqual(zoom_ui.parse_item(label), expected, label)

    def test_snapshot_separates_self(self):
        snapshot = zoom_ui.ParticipantReader._snapshot(
            ["Yunus,(Host, me), Computer audio unmuted", "Ahmet, Computer audio muted"], expected=2)
        self.assertEqual(snapshot.self_name, "Yunus")
        self.assertEqual(snapshot.names, ["Ahmet"])
        self.assertEqual(snapshot.read_count, 2)

    def test_mask_text(self):
        self.assertEqual(zoom_ui.mask_text("Ahmet Yılmaz (12)"), "A**** Y***** (12)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
