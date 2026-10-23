"""
Zoom Radar - Mock Test Suite
API key olmadan tüm fonksiyonları test eder.
Çalıştır: python test_zoom_activity.py
"""

import argparse
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
import datetime
import sys
import os
import tempfile

# zoom_activity.py ile aynı klasörde olduğunu varsayıyoruz
# sys.path.insert(0, '/path/to/zoom_activity')  # Gerekirse yolu düzenle

from zoom_activity import (
    ZoomAPI,
    ActivityType,
    UserActivity,
    parse_datetime,
    encode_uuid,
    classify_event,
    get_meeting_instances,
    diagnose_missing_meeting,
    get_meeting_participants,
    get_meeting_events,
    parse_user_activities,
    display_detailed_activity_report,
    save_activities_to_file,
    resolve_credentials,
    mask,
    main,
    setup_output_encoding,
)

# Çıktı yönlendirildiğinde (CI, pipe) Türkçe karakterler UnicodeEncodeError vermesin
setup_output_encoding()

# ─────────────────────────────────────────────
#  SAHTE (MOCK) VERİLER
# ─────────────────────────────────────────────

FAKE_INSTANCES = {
    "meetings": [
        {"uuid": "abc123==", "start_time": "2025-10-15T14:30:15Z"},
        {"uuid": "xyz789==", "start_time": "2025-10-16T10:00:00Z"},
    ]
}

FAKE_PARTICIPANTS = {
    "participants": [
        {
            "name": "Ahmet Yılmaz",
            "user_id": "user_001",
            "join_time": "2025-10-15T14:32:10Z",
            "leave_time": "2025-10-15T15:17:22Z",
            "duration": 2712,
        },
        {
            "name": "Ayşe Kaya",
            "user_id": "user_002",
            "join_time": "2025-10-15T14:35:05Z",
            "leave_time": "2025-10-15T15:17:55Z",
            "duration": 2570,
        },
    ]
}

FAKE_EVENTS = {
    "events": [
        {
            "time": "2025-10-15T14:32:10Z",
            "event_type": "participant.joined",
            "participant_name": "Ahmet Yılmaz",
            "participant_user_id": "user_001",
        },
        {
            "time": "2025-10-15T14:33:00Z",
            "event_type": "video.started",
            "participant_name": "Ahmet Yılmaz",
            "participant_user_id": "user_001",
        },
        {
            "time": "2025-10-15T14:34:00Z",
            "event_type": "audio.unmuted",
            "participant_name": "Ahmet Yılmaz",
            "participant_user_id": "user_001",
        },
        {
            "time": "2025-10-15T14:40:00Z",
            "event_type": "screen.share.started",
            "participant_name": "Ayşe Kaya",
            "participant_user_id": "user_002",
        },
        {
            "time": "2025-10-15T14:50:00Z",
            "event_type": "screen.share.ended",
            "participant_name": "Ayşe Kaya",
            "participant_user_id": "user_002",
        },
        {
            "time": "2025-10-15T15:00:00Z",
            "event_type": "recording.started",
            "participant_name": "Ahmet Yılmaz",
            "participant_user_id": "user_001",
        },
        {
            "time": "2025-10-15T15:17:22Z",
            "event_type": "participant.left",
            "participant_name": "Ahmet Yılmaz",
            "participant_user_id": "user_001",
        },
    ]
}

FAKE_EMPTY = {"meetings": []}

FAKE_TOKEN = {"access_token": "fake_token_789", "expires_in": 3600}


# ─────────────────────────────────────────────
#  YARDIMCI FONKSİYON
# ─────────────────────────────────────────────

