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

import sys
import jwt
import requests
import datetime
import json
import os
from typing import List, Dict, Any, Optional
import argparse
from urllib.parse import quote
from dataclasses import dataclass
from enum import Enum

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
    
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()
        self._setup_session()
    
    def _setup_session(self):
        """Setup session with authentication headers."""
        payload = {
            'iss': self.api_key,
            'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        }
        jwt_token = jwt.encode(payload, self.api_secret, algorithm="HS256")
        
        self.session.headers.update({
            'Authorization': f'Bearer {jwt_token}',
            'Content-Type': 'application/json'
        })
    
    def make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Make API request with error handling."""
        url = f"{self.BASE_URL}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_data = e.response.json()
                    print(f"Error details: {error_data}")
                except:
                    pass
            raise

def parse_datetime(date_string: str) -> datetime.datetime:
    """Parse Zoom API datetime string to local datetime object."""
    return datetime.datetime.strptime(
        date_string, '%Y-%m-%dT%H:%M:%SZ'
    ).replace(tzinfo=datetime.timezone.utc).astimezone()

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

def get_meeting_participants(zoom_api: ZoomAPI, meeting_uuid: str) -> List[Dict[str, Any]]:
    """Get participants for a specific meeting instance."""
    encoded_uuid = quote(meeting_uuid, safe='')
    endpoint = f"/report/meetings/{encoded_uuid}/participants"
    
    try:
        response = zoom_api.make_request(endpoint)
        participants = response.get('participants', [])
        participants.sort(key=lambda x: (x.get('name', ''), x.get('join_time', '')))
        return participants
    except Exception as e:
        print(f"Failed to get participants for meeting {meeting_uuid}: {e}")
        return []

def get_meeting_events(zoom_api: ZoomAPI, meeting_uuid: str) -> List[Dict[str, Any]]:
    """Get detailed meeting events including user activities."""
    encoded_uuid = quote(meeting_uuid, safe='')
    endpoint = f"/report/meetings/{encoded_uuid}/events"
    
    try:
        response = zoom_api.make_request(endpoint)
        events = response.get('events', [])
        # Sort events by timestamp
        events.sort(key=lambda x: x.get('time', ''))
        return events
    except Exception as e:
        print(f"Failed to get events for meeting {meeting_uuid}: {e}")
        return []

def get_meeting_polls(zoom_api: ZoomAPI, meeting_uuid: str) -> List[Dict[str, Any]]:
    """Get meeting poll results."""
    encoded_uuid = quote(meeting_uuid, safe='')
    endpoint = f"/report/meetings/{encoded_uuid}/polls"
    
    try:
        response = zoom_api.make_request(endpoint)
        return response.get('polls', [])
    except Exception as e:
        if "404" not in str(e):  # Don't show error for meetings without polls
            print(f"Failed to get polls for meeting {meeting_uuid}: {e}")
        return []

def parse_user_activities(participants: List[Dict], events: List[Dict]) -> List[UserActivity]:
    """Parse all user activities from participants and events data."""
    activities = []
    
    # Parse basic join/leave activities from participants
    for participant in participants:
        user_name = participant.get('name', 'Unknown')
        user_id = participant.get('user_id', participant.get('id', 'unknown'))
        
        # Join activity
        if 'join_time' in participant:
            join_time = parse_datetime(participant['join_time'])
            activities.append(UserActivity(
                user_name=user_name,
                user_id=user_id,
                activity_type=ActivityType.JOIN,
                timestamp=join_time,
                details=f"Duration: {participant.get('duration', 0)} minutes"
            ))
        
        # Leave activity
        if 'leave_time' in participant:
            leave_time = parse_datetime(participant['leave_time'])
            activities.append(UserActivity(
                user_name=user_name,
                user_id=user_id,
                activity_type=ActivityType.LEAVE,
                timestamp=leave_time
            ))
    
    # Parse detailed activities from events
    for event in events:
        event_time = parse_datetime(event.get('time', ''))
        event_type = event.get('event_type', '').lower()
        user_name = event.get('participant_name', event.get('name', 'Unknown'))
        user_id = event.get('participant_user_id', event.get('user_id', 'unknown'))
        
        activity_type = None
        details = None
        
        # Map event types to activity types
        if 'join' in event_type:
            activity_type = ActivityType.JOIN
        elif 'leave' in event_type:
            activity_type = ActivityType.LEAVE
        elif 'video' in event_type or 'camera' in event_type:
            if 'start' in event_type or 'on' in event_type:
                activity_type = ActivityType.CAMERA_ON
            else:
                activity_type = ActivityType.CAMERA_OFF
        elif 'audio' in event_type or 'mic' in event_type:
            if 'unmute' in event_type or 'start' in event_type:
                activity_type = ActivityType.MIC_UNMUTE
            else:
                activity_type = ActivityType.MIC_MUTE
        elif 'screen' in event_type or 'share' in event_type:
            if 'start' in event_type:
                activity_type = ActivityType.SCREEN_SHARE_START
            else:
                activity_type = ActivityType.SCREEN_SHARE_END
        elif 'recording' in event_type:
            if 'start' in event_type:
                activity_type = ActivityType.RECORDING_START
            else:
                activity_type = ActivityType.RECORDING_END
        
        if activity_type:
            activities.append(UserActivity(
                user_name=user_name,
                user_id=user_id,
                activity_type=activity_type,
                timestamp=event_time,
                details=details or event.get('details', '')
            ))
    
    # Sort all activities by timestamp
    activities.sort(key=lambda x: x.timestamp)
    return activities

def display_detailed_activity_report(meeting_instances: List[Dict[str, Any]], zoom_api: ZoomAPI, save_to_file: bool = False):
    """Display comprehensive activity report with all user actions."""
    all_activities = []
    
    for i, meeting in enumerate(meeting_instances):
        try:
            start_time = parse_datetime(meeting['start_time'])
            print(f"\n{'='*80}")
            print(f"Meeting Instance {i+1}: {start_time.strftime('%m/%d/%Y, %I:%M:%S %p')}")
            print(f"Meeting UUID: {meeting['uuid']}")
            print(f"{'='*80}")
            
            # Get participants and events
            participants = get_meeting_participants(zoom_api, meeting['uuid'])
            events = get_meeting_events(zoom_api, meeting['uuid'])
            
            if not participants and not events:
                print("   No participant data or events found")
                continue
            
            # Parse all activities
            activities = parse_user_activities(participants, events)
            all_activities.extend(activities)
            
            if not activities:
                print("   No activities found")
                continue
            
            # Group activities by user
            users_activities = {}
            for activity in activities:
                if activity.user_name not in users_activities:
                    users_activities[activity.user_name] = []
                users_activities[activity.user_name].append(activity)
            
            # Display activities for each user
            for user_name, user_activities in users_activities.items():
                print(f"\n📊 {user_name}:")
                print(f"   {'Time':<12} {'Activity':<20} {'Details'}")
                print(f"   {'-'*60}")
                
                for activity in user_activities:
                    time_str = activity.timestamp.strftime('%I:%M:%S %p')
                    activity_str = activity.activity_type.value.replace('_', ' ').title()
                    details_str = activity.details or ''
                    
                    # Add emoji for better visualization
                    emoji = {
                        ActivityType.JOIN: "🟢",
                        ActivityType.LEAVE: "🔴",
                        ActivityType.CAMERA_ON: "📹",
                        ActivityType.CAMERA_OFF: "📷",
                        ActivityType.MIC_UNMUTE: "🎤",
                        ActivityType.MIC_MUTE: "🔇",
                        ActivityType.SCREEN_SHARE_START: "🖥️",
                        ActivityType.SCREEN_SHARE_END: "📱",
                        ActivityType.RECORDING_START: "⏺️",
                        ActivityType.RECORDING_END: "⏹️"
                    }.get(activity.activity_type, "📌")
                    
                    print(f"   {time_str:<12} {emoji} {activity_str:<18} {details_str}")
                
                # Calculate total session time
                join_times = [a for a in user_activities if a.activity_type == ActivityType.JOIN]
                leave_times = [a for a in user_activities if a.activity_type == ActivityType.LEAVE]
                
                if join_times and leave_times:
                    total_duration = sum(
                        (leave.timestamp - join.timestamp).total_seconds() / 60
                        for join, leave in zip(join_times, leave_times)
                    )
                    print(f"   Total meeting time: {total_duration:.1f} minutes")
                
        except Exception as e:
            print(f"Error processing meeting instance: {e}")
    
    # Save to file if requested
    if save_to_file and all_activities:
        save_activities_to_file(all_activities, meeting_instances)

def save_activities_to_file(activities: List[UserActivity], meeting_instances: List[Dict]):
    """Save all activities to a CSV file."""
    import csv
    from datetime import datetime
    
    filename = f"zoom_meeting_activities_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    try:
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['Meeting_Date', 'User_Name', 'User_ID', 'Activity_Type', 'Timestamp', 'Time', 'Details']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            writer.writeheader()
            for activity in activities:
                # Find corresponding meeting date
                meeting_date = "Unknown"
                for meeting in meeting_instances:
                    meeting_start = parse_datetime(meeting['start_time'])
                    if abs((activity.timestamp - meeting_start).total_seconds()) < 86400:  # Within 24 hours
                        meeting_date = meeting_start.strftime('%Y-%m-%d')
                        break
                
                writer.writerow({
                    'Meeting_Date': meeting_date,
                    'User_Name': activity.user_name,
                    'User_ID': activity.user_id,
                    'Activity_Type': activity.activity_type.value,
                    'Timestamp': activity.timestamp.isoformat(),
                    'Time': activity.timestamp.strftime('%I:%M:%S %p'),
                    'Details': activity.details or ''
                })
        
        print(f"\n Activities saved to: {filename}")
        
    except Exception as e:
        print(f"Error saving to file: {e}")

def main():
    """Main function to run the detailed participant activity report."""
    parser = argparse.ArgumentParser(
        description='Generate comprehensive Zoom meeting participant activity report'
    )
    parser.add_argument(
        'meeting_id', 
        type=int, 
        help='Zoom meeting ID'
    )
    parser.add_argument(
        '--debug', 
        action='store_true', 
        help='Enable debug output'
    )
    parser.add_argument(
        '--save', 
        action='store_true', 
        help='Save activities to CSV file'
    )
    
    args = parser.parse_args()
    
    # Get credentials
    api_key = os.getenv('ZOOM_API_KEY')
    api_secret = os.getenv('ZOOM_API_SECRET')
    
    if not api_key or not api_secret:
        print("Error: Missing Zoom API credentials!")
        print("Please set the following environment variables:")
        print("  ZOOM_API_KEY - Your Zoom API Key")
        print("  ZOOM_API_SECRET - Your Zoom API Secret")
        print("\nIn PowerShell, use:")
        print('  $env:ZOOM_API_KEY = "your_api_key"')
        print('  $env:ZOOM_API_SECRET = "your_api_secret"')
        sys.exit(1)
    
    try:
        # Initialize API client
        zoom_api = ZoomAPI(api_key, api_secret)
        
        if args.debug:
            print(f"Fetching detailed activity data for meeting ID: {args.meeting_id}")
        
        # Get meeting instances
        meeting_instances = get_meeting_instances(zoom_api, args.meeting_id)
        
        if not meeting_instances:
            print("No meeting instances found.")
            sys.exit(1)
        
        print(f"Found {len(meeting_instances)} meeting instance(s)")
        
        # Display detailed activity report
        display_detailed_activity_report(meeting_instances, zoom_api, args.save)
        
    except KeyboardInterrupt:
        print("\n Operation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)

# Entry point
if __name__ == "__main__":
    main()