"""
Reads the Participants panel of the Zoom desktop app through Windows UI Automation.

This is the only file that depends on Zoom's UI (and may break when Zoom updates it);
zoom_attendance.py and attendance.py only work with the list of names it returns.

Layout verified on Zoom Workplace 7.1.9:
    Text  'Participants (3)'
    List  'Participant list, use arrow key to navigate'
      ListItem 'Ad Soyad,(Host, me), Computer audio unmuted,Video off, Press tab for more options'
"""

import ctypes
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

import uiautomation as auto

ZOOM_PROCESS_NAME = "zoom.exe"
# Main "Zoom Workplace" window (chat, contacts); holds no meeting data, so it is not scanned
MAIN_WINDOW_CLASS = "ZPPTMainFrmWndClassEx"

# Panel title: "Participants (12)" / Turkish UI "Katılımcılar (12)"
PANEL_TITLE = re.compile(r"(participants|katılımcılar)\s*\((\d+)\)", re.IGNORECASE)
PARTICIPANT_HINT = re.compile(r"particip|katılımcı|plist", re.IGNORECASE)

# Role suffixes in item labels, English and Turkish UI: "(Host, me)", "(Co-host)", "(Ben)" ...
ROLE_WORDS = {"host", "co-host", "cohost", "me", "guest", "ben", "sunucu", "ortak sunucu",
              "yardımcı sunucu", "toplantı sahibi", "misafir", "konuk"}
SELF_WORDS = {"me", "ben"}

MAX_DEPTH = 30


@dataclass
class ParticipantSnapshot:
    """One read of the Participants panel."""
    names: List[str]                # everyone except yourself
    expected_count: Optional[int]   # count in the panel title (includes yourself)
    self_name: Optional[str] = None

    @property
    def read_count(self) -> int:
        return len(self.names) + (1 if self.self_name else 0)


# ─────────────────────────────────────────────
#  Finding windows
# ─────────────────────────────────────────────

_process_names: Dict[int, str] = {}

def _process_name(pid: int) -> str:
    """Executable name of a process (cached; PIDs of a running Zoom do not change)."""
    if pid not in _process_names:
        name = ""
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                buffer = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_uint32(len(buffer))
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    name = os.path.basename(buffer.value).lower()
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        _process_names[pid] = name
    return _process_names[pid]

def find_zoom_windows(include_main: bool = False) -> List[auto.Control]:
    """Top-level windows that belong to Zoom.exe (meeting, pop-out participants, ...)."""
    windows = []
    for window in auto.GetRootControl().GetChildren():
        if _process_name(window.ProcessId) != ZOOM_PROCESS_NAME:
            continue
        if not include_main and window.ClassName == MAIN_WINDOW_CLASS:
            continue
        windows.append(window)
    return windows

def _walk(control: auto.Control, max_depth: int = MAX_DEPTH) -> Iterator[Tuple[auto.Control, int]]:
    """Yield (control, depth) depth-first, starting with the control itself."""
    yield from auto.WalkControl(control, includeTop=True, maxDepth=max_depth)


# ─────────────────────────────────────────────
#  Parsing item labels
# ─────────────────────────────────────────────

def _split_top_level(text: str) -> List[str]:
    """Split on commas that are not inside parentheses."""
    parts, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts]

def _roles(text: str) -> Optional[List[str]]:
    """Role words inside "(Host, me)", or None if the parentheses hold something else."""
    roles = [part.strip().lower() for part in text.split(",")]
    return roles if roles and all(role in ROLE_WORDS for role in roles) else None

