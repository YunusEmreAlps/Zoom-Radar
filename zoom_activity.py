"""
Zoom Meeting Participant Report Generator with Detailed User Activity

This script retrieves comprehensive participant activity data including:
- Join/Leave events
- Camera on/off events
- Microphone mute/unmute events
- Screen sharing events
- Recording events
- And more detailed activity logs
"""

import argparse
import csv
import datetime
import logging
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Concurrent API requests; report endpoints fall under Zoom's "Heavy" rate limit
MAX_WORKERS = 4
# Largest page size the report endpoints accept
PAGE_SIZE = 300

CREDENTIAL_KEYS = ('account_id', 'client_id', 'client_secret')
PLACEHOLDER = 'xxxxx'

# Arka planda olanlar (istekler, sayfalama, parse) --debug ile bu logger üzerinden stderr'e yazılır
log = logging.getLogger("zoom_activity")

# Activity types
class ActivityType(Enum):
    """Types of user activities in Zoom meetings."""
    JOIN = "join"
    LEAVE = "leave"
    CAMERA_ON = "camera_on"
    CAMERA_OFF = "camera_off"
    MIC_UNMUTE = "mic_unmute"
    MIC_MUTE = "mic_mute"
    SCREEN_SHARE_START = "screen_share_start"
    SCREEN_SHARE_END = "screen_share_end"
    RECORDING_START = "recording_start"
    RECORDING_END = "recording_end"

ACTIVITY_EMOJI = {
    ActivityType.JOIN: "🟢",
    ActivityType.LEAVE: "🔴",
    ActivityType.CAMERA_ON: "📹",
    ActivityType.CAMERA_OFF: "📷",
    ActivityType.MIC_UNMUTE: "🎤",
    ActivityType.MIC_MUTE: "🔇",
    ActivityType.SCREEN_SHARE_START: "🖥️",
    ActivityType.SCREEN_SHARE_END: "📱",
    ActivityType.RECORDING_START: "⏺️",
    ActivityType.RECORDING_END: "⏹️",
}

@dataclass
class UserActivity:
    """Represents a single user activity event."""
    user_name: str
    user_id: str
    activity_type: ActivityType
    timestamp: datetime.datetime
    details: Optional[str] = None

class ZoomAPI:
    """Handles Zoom API authentication and requests."""

    BASE_URL = "https://api.zoom.us/v2"
    TOKEN_URL = "https://zoom.us/oauth/token"
    TIMEOUT = 30

    def __init__(self, account_id: str, client_id: str, client_secret: str):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = self._create_session()
        self._token_lock = threading.Lock()
        self._token_expires_at = 0.0
        self._ensure_token()

    @staticmethod
    def _create_session() -> requests.Session:
        """Pooled session that retries rate-limited (429) and 5xx responses with backoff."""
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            raise_on_status=False,
        )
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=MAX_WORKERS))
        return session

    def _fetch_token(self) -> Dict[str, Any]:
        """Server-to-Server OAuth ile access token al."""
        response = requests.post(
            self.TOKEN_URL,
            params={
                "grant_type": "account_credentials",
                "account_id": self.account_id,
            },
            auth=(self.client_id, self.client_secret),
            timeout=self.TIMEOUT,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Token alınamadı ({response.status_code}): {response.text}"
            )

        return response.json()

    def _ensure_token(self):
        """Token yoksa veya süresi dolmak üzereyse yenile (thread-safe)."""
        with self._token_lock:
            if time.monotonic() < self._token_expires_at:
                return
            log.debug("→ POST %s (grant_type=account_credentials, account_id=%s)",
                      self.TOKEN_URL, mask('account_id', self.account_id))
            data = self._fetch_token()
            expires_in = data.get("expires_in", 3600)
            self.session.headers["Authorization"] = f"Bearer {data['access_token']}"
            # Refresh a minute early so no request goes out with an expired token
            self._token_expires_at = time.monotonic() + expires_in - 60
            log.debug("← Access token alındı (%s sn geçerli)", expires_in)

    def make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Make API request with error handling."""
        self._ensure_token()
        url = f"{self.BASE_URL}{endpoint}"
        log.debug("→ GET %s%s", url, f" {params}" if params else "")
        started = time.monotonic()
        try:
            response = self.session.get(url, params=params, timeout=self.TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            log.debug("✗ GET %s başarısız (%.0f ms)", endpoint, (time.monotonic() - started) * 1000)
            print(f"API request failed: {e}")
            if e.response is not None:
                try:
                    print(f"Error details: {e.response.json()}")
                except ValueError:
                    pass
            raise
        log.debug("← %s: %s (%.0f ms)", endpoint, summarize_response(data), (time.monotonic() - started) * 1000)
        return data

    def get_paginated(self, endpoint: str, key: str, page_size: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch every page of a list endpoint by following next_page_token."""
        params: Dict[str, Any] = {"page_size": page_size} if page_size else {}
        items: List[Dict[str, Any]] = []
        while True:
            response = self.make_request(endpoint, params)
            items.extend(response.get(key, []))
            next_page_token = response.get("next_page_token")
            if not next_page_token:
                return items
            params["next_page_token"] = next_page_token

