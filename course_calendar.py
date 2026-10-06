"""
Course Calendar — match a lecture to your timetable (.ics) for metadata.

Reads an iCalendar file (e.g. the TraiNex "Studienplan" export, or any
calendar's secret iCal URL) and finds the event happening right now, so a
lecture can be filed under the right course with type, room and lecturer.

Pure logic + stdlib only. The calendar is either a local file or a URL that
is downloaded and cached (used offline when the download fails).

TraiNex event format (parsed into course / type / room / lecturer):
  SUMMARY:     M13 I Multivariate Verfahren, …/Seminar - R1.0 (Seminarraum)  -  t25_hmu21
  DESCRIPTION: … - Psychologie … (D_Master PT WS 2026-3)/Carolin Hey  ab 11:30 Uhr - …
           or: … - Grp. Master …  ab 08:00 Uhr - … (Leiter/-in: Dr. Hanneke Singer)
Other calendars fall back to course = SUMMARY.
"""

import logging
import os
import re
import shutil
import time
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CALENDAR_FILE = os.path.join(APP_DIR, "calendar.ics")
CACHE_MAX_AGE_S = 6 * 3600

# How close to an event a recording may start and still be matched to it
EARLY_MATCH_MIN = 20
LATE_MATCH_MIN = 0     # 0 → anywhere up to the scheduled end


@dataclass
class CalendarEvent:
    summary: str
    start: datetime          # naive local time
    end: datetime            # naive local time
    location: str = ""
    description: str = ""
    course: str = ""
    kind: str = ""           # Vorlesung / Seminar / VT / …
    room: str = ""
    lecturer: str = ""
    program: str = ""
    extra: dict = field(default_factory=dict)

    def to_meta(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.strftime("%Y-%m-%dT%H:%M")
        d["end"] = self.end.strftime("%Y-%m-%dT%H:%M")
        d.pop("extra", None)
        return d

    @property
    def short_label(self) -> str:
        """'M13 Seminar · R1.0' — for the tray menu."""
        code = self.course.split(" ", 1)[0] if re.match(r"^M\d", self.course) else self.course[:30]
        label = f"{code} {self.kind}".strip()
        if self.room:
            m = re.match(r"^[A-Z]{1,3}\s?\d[\d.]*", self.room)
            label += f" · {m.group(0) if m else self.room[:12]}"
        return label[:60]


# ─── ICS parsing ─────────────────────────────────────────────

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _parse_dt(value: str, params: dict) -> datetime | None:
    """ICS date/time → naive local datetime."""
    value = value.strip()
    try:
        if len(value) == 8:  # all-day DATE
            return datetime.strptime(value, "%Y%m%d")
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.astimezone().replace(tzinfo=None)
        dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        tzid = params.get("TZID")
        if tzid:
            try:
                from zoneinfo import ZoneInfo
                return dt.replace(tzinfo=ZoneInfo(tzid)).astimezone().replace(tzinfo=None)
            except Exception:
                pass  # unknown zone → treat as local
        return dt
    except ValueError:
        return None


def _split_prop(line: str) -> tuple[str, dict, str]:
    if ":" not in line:
        return line.upper(), {}, ""
    head, value = line.split(":", 1)
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def parse_ics(text: str) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    current: dict | None = None
    depth = 0  # nested components inside VEVENT (e.g. VALARM)

    for line in _unfold(text):
        if not line.strip():
            continue
        name, params, value = _split_prop(line)
        if name == "BEGIN":
            if value.upper() == "VEVENT" and current is None:
                current = {}
            elif current is not None:
                depth += 1
            continue
        if name == "END":
            if current is not None and depth:
                depth -= 1
            elif current is not None and value.upper() == "VEVENT":
                events.extend(_build_events(current))
                current = None
            continue
        if current is None or depth:
            continue
        if name in ("DTSTART", "DTEND"):
            current[name] = _parse_dt(value, params)
        elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "RRULE", "DURATION", "STATUS"):
            current[name] = _unescape(value)
        elif name == "EXDATE":
            for v in value.split(","):
                dt = _parse_dt(v, params)
                if dt:
                    current.setdefault("EXDATE", set()).add(dt)

    return sorted(events, key=lambda e: e.start)