def parse_item(label: str) -> Tuple[str, List[str]]:
    """Participant list item label → (display name, roles).

    "Ad Soyad,(Host, me), Computer audio unmuted, ..." → ("Ad Soyad", ["host", "me"])
    Older layout "Ad Soyad (Host, me), ..." is handled too; "Ahmet (IT)" keeps its parentheses.
    """
    parts = _split_top_level(label)
    name, roles = parts[0], []
    trailing = re.search(r"\(([^()]*)\)\s*$", name)
    if trailing and _roles(trailing.group(1)) is not None:
        roles = _roles(trailing.group(1))
        name = name[:trailing.start()].strip()
    elif len(parts) > 1:
        separate = re.fullmatch(r"\(([^()]*)\)", parts[1])
        if separate and _roles(separate.group(1)) is not None:
            roles = _roles(separate.group(1))
    return name or label.strip(), roles

def _expected_count(text: Optional[str]) -> Optional[int]:
    match = PANEL_TITLE.search(text or "")
    return int(match.group(2)) if match else None


# ─────────────────────────────────────────────
#  Reading participants
# ─────────────────────────────────────────────

def _list_items(list_control: auto.Control) -> List[str]:
    return [item.Name for item in list_control.GetChildren()
            if item.ControlTypeName == "ListItemControl" and item.Name]

class ParticipantReader:
    """Reads the Participants panel; remembers where the list is so later reads are fast."""

    def __init__(self):
        self._list: Optional[auto.Control] = None
        self._title: Optional[auto.Control] = None

    def read(self) -> Optional[ParticipantSnapshot]:
        """Current participants, or None if the panel is closed / Zoom is not in a meeting."""
        for _ in range(2):  # cached location first, then one fresh search
            if self._list is None and not self._locate():
                return None
            try:
                labels = _list_items(self._list)
                expected = _expected_count(self._title.Name) if self._title is not None else None
            except Exception:  # COMError: panel closed or rebuilt since the last read
                labels = []
            if labels:
                return self._snapshot(labels, expected)
            self._list = self._title = None
        return None

    def _locate(self) -> bool:
        """Find the participant list (and its title) in Zoom's windows."""
        best = None
        for window in find_zoom_windows():
            title = window if _expected_count(window.Name) is not None else None
            for control, _ in _walk(window):
                control_type = control.ControlTypeName
                if title is None and control_type == "TextControl" and _expected_count(control.Name) is not None:
                    title = control
                if control_type != "ListControl":
                    continue
                labels = _list_items(control)
                if not labels:
                    continue
                score = 0
                if PARTICIPANT_HINT.search(f"{control.Name} {control.AutomationId} {control.ClassName}"):
                    score += 2
                if title is not None and _expected_count(title.Name) == len(labels):
                    score += 1
                if best is None or score > best[0]:
                    best = (score, control, title)
        if best is None:
            return False
        _, self._list, self._title = best
        return True

    @staticmethod
    def _snapshot(labels: List[str], expected: Optional[int]) -> ParticipantSnapshot:
        names, self_name = [], None
        for label in labels:
            name, roles = parse_item(label)
            if self_name is None and SELF_WORDS & set(roles):
                self_name = name
            else:
                names.append(name)
        return ParticipantSnapshot(names=names, expected_count=expected, self_name=self_name)


# ─────────────────────────────────────────────
#  Diagnostics (zoom_attendance.py check)
# ─────────────────────────────────────────────

def mask_text(text: str) -> str:
    """Keep the first letter of each word, digits and punctuation: "Ahmet Yılmaz (12)" → "A**** Y***** (12)"."""
    return re.sub(r"\w+", lambda m: m.group(0) if m.group(0).isdigit()
                  else m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)

def dump_tree(mask: bool = True, include_main: bool = False, max_elements: int = 3000) -> List[str]:
    """Describe the UI tree of Zoom windows (names masked by default) to diagnose the panel layout."""
    lines = []
    for window in find_zoom_windows(include_main):
        for control, depth in _walk(window):
            if len(lines) >= max_elements:
                lines.append(f"... stopped at the {max_elements} element limit")
                return lines
            name = mask_text(control.Name or "") if mask else control.Name or ""
            lines.append(
                f"{'  ' * depth}{control.ControlTypeName.replace('Control', '')}"
                f" class={control.ClassName!r} id={control.AutomationId!r} name={name!r}"
            )
    return lines