def parse_datetime(date_string: str) -> datetime.datetime:
    """Parse Zoom API datetime string to local datetime object."""
    return datetime.datetime.strptime(
        date_string, '%Y-%m-%dT%H:%M:%SZ'
    ).replace(tzinfo=datetime.timezone.utc).astimezone()

def mask(key: str, value: Optional[str]) -> str:
    """Log'larda kimlik bilgilerini gizle: secret hiç gösterilmez, diğerlerinin ilk 4 karakteri görünür."""
    if not value:
        return "(yok)"
    if key == 'client_secret':
        return "********"
    return f"{value[:4]}…"

def summarize_response(data: Dict[str, Any]) -> str:
    """Log satırı için yanıt özeti: liste alanlarının boyutları ve sayfalama durumu."""
    parts = [f"{key}={len(value)}" for key, value in data.items() if isinstance(value, list)]
    if data.get("next_page_token"):
        parts.append("next_page_token var → sonraki sayfa isteniyor")
    return ", ".join(parts) or "boş yanıt"

def encode_uuid(meeting_uuid: str) -> str:
    """URL-encode a meeting UUID; Zoom requires double encoding if it starts with '/' or contains '//'."""
    encoded = quote(meeting_uuid, safe='')
    if meeting_uuid.startswith('/') or '//' in meeting_uuid:
        encoded = quote(encoded, safe='')
    return encoded

def get_meeting_instances(zoom_api: ZoomAPI, meeting_id: int) -> List[Dict[str, Any]]:
    """Get all instances of a meeting."""
    endpoint = f"/past_meetings/{meeting_id}/instances"
    response = zoom_api.make_request(endpoint)

    if 'meetings' not in response:
        print(f"No meetings found. Available keys: {list(response.keys())}")
        if 'message' in response:
            print(f"API Message: {response['message']}")
        return []

    meetings = response['meetings']
    meetings.sort(key=lambda x: x['start_time'])
    return meetings

def diagnose_missing_meeting(zoom_api: ZoomAPI, meeting_id: int) -> str:
    """Explain why no ended instances were found, using the meeting's current status."""
    try:
        meeting = zoom_api.make_request(f"/meetings/{meeting_id}")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return ("Bu ID ile bir toplantı bu Zoom hesabında yok. ID'yi kontrol edin; "
                    "başka bir hesabın düzenlediği toplantılara bu uygulamayla erişilemez.")
        return f"Toplantı durumu alınamadı: {e}"
    except requests.exceptions.RequestException as e:
        return f"Toplantı durumu alınamadı: {e}"

    log.debug("Toplantı durumu: %s", meeting.get('status'))
    if meeting.get('status') == 'started':
        return ("Toplantı şu an devam ediyor. Zoom yalnızca bitmiş toplantıları listeler ve katılımcı "
                "raporunu toplantı bittikten sonra oluşturur; toplantı bitince (birkaç dakika sonra) tekrar deneyin.")
    return ("Toplantı mevcut ama bitmiş bir oturumu yok. Henüz yapılmamış, yeni bitmiş (rapor birkaç dakika "
            "sürebilir) ya da sadece host katılmış olabilir (Zoom tek kişilik oturumları listelemez).")