def _parse_duration(value: str) -> timedelta | None:
    m = re.match(r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", value or "")
    if not m:
        return None
    w, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _build_events(props: dict) -> list[CalendarEvent]:
    start = props.get("DTSTART")
    if start is None or props.get("STATUS", "").upper() == "CANCELLED":
        return []
    end = props.get("DTEND") or (start + (_parse_duration(props.get("DURATION", "")) or timedelta(hours=1)))
    length = end - start

    starts = [start]
    if props.get("RRULE"):
        starts = _expand_rrule(start, props["RRULE"])
    exdates = props.get("EXDATE", set())

    events = []
    for s in starts:
        if s in exdates:
            continue
        ev = CalendarEvent(
            summary=props.get("SUMMARY", "").strip(),
            start=s,
            end=s + length,
            location=props.get("LOCATION", "").strip(),
            description=props.get("DESCRIPTION", "").strip(),
        )
        _enrich(ev)
        events.append(ev)
    return events


def _expand_rrule(start: datetime, rrule: str, horizon_days: int = 400) -> list[datetime]:
    """Minimal RRULE support: DAILY/WEEKLY with INTERVAL, COUNT, UNTIL, BYDAY."""
    rule = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    freq = rule.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        return [start]
    interval = int(rule.get("INTERVAL", "1") or 1)
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = _parse_dt(rule["UNTIL"], {}) if "UNTIL" in rule else None
    limit = until or (start + timedelta(days=horizon_days))

    days = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    byday = [days[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in days]

    out: list[datetime] = []
    if freq == "DAILY":
        cur = start
        while cur <= limit and (count is None or len(out) < count):
            out.append(cur)
            cur += timedelta(days=interval)
        return out

    weekdays = sorted(byday) or [start.weekday()]
    week_start = start - timedelta(days=start.weekday())
    while week_start <= limit and (count is None or len(out) < count):
        for wd in weekdays:
            cur = week_start + timedelta(days=wd)
            if cur < start or cur > limit:
                continue
            out.append(cur)
            if count is not None and len(out) >= count:
                break
        week_start += timedelta(weeks=interval)
    return out


_TRAINEX_SUMMARY = re.compile(r"^(?P<course>.+?)/(?P<kind>[^/]+?)(?:\s+-\s+(?P<room>.+?))?\s+-\s+\S+\s*$")
_TRAINEX_REST = re.compile(r"^(?P<program>.+?)(?:/(?P<lecturer>[^/]+?))?\s+ab \d{1,2}:\d{2} Uhr")
_LECTURER_LEITER = re.compile(r"\(Leiter/-in:\s*(?P<name>[^)]+)\)")


def _enrich(ev: CalendarEvent):
    """Fill course/kind/room/lecturer/program from the event text."""
    summary = re.sub(r"\s+", " ", ev.summary).strip()
    m = _TRAINEX_SUMMARY.match(summary) if "/" in summary else None
    if not m:
        ev.course = summary
        if ev.location and not ev.location.lstrip().startswith("-"):
            ev.room = ev.location.split(" - ", 1)[0].strip()
        return

    ev.course = m.group("course").strip()
    ev.kind = m.group("kind").strip().rstrip(".")
    ev.room = (m.group("room") or "").strip()

    # Description: "<course>/<kind>[ - <room>] - <program>[/<lecturer>]  ab HH:MM Uhr - …"
    desc = re.sub(r"\s+", " ", ev.description).strip()
    rest = desc[len(ev.course) + 1:] if desc.startswith(ev.course + "/") else ""
    if ev.room and f" - {ev.room} - " in rest:
        rest = rest.split(f" - {ev.room} - ", 1)[1]
    elif " - " in rest:
        rest = rest.split(" - ", 1)[1]
    rm = _TRAINEX_REST.match(rest)
    if rm:
        ev.program = rm.group("program").strip()
        ev.lecturer = (rm.group("lecturer") or "").strip()
    lm = _LECTURER_LEITER.search(desc)
    if lm and not ev.lecturer:
        ev.lecturer = lm.group("name").strip()


# ─── Matching ────────────────────────────────────────────────

def find_current_event(events: list[CalendarEvent], now: datetime | None = None,
                       early_min: int = EARLY_MATCH_MIN) -> CalendarEvent | None:
    """
    The event that a recording started at `now` belongs to: running now, or
    starting within `early_min` minutes. If several overlap, prefer the one
    that started most recently (or starts soonest).
    """
    now = now or datetime.now()
    candidates = [
        e for e in events
        if e.start - timedelta(minutes=early_min) <= now < e.end
        and (e.end - e.start) < timedelta(hours=12)   # skip all-day events
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda e: abs((now - e.start).total_seconds()))


def events_on(events: list[CalendarEvent], day: datetime) -> list[CalendarEvent]:
    return [e for e in events if e.start.date() == day.date()]


# ─── Source management ───────────────────────────────────────

def import_file(src_path: str) -> int:
    """Copy an .ics file into the app folder. Returns number of events."""
    with open(src_path, "r", encoding="utf-8-sig", errors="replace") as f:
        n = len(parse_ics(f.read()))
    if n == 0:
        raise ValueError("No events found in that file.")
    shutil.copyfile(src_path, CALENDAR_FILE)
    return n


def refresh_from_url(url: str, timeout: float = 15.0) -> int:
    """Download the calendar from a (webcal/https) URL into the cache."""
    url = re.sub(r"^webcals?://", "https://", url.strip(), flags=re.I)
    req = urllib.request.Request(url, headers={"User-Agent": "WhisperDictation"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8-sig", errors="replace")
    n = len(parse_ics(data))
    if n == 0:
        raise ValueError("Downloaded calendar has no events.")
    tmp = CALENDAR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, CALENDAR_FILE)
    return n


class CourseCalendar:
    """Loads and caches the calendar; refreshes from URL when stale."""

    def __init__(self, url: str = "", path: str = CALENDAR_FILE):
        self.url = url
        self.path = path
        self._events: list[CalendarEvent] = []
        self._loaded_mtime = 0.0

    @property
    def available(self) -> bool:
        return bool(self.url) or os.path.exists(self.path)

    def events(self) -> list[CalendarEvent]:
        if self.url:
            try:
                age = time.time() - os.path.getmtime(self.path)
            except OSError:
                age = float("inf")
            if age > CACHE_MAX_AGE_S:
                try:
                    refresh_from_url(self.url)
                except Exception as e:
                    logger.warning(f"Calendar refresh failed (using cache): {e}")
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return []
        if mtime != self._loaded_mtime:
            with open(self.path, "r", encoding="utf-8-sig", errors="replace") as f:
                self._events = parse_ics(f.read())
            self._loaded_mtime = mtime
            logger.info(f"Calendar loaded: {len(self._events)} events")
        return self._events

    def current(self, now: datetime | None = None) -> CalendarEvent | None:
        try:
            return find_current_event(self.events(), now)
        except Exception as e:
            logger.warning(f"Calendar lookup failed: {e}")
            return None