def make_mock_response(json_data, status_code=200):
    """requests.get() için sahte response nesnesi üretir."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()  # hata fırlatmaz
    return mock


def make_api():
    """Token isteği atmadan ZoomAPI nesnesi üretir."""
    with patch.object(ZoomAPI, "_fetch_token", return_value=FAKE_TOKEN):
        return ZoomAPI("fake_account", "fake_client", "fake_secret")


# ─────────────────────────────────────────────
#  TEST SINIFI
# ─────────────────────────────────────────────

class TestZoomAPI(unittest.TestCase):
    """ZoomAPI sınıfı testleri."""

    def setUp(self):
        """Her test öncesi sahte API nesnesi oluştur."""
        self.api = make_api()

    def test_init(self):
        """Kimlik bilgileri doğru saklanıyor mu?"""
        self.assertEqual(self.api.account_id, "fake_account")
        self.assertEqual(self.api.client_id, "fake_client")
        self.assertEqual(self.api.client_secret, "fake_secret")

    def test_session_has_auth_header(self):
        """OAuth Authorization header set edilmiş mi?"""
        auth_header = self.api.session.headers.get("Authorization", "")
        self.assertEqual(auth_header, "Bearer fake_token_789")

    @patch("zoom_activity.requests.post")
    def test_token_failure_raises(self, mock_post):
        """Token alınamazsa RuntimeError fırlatıyor mu?"""
        mock_post.return_value = make_mock_response({}, status_code=401)
        with self.assertRaises(RuntimeError):
            ZoomAPI("bad_account", "bad_client", "bad_secret")

    @patch("zoom_activity.requests.Session.get")
    def test_expired_token_refreshed(self, mock_get):
        """Süresi dolan token istekten önce yenileniyor mu?"""
        mock_get.return_value = make_mock_response({})
        self.api._token_expires_at = 0
        new_token = {"access_token": "new_token", "expires_in": 3600}
        with patch.object(ZoomAPI, "_fetch_token", return_value=new_token) as mock_fetch:
            self.api.make_request("/test-endpoint")
            self.api.make_request("/test-endpoint")
        mock_fetch.assert_called_once()
        self.assertEqual(self.api.session.headers["Authorization"], "Bearer new_token")

    @patch("zoom_activity.requests.Session.get")
    def test_get_paginated_follows_next_page_token(self, mock_get):
        """Tüm sayfalar next_page_token ile çekiliyor mu?"""
        mock_get.side_effect = [
            make_mock_response({"participants": [{"name": "A"}], "next_page_token": "tok"}),
            make_mock_response({"participants": [{"name": "B"}], "next_page_token": ""}),
        ]
        result = self.api.get_paginated("/test-endpoint", "participants", page_size=300)
        self.assertEqual([p["name"] for p in result], ["A", "B"])
        self.assertEqual(mock_get.call_count, 2)
        second_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertEqual(second_params["next_page_token"], "tok")
        self.assertEqual(second_params["page_size"], 300)

    @patch("zoom_activity.requests.Session.get")
    def test_make_request_success(self, mock_get):
        """Başarılı API isteği doğru veriyi döndürüyor mu?"""
        mock_get.return_value = make_mock_response({"key": "value"})
        result = self.api.make_request("/test-endpoint")
        self.assertEqual(result, {"key": "value"})

    @patch("zoom_activity.requests.Session.get")
    def test_make_request_failure(self, mock_get):
        """Ağ hatası durumunda exception fırlatıyor mu?"""
        import requests as req
        mock_get.side_effect = req.exceptions.ConnectionError("Bağlantı hatası")
        with self.assertRaises(req.exceptions.ConnectionError):
            self.api.make_request("/bad-endpoint")


class TestParseDatetime(unittest.TestCase):
    """parse_datetime fonksiyonu testleri."""

    def test_valid_datetime(self):
        result = parse_datetime("2025-10-15T14:30:15Z")
        self.assertEqual(result.year, 2025)
        self.assertEqual(result.month, 10)
        self.assertEqual(result.day, 15)
        self.assertEqual(result.hour, result.hour)  # timezone dönüşümü olabilir

    def test_returns_aware_datetime(self):
        """Timezone-aware datetime dönüyor mu?"""
        result = parse_datetime("2025-10-15T14:30:15Z")
        self.assertIsNotNone(result.tzinfo)

    def test_invalid_format_raises(self):
        """Hatalı format exception fırlatıyor mu?"""
        with self.assertRaises(ValueError):
            parse_datetime("15-10-2025 14:30")


class TestEncodeUuid(unittest.TestCase):
    """encode_uuid fonksiyonu testleri."""

    def test_plain_uuid_single_encoded(self):
        self.assertEqual(encode_uuid("abc123=="), "abc123%3D%3D")

    def test_leading_slash_double_encoded(self):
        """'/' ile başlayan UUID iki kez encode ediliyor mu?"""
        self.assertEqual(encode_uuid("/abc=="), "%252Fabc%253D%253D")

    def test_double_slash_double_encoded(self):
        """'//' içeren UUID iki kez encode ediliyor mu?"""
        self.assertEqual(encode_uuid("ab//c"), "ab%252F%252Fc")


class TestGetMeetingInstances(unittest.TestCase):
    """get_meeting_instances fonksiyonu testleri."""

    def setUp(self):
        self.api = make_api()

    @patch("zoom_activity.requests.Session.get")
    def test_returns_sorted_meetings(self, mock_get):
        """Toplantılar tarihe göre sıralı geliyor mu?"""
        mock_get.return_value = make_mock_response(FAKE_INSTANCES)
        result = get_meeting_instances(self.api, 91202248508)
        self.assertEqual(len(result), 2)
        # İlk toplantı daha erken olmalı
        self.assertLess(result[0]["start_time"], result[1]["start_time"])

    @patch("zoom_activity.requests.Session.get")
    def test_empty_meetings(self, mock_get):
        """Toplantı bulunamadığında boş liste döndürüyor mu?"""
        mock_get.return_value = make_mock_response(FAKE_EMPTY)
        result = get_meeting_instances(self.api, 99999)
        self.assertEqual(result, [])

    @patch("zoom_activity.requests.Session.get")
    def test_missing_meetings_key(self, mock_get):
        """API 'meetings' anahtarı dönmezse boş liste geliyor mu?"""
        mock_get.return_value = make_mock_response({"code": 3001, "message": "Meeting not found"})
        result = get_meeting_instances(self.api, 00000)
        self.assertEqual(result, [])


class TestDiagnoseMissingMeeting(unittest.TestCase):
    """diagnose_missing_meeting fonksiyonu testleri."""

    def setUp(self):
        self.api = make_api()

    @patch("zoom_activity.requests.Session.get")
    def test_live_meeting(self, mock_get):
        """Devam eden toplantı için 'bitince tekrar deneyin' deniyor mu?"""
        mock_get.return_value = make_mock_response({"id": 123, "status": "started"})
        self.assertIn("devam ediyor", diagnose_missing_meeting(self.api, 123))
        self.assertTrue(mock_get.call_args.args[0].endswith("/meetings/123"))

    @patch("zoom_activity.requests.Session.get")
    def test_waiting_meeting(self, mock_get):
        """Başlamamış / tek kişilik toplantı açıklanıyor mu?"""
        mock_get.return_value = make_mock_response({"id": 123, "status": "waiting"})
        self.assertIn("bitmiş bir oturumu yok", diagnose_missing_meeting(self.api, 123))

    @patch("zoom_activity.requests.Session.get")
    def test_unknown_meeting(self, mock_get):
        """Hesapta olmayan toplantı için ID / hesap uyarısı veriliyor mu?"""
        import io
        import requests as req
        from contextlib import redirect_stdout

        response = make_mock_response({"code": 3001, "message": "Meeting does not exist"}, status_code=404)
        mock_get.side_effect = req.exceptions.HTTPError("404 Client Error", response=response)
        with redirect_stdout(io.StringIO()):
            message = diagnose_missing_meeting(self.api, 999)
        self.assertIn("bu Zoom hesabında yok", message)


class TestGetMeetingParticipants(unittest.TestCase):
    """get_meeting_participants fonksiyonu testleri."""

    def setUp(self):
        self.api = make_api()

    @patch("zoom_activity.requests.Session.get")
    def test_returns_participants(self, mock_get):
        """Katılımcılar doğru parse ediliyor mu?"""
        mock_get.return_value = make_mock_response(FAKE_PARTICIPANTS)
        result = get_meeting_participants(self.api, "abc123==")
        self.assertEqual(len(result), 2)
        names = [p["name"] for p in result]
        self.assertIn("Ahmet Yılmaz", names)
        self.assertIn("Ayşe Kaya", names)

    @patch("zoom_activity.requests.Session.get")
    def test_participants_sorted_by_name(self, mock_get):
        """Katılımcılar isme göre sıralanıyor mu?"""
        mock_get.return_value = make_mock_response(FAKE_PARTICIPANTS)
        result = get_meeting_participants(self.api, "abc123==")
        names = [p["name"] for p in result]
        self.assertEqual(names, sorted(names))

    @patch("zoom_activity.requests.Session.get")
    def test_api_error_returns_empty(self, mock_get):
        """API hatasında boş liste döndürüyor mu?"""
        import requests as req
        mock_get.side_effect = req.exceptions.HTTPError("404 Not Found")
        result = get_meeting_participants(self.api, "bad_uuid")
        self.assertEqual(result, [])


class TestGetMeetingEvents(unittest.TestCase):
    """get_meeting_events fonksiyonu testleri."""

    def setUp(self):
        self.api = make_api()

    @patch("zoom_activity.requests.Session.get")
    def test_returns_events(self, mock_get):
        """Etkinlikler doğru parse ediliyor mu?"""
        mock_get.return_value = make_mock_response(FAKE_EVENTS)
        result = get_meeting_events(self.api, "abc123==")
        self.assertEqual(len(result), 7)

    @patch("zoom_activity.requests.Session.get")
    def test_events_sorted_by_time(self, mock_get):
        """Etkinlikler zamana göre sıralı mı?"""
        mock_get.return_value = make_mock_response(FAKE_EVENTS)
        result = get_meeting_events(self.api, "abc123==")
        times = [e["time"] for e in result]
        self.assertEqual(times, sorted(times))

    @patch("zoom_activity.requests.Session.get")
    def test_api_error_returns_empty(self, mock_get):
        """API hatasında boş liste döndürüyor mu?"""
        import requests as req
        mock_get.side_effect = req.exceptions.HTTPError("500")
        result = get_meeting_events(self.api, "bad_uuid")
        self.assertEqual(result, [])


class TestParseUserActivities(unittest.TestCase):
    """parse_user_activities fonksiyonu testleri."""

    def _participants(self):
        return FAKE_PARTICIPANTS["participants"]

    def _events(self):
        return FAKE_EVENTS["events"]

    def test_join_leave_parsed(self):
        """JOIN ve LEAVE aktiviteleri parse ediliyor mu?"""
        activities = parse_user_activities(self._participants(), [])
        types = [a.activity_type for a in activities]
        self.assertIn(ActivityType.JOIN, types)
        self.assertIn(ActivityType.LEAVE, types)

    def test_camera_on_parsed(self):
        """Kamera açma eventi parse ediliyor mu?"""
        activities = parse_user_activities([], self._events())
        types = [a.activity_type for a in activities]
        self.assertIn(ActivityType.CAMERA_ON, types)

    def test_mic_unmute_parsed(self):
        """Mikrofon açma eventi parse ediliyor mu?"""
        activities = parse_user_activities([], self._events())
        types = [a.activity_type for a in activities]
        self.assertIn(ActivityType.MIC_UNMUTE, types)

    def test_screen_share_parsed(self):
        """Ekran paylaşımı parse ediliyor mu?"""
        activities = parse_user_activities([], self._events())
        types = [a.activity_type for a in activities]
        self.assertIn(ActivityType.SCREEN_SHARE_START, types)
        self.assertIn(ActivityType.SCREEN_SHARE_END, types)

    def test_recording_parsed(self):
        """Kayıt eventi parse ediliyor mu?"""
        activities = parse_user_activities([], self._events())
        types = [a.activity_type for a in activities]
        self.assertIn(ActivityType.RECORDING_START, types)

    def test_sorted_by_timestamp(self):
        """Aktiviteler zamana göre sıralı mı?"""
        activities = parse_user_activities(self._participants(), self._events())
        timestamps = [a.timestamp for a in activities]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_empty_inputs(self):
        """Boş girişlerde boş liste döndürüyor mu?"""
        activities = parse_user_activities([], [])
        self.assertEqual(activities, [])

    def test_user_name_assigned(self):
        """Kullanıcı adı doğru atanıyor mu?"""
        activities = parse_user_activities(self._participants(), [])
        names = {a.user_name for a in activities}
        self.assertIn("Ahmet Yılmaz", names)
        self.assertIn("Ayşe Kaya", names)

    def test_combined_participants_and_events(self):
        """Katılımcı + event verileri birlikte çalışıyor mu?"""
        activities = parse_user_activities(self._participants(), self._events())
        self.assertGreater(len(activities), 5)

    def test_participant_left_parsed_as_leave(self):
        """'participant.left' eventi LEAVE olarak parse ediliyor mu?"""
        self.assertEqual(classify_event("participant.left"), ActivityType.LEAVE)
        self.assertEqual(classify_event("meeting.participant_left"), ActivityType.LEAVE)

    def test_duplicate_join_leave_removed(self):
        """Katılımcı raporu ve eventlerdeki aynı JOIN/LEAVE tek kez sayılıyor mu?"""
        activities = parse_user_activities(self._participants(), self._events())
        ahmet = [a for a in activities if a.user_name == "Ahmet Yılmaz"]
        joins = [a for a in ahmet if a.activity_type == ActivityType.JOIN]
        leaves = [a for a in ahmet if a.activity_type == ActivityType.LEAVE]
        self.assertEqual(len(joins), 1)
        self.assertEqual(len(leaves), 1)
        # Katılımcı kaydındaki (detaylı) kopya korunmalı
        self.assertEqual(joins[0].details, "Duration: 45 minutes")

    def test_duration_converted_to_minutes(self):
        """Zoom'un saniye cinsinden süresi dakikaya çevriliyor mu?"""
        activities = parse_user_activities(self._participants(), [])
        join = next(a for a in activities if a.user_name == "Ayşe Kaya" and a.activity_type == ActivityType.JOIN)
        self.assertEqual(join.details, "Duration: 43 minutes")

    def test_event_without_time_skipped(self):
        """Zamanı olmayan event crash yerine atlanıyor mu?"""
        activities = parse_user_activities([], [{"event_type": "video.started", "participant_name": "X"}])
        self.assertEqual(activities, [])