def get_meeting_participants(zoom_api: ZoomAPI, meeting_uuid: str) -> List[Dict[str, Any]]:
    """Get participants for a specific meeting instance."""
    endpoint = f"/report/meetings/{encode_uuid(meeting_uuid)}/participants"

    try:
        participants = zoom_api.get_paginated(endpoint, 'participants', page_size=PAGE_SIZE)
        participants.sort(key=lambda x: (x.get('name', ''), x.get('join_time', '')))
        return participants
    except Exception as e:
        print(f"Failed to get participants for meeting {meeting_uuid}: {e}")
        return []

def get_meeting_events(zoom_api: ZoomAPI, meeting_uuid: str) -> List[Dict[str, Any]]:
    """Get detailed meeting events including user activities."""
    endpoint = f"/report/meetings/{encode_uuid(meeting_uuid)}/events"

    try:
        events = zoom_api.get_paginated(endpoint, 'events')
        events.sort(key=lambda x: x.get('time', ''))
        return events
    except Exception as e:
        print(f"Failed to get events for meeting {meeting_uuid}: {e}")
        return []

def get_meeting_polls(zoom_api: ZoomAPI, meeting_uuid: str) -> List[Dict[str, Any]]:
    """Get meeting poll results."""
    endpoint = f"/report/meetings/{encode_uuid(meeting_uuid)}/polls"

    try:
        response = zoom_api.make_request(endpoint)
        return response.get('polls', [])
    except Exception as e:
        if "404" not in str(e):
            print(f"Failed to get polls for meeting {meeting_uuid}: {e}")
        return []

def fetch_meeting_data(
    zoom_api: ZoomAPI, meeting_instances: List[Dict[str, Any]]
) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """Fetch participants and events for all instances concurrently, keeping instance order."""
    log.debug("%d toplantı için katılımcı + event istekleri %d paralel worker ile gönderiliyor",
              len(meeting_instances), MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="worker") as pool:
        futures = [
            (pool.submit(get_meeting_participants, zoom_api, meeting['uuid']),
             pool.submit(get_meeting_events, zoom_api, meeting['uuid']))
            for meeting in meeting_instances
        ]
        results = [(participants.result(), events.result()) for participants, events in futures]
    log.debug("Tüm veriler alındı, rapor oluşturuluyor")
    return results

def classify_event(event_type: str) -> Optional[ActivityType]:
    """Map a raw Zoom event type (e.g. 'video.started') to an ActivityType."""
    event_type = event_type.lower()

    if 'join' in event_type:
        return ActivityType.JOIN
    if 'leave' in event_type or 'left' in event_type:
        return ActivityType.LEAVE
    if 'video' in event_type or 'camera' in event_type:
        if 'start' in event_type or 'on' in event_type:
            return ActivityType.CAMERA_ON
        return ActivityType.CAMERA_OFF
    if 'audio' in event_type or 'mic' in event_type:
        if 'unmute' in event_type or 'start' in event_type:
            return ActivityType.MIC_UNMUTE
        return ActivityType.MIC_MUTE
    if 'screen' in event_type or 'share' in event_type:
        if 'start' in event_type:
            return ActivityType.SCREEN_SHARE_START
        return ActivityType.SCREEN_SHARE_END
    if 'recording' in event_type:
        if 'start' in event_type:
            return ActivityType.RECORDING_START
        return ActivityType.RECORDING_END
    return None

def parse_user_activities(participants: List[Dict], events: List[Dict]) -> List[UserActivity]:
    """Parse all user activities from participants and events data."""
    activities = []
    skipped_types = []

    for participant in participants:
        user_name = participant.get('name', 'Unknown')
        user_id = participant.get('user_id', participant.get('id', 'unknown'))

        if 'join_time' in participant:
            # Zoom reports participant duration in seconds
            duration_minutes = round(participant.get('duration', 0) / 60)
            activities.append(UserActivity(
                user_name=user_name,
                user_id=user_id,
                activity_type=ActivityType.JOIN,
                timestamp=parse_datetime(participant['join_time']),
                details=f"Duration: {duration_minutes} minutes"
            ))

        if 'leave_time' in participant:
            activities.append(UserActivity(
                user_name=user_name,
                user_id=user_id,
                activity_type=ActivityType.LEAVE,
                timestamp=parse_datetime(participant['leave_time'])
            ))

    for event in events:
        activity_type = classify_event(event.get('event_type', ''))
        if not activity_type or not event.get('time'):
            skipped_types.append(event.get('event_type') or '?')
            continue

        activities.append(UserActivity(
            user_name=event.get('participant_name', event.get('name', 'Unknown')),
            user_id=event.get('participant_user_id', event.get('user_id', 'unknown')),
            activity_type=activity_type,
            timestamp=parse_datetime(event['time']),
            details=event.get('details', '')
        ))

    # Join/leave can appear in both participants and events; keep the first (participant) copy
    unique = {}
    for activity in activities:
        unique.setdefault((activity.user_name, activity.activity_type, activity.timestamp), activity)

    log.debug(
        "Parse: %d katılımcı + %d event → %d aktivite (%d tekrar birleştirildi, %d event atlandı%s)",
        len(participants), len(events), len(unique), len(activities) - len(unique), len(skipped_types),
        f": {', '.join(sorted(set(skipped_types)))}" if skipped_types else "",
    )

    return sorted(unique.values(), key=lambda x: x.timestamp)

