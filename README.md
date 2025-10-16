# Zoom Meeting Participant Report Generator

An optimized Python script that retrieves comprehensive participant reports for all instances of a Zoom meeting using the Zoom REST API. This tool is ideal for educators and administrators who need detailed attendance tracking.

> Requires Zoom account with **Pro** plan or higher and **REST API** enabled.
> Requires Python 3.7+.

## Features

- ✅ **Object-oriented design** with proper separation of concerns
- ✅ **Robust error handling** with detailed error messages
- ✅ **Type hints** for better code maintainability
- ✅ **Session management** for efficient API requests
- ✅ **Automatic URL encoding** for meeting UUIDs
- ✅ **Improved formatting** with aligned output
- ✅ **Debug mode** for troubleshooting
- ✅ **Graceful interrupt handling**

## Requirements

```bash
pip install requests pyjwt
```

## Setup Instructions

### 1. Set Environment Variables

```powershell
# Windows PowerShell
$env:ZOOM_API_KEY = <"your_zoom_api_key">
$env:ZOOM_API_SECRET = <"your_zoom_api_secret">
```

```bash
# Linux/macOS
export ZOOM_API_KEY=<"your_zoom_api_key">
export ZOOM_API_SECRET=<"your_zoom_api_secret">
```

### 2. Enable REST API in Zoom Account

1. Sign in to [zoom.us](https://zoom.us)
2. Navigate to: **Account Management → Account Settings → Other**
3. Find **"REST API"** and toggle it **ON**
4. Save changes

### 3. Run the Script

```bash
# Basic usage
python zoom_activity.py <meeting_id>

# With debug output
python zoom_activity.py <meeting_id> --debug

# Example
python zoom_activity.py 91202248508 --debug
```

## Sample Output

```bash
Found 2 meeting instance(s)

Meeting start: 10/15/2025, 2:30:15 PM
   John Doe                        45.2 minutes: 2:32:10 PM to 3:17:22 PM
   Jane Smith                      42.8 minutes: 2:35:05 PM to 3:17:55 PM
   Mike Johnson                    38.1 minutes: 2:39:30 PM to 3:17:36 PM

Meeting start: 10/16/2025, 10:00:00 AM
   Alice Brown                     60.0 minutes: 10:00:15 AM to 11:00:15 AM
   Bob Wilson                      58.5 minutes: 10:01:45 AM to 11:00:15 AM
```

## Troubleshooting

| Error                                     | Solution                                                       |
|-------------------------------------------|----------------------------------------------------------------|
| `Account does not enabled REST API`       | Enable REST API in your Zoom account settings                  |
| `Missing Zoom API credentials`            | Set `ZOOM_API_KEY` and `ZOOM_API_SECRET` environment variables |
| `Import "requests" could not be resolved` | Install requests: `pip install requests`                       |
| `JWT authentication issues`               | Verify your API key and secret are correct                     |
| `Meeting not found`                       | Ensure the meeting ID is correct and the meeting has occurred  |