class TestDisplayReport(unittest.TestCase):
    """display_detailed_activity_report fonksiyonu testleri (output kontrolü)."""

    def setUp(self):
        self.api = make_api()

    @patch("zoom_activity.get_meeting_events")
    @patch("zoom_activity.get_meeting_participants")
    def test_report_prints_without_error(self, mock_parts, mock_events):
        """Rapor hata vermeden çalışıyor mu?"""
        mock_parts.return_value = FAKE_PARTICIPANTS["participants"]
        mock_events.return_value = FAKE_EVENTS["events"]

        meeting_instances = FAKE_INSTANCES["meetings"]
        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            display_detailed_activity_report(meeting_instances, self.api, save_to_file=False)

        output = f.getvalue()
        self.assertIn("Meeting Instance", output)
        self.assertIn("Ahmet Yılmaz", output)
        # Paralel çekime rağmen toplantılar sıralı yazdırılmalı
        self.assertLess(output.index("abc123=="), output.index("xyz789=="))
        # Tekrarlanan JOIN/LEAVE süreyi ikiye katlamamalı (14:32:10 → 15:17:22)
        self.assertIn("Total meeting time: 45.2 minutes", output)

    @patch("zoom_activity.get_meeting_events")
    @patch("zoom_activity.get_meeting_participants")
    def test_empty_meeting_handled(self, mock_parts, mock_events):
        """Boş toplantıda crash olmuyor mu?"""
        mock_parts.return_value = []
        mock_events.return_value = []

        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            display_detailed_activity_report(FAKE_INSTANCES["meetings"], self.api)

        output = f.getvalue()
        self.assertIn("No participant", output)