def display_detailed_activity_report(meeting_instances: List[Dict[str, Any]], zoom_api: ZoomAPI, save_to_file: bool = False):
    """Display comprehensive activity report with all user actions."""
    csv_rows: List[Tuple[str, UserActivity]] = []
    meeting_data = fetch_meeting_data(zoom_api, meeting_instances)

    for i, (meeting, (participants, events)) in enumerate(zip(meeting_instances, meeting_data), 1):
        try:
            start_time = parse_datetime(meeting['start_time'])
            print(f"\n{'='*80}")
            print(f"Meeting Instance {i}: {start_time.strftime('%m/%d/%Y, %I:%M:%S %p')}")
            print(f"Meeting UUID: {meeting['uuid']}")
            print(f"{'='*80}")

            if not participants and not events:
                print("   No participant data or events found")
                continue

            activities = parse_user_activities(participants, events)

            if not activities:
                print("   No activities found")
                continue

            meeting_date = start_time.strftime('%Y-%m-%d')
            csv_rows.extend((meeting_date, activity) for activity in activities)

            users_activities = defaultdict(list)
            for activity in activities:
                users_activities[activity.user_name].append(activity)

            for user_name, user_activities in users_activities.items():
                print(f"\n📊 {user_name}:")
                print(f"   {'Time':<12} {'Activity':<20} {'Details'}")
                print(f"   {'-'*60}")

                for activity in user_activities:
                    time_str = activity.timestamp.strftime('%I:%M:%S %p')
                    activity_str = activity.activity_type.value.replace('_', ' ').title()
                    emoji = ACTIVITY_EMOJI.get(activity.activity_type, "📌")
                    print(f"   {time_str:<12} {emoji} {activity_str:<18} {activity.details or ''}")

                join_times = [a.timestamp for a in user_activities if a.activity_type == ActivityType.JOIN]
                leave_times = [a.timestamp for a in user_activities if a.activity_type == ActivityType.LEAVE]

                if join_times and leave_times:
                    total_duration = sum(
                        (leave - join).total_seconds() for join, leave in zip(join_times, leave_times)
                    ) / 60
                    print(f"   Total meeting time: {total_duration:.1f} minutes")

        except Exception as e:
            print(f"Error processing meeting instance: {e}")

    if save_to_file and csv_rows:
        save_activities_to_file(csv_rows)

def save_activities_to_file(rows: List[Tuple[str, UserActivity]]):
    """Save (meeting_date, activity) rows to a CSV file."""
    filename = f"zoom_meeting_activities_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    try:
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['Meeting_Date', 'User_Name', 'User_ID', 'Activity_Type', 'Timestamp', 'Time', 'Details']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            writer.writerows({
                'Meeting_Date': meeting_date,
                'User_Name': activity.user_name,
                'User_ID': activity.user_id,
                'Activity_Type': activity.activity_type.value,
                'Timestamp': activity.timestamp.isoformat(),
                'Time': activity.timestamp.strftime('%I:%M:%S %p'),
                'Details': activity.details or ''
            } for meeting_date, activity in rows)

        print(f"\n✅ Activities saved to: {filename}")

    except Exception as e:
        print(f"Error saving to file: {e}")

def load_credentials_from_yaml(yaml_path: str = 'env.yaml') -> Dict[str, Any]:
    """Read the 'zoom' section of the YAML config; returns {} if the file does not exist."""
    config_path = Path(yaml_path)
    if not config_path.exists():
        return {}

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        return config.get('zoom') or {}
    except Exception as e:
        print(f"Hata: {yaml_path} dosyası okunurken bir sorun oluştu: {e}")
        sys.exit(1)

def resolve_credentials(args: argparse.Namespace) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Collect credentials (CLI arguments override env.yaml); returns (credentials, source of each).

    Empty or 'xxxxx' placeholder values are left out, so they show up as missing.
    """
    yaml_values = load_credentials_from_yaml(args.config)
    credentials: Dict[str, str] = {}
    sources: Dict[str, str] = {}

    for key in CREDENTIAL_KEYS:
        for source, value in (("CLI", getattr(args, key)), (args.config, yaml_values.get(key))):
            if value and value != PLACEHOLDER:
                credentials[key] = value
                sources[key] = source
                break

    return credentials, sources

def setup_output_encoding():
    """Force UTF-8, line-buffered stdout/stderr."""
    # Windows'ta çıktı dosyaya yönlendirildiğinde emoji'ler UnicodeEncodeError vermesin;
    # line buffering, rapor (stdout) ile log (stderr) satırlarının sırasını korur
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', line_buffering=True)

def setup_logging(verbose: bool):
    """Configure the background log (stderr)."""
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d [%(threadName)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    if verbose:
        log.setLevel(logging.DEBUG)
        # urllib3 logs 429/5xx retries here
        logging.getLogger("urllib3.util.retry").setLevel(logging.DEBUG)

def main():
    """Main function to run the detailed participant activity report."""
    setup_output_encoding()
    parser = argparse.ArgumentParser(
        description='Generate comprehensive Zoom meeting participant activity report'
    )
    parser.add_argument('meeting_id', type=int, help='Zoom meeting ID')
    parser.add_argument('--debug', action='store_true', help='Arka planda olanları göster (istekler, sayfalama, parse)')
    parser.add_argument('--save', action='store_true', help='Save activities to CSV file')
    parser.add_argument('--config', default='env.yaml', help='Path to YAML config file (default: env.yaml)')
    parser.add_argument('--account-id', help='Zoom Account ID (env.yaml değerini ezer)')
    parser.add_argument('--client-id', help='Zoom Client ID (env.yaml değerini ezer)')
    parser.add_argument('--client-secret', help='Zoom Client Secret (env.yaml değerini ezer)')

    args = parser.parse_args()
    setup_logging(verbose=args.debug)

    # ── Kimlik bilgilerini CLI argümanlarından veya env.yaml'dan topla ───
    credentials, sources = resolve_credentials(args)
    # ────────────────────────────────────────────────────────────────────────

    for key in CREDENTIAL_KEYS:
        log.debug("%-13s = %s (kaynak: %s)", key, mask(key, credentials.get(key)), sources.get(key, "yok"))

    missing = [key for key in CREDENTIAL_KEYS if key not in credentials]
    if missing:
        print(f"Hata: Eksik kimlik bilgisi: {', '.join(missing)}")
        print(f"\n'{args.config}' dosyasını doldurun veya --account-id / --client-id / --client-secret ile verin.")
        print("\nÖrnek env.yaml formatı:")
        print('zoom:\n'
              '  account_id: "your_account_id"\n'
              '  client_id: "your_client_id"\n'
              '  client_secret: "your_client_secret"')
        sys.exit(1)

    try:
        zoom_api = ZoomAPI(*(credentials[key] for key in CREDENTIAL_KEYS))

        log.debug("✅ Zoom API bağlantısı kuruldu")
        log.debug("📋 Meeting ID: %s", args.meeting_id)

        meeting_instances = get_meeting_instances(zoom_api, args.meeting_id)

        if not meeting_instances:
            print("Toplantı bulunamadı.")
            print(f"   {diagnose_missing_meeting(zoom_api, args.meeting_id)}")
            sys.exit(1)

        print(f"Found {len(meeting_instances)} meeting instance(s)")
        display_detailed_activity_report(meeting_instances, zoom_api, args.save)

    except KeyboardInterrupt:
        print("\n⚠️ İşlem kullanıcı tarafından iptal edildi.")
        sys.exit(1)
    except Exception as e:
        print(f"Beklenmeyen hata: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