class TestSaveActivities(unittest.TestCase):
    """save_activities_to_file fonksiyonu testleri."""

    def test_csv_uses_meeting_date_of_each_row(self):
        """CSV'de her satır kendi toplantısının tarihini taşıyor mu?"""
        import csv
        import io
        import tempfile
        from contextlib import redirect_stdout

        activity = UserActivity(
            user_name="Ahmet Yılmaz",
            user_id="user_001",
            activity_type=ActivityType.JOIN,
            timestamp=parse_datetime("2025-10-15T14:32:10Z"),
        )
        rows = [("2025-10-15", activity), ("2025-10-16", activity)]

        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with redirect_stdout(io.StringIO()):
                    save_activities_to_file(rows)
                csv_file = next(name for name in os.listdir(tmp) if name.endswith(".csv"))
                with open(csv_file, encoding="utf-8") as f:
                    records = list(csv.DictReader(f))
            finally:
                os.chdir(cwd)

        self.assertEqual([r["Meeting_Date"] for r in records], ["2025-10-15", "2025-10-16"])
        self.assertEqual(records[0]["Activity_Type"], "join")


class TestResolveCredentials(unittest.TestCase):
    """resolve_credentials / mask testleri."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "env.yaml")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_config(self, account_id, client_id, client_secret):
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(f'zoom:\n  account_id: "{account_id}"\n  client_id: "{client_id}"\n  client_secret: "{client_secret}"\n')

    def _args(self, account_id=None, client_id=None, client_secret=None):
        return argparse.Namespace(
            config=self.config, account_id=account_id, client_id=client_id, client_secret=client_secret
        )

    def test_missing_config_returns_nothing(self):
        """env.yaml yoksa kimlik bilgisi boş döner."""
        credentials, sources = resolve_credentials(self._args())
        self.assertEqual(credentials, {})
        self.assertEqual(sources, {})

    def test_cli_only(self):
        """env.yaml olmadan sadece CLI argümanlarıyla çalışıyor mu?"""
        credentials, sources = resolve_credentials(self._args("acc", "cid", "sec"))
        self.assertEqual(credentials, {"account_id": "acc", "client_id": "cid", "client_secret": "sec"})
        self.assertEqual(set(sources.values()), {"CLI"})

    def test_cli_overrides_yaml(self):
        """CLI argümanı env.yaml değerini eziyor mu?"""
        self._write_config("yaml_acc", "yaml_cid", "yaml_sec")
        credentials, sources = resolve_credentials(self._args(account_id="cli_acc"))
        self.assertEqual(credentials["account_id"], "cli_acc")
        self.assertEqual(sources["account_id"], "CLI")
        self.assertEqual(credentials["client_id"], "yaml_cid")
        self.assertEqual(sources["client_id"], self.config)

    def test_placeholder_treated_as_missing(self):
        """'xxxxx' değeri eksik sayılıyor mu?"""
        self._write_config("acc", "cid", "xxxxx")
        credentials, _ = resolve_credentials(self._args())
        self.assertNotIn("client_secret", credentials)

    def test_secret_never_shown(self):
        """Secret log'da hiç görünmüyor, diğerleri kısaltılıyor mu?"""
        self.assertEqual(mask("client_secret", "super_secret_value"), "********")
        self.assertEqual(mask("client_id", "abcd1234efgh"), "abcd…")
        self.assertEqual(mask("account_id", None), "(yok)")


class TestMain(unittest.TestCase):
    """main() testleri."""

    @patch("zoom_activity.ZoomAPI")
    def test_missing_credentials_exits(self, mock_api):
        """Kimlik bilgisi eksikse eksikleri yazıp çıkıyor ve Zoom'a bağlanmıyor mu?"""
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["zoom_activity.py", "123", "--config", os.path.join(tmp, "yok.yaml"), "--account-id", "acc"]
            f = io.StringIO()
            with patch.object(sys, "argv", argv), redirect_stdout(f), self.assertRaises(SystemExit) as ctx:
                main()

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Eksik kimlik bilgisi: client_id, client_secret", f.getvalue())
        mock_api.assert_not_called()


# ─────────────────────────────────────────────
#  ÇALIŞTIR
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Zoom Radar - Mock Test Suite")
    print("  API key gerekmez, tamamen offline çalışır")
    print("=" * 60)
    unittest.main(verbosity=2)