"""
Notion Export — one Notion page per transcribed lecture (optional Colab output).

Setup (once): create an integration at notion.so/profile/integrations, copy its
token into Colab's 🔑 Secrets as NOTION_TOKEN, and on the Notion page that
should hold the transcripts: ••• → Connections → add the integration. The
first run creates a database “Vorlesungs-Transkripte” on that page with
Kurs / Datum / Typ / Dozent / Dauer, and its pages are found again by key,
so nothing is uploaded twice.
"""

import re
import time

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
DB_TITLE = "Vorlesungs-Transkripte"
MAX_TEXT = 1900          # Notion allows 2000 characters per text object
MAX_BLOCKS = 100         # … and 100 blocks per request


def page_id_from_url(url: str) -> str | None:
    """'https://www.notion.so/Vorlesungen-1a2b…(32 hex)' → '1a2b…' as a UUID."""
    m = re.search(r"([0-9a-f]{32})(?:\?|$)", url.replace("-", "")) or \
        re.search(r"([0-9a-f]{32})", url.replace("-", ""))
    if not m:
        return None
    h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class Notion:
    def __init__(self, token: str, session=None):
        if session is None:
            import requests
            session = requests.Session()
        self.s = session
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": VERSION,
                        "Content-Type": "application/json"}

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(5):
            r = self.s.request(method, f"{API}{path}", headers=self.headers, json=body, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:           # rate limit / hiccup
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Notion {r.status_code}: {r.json().get('message', r.text)}")
            return r.json()
        raise RuntimeError("Notion keeps rate-limiting — try again later.")


def _select(name: str) -> str:
    """Notion select options can't contain commas and max out at 100 characters."""
    return re.sub(r"\s*,\s*", " · ", name or "—")[:100]


def ensure_database(notion: Notion, parent_page: str, known_id: str | None = None) -> str:
    """The transcripts database on `parent_page` (created on first use)."""
    if known_id:
        try:
            notion.call("GET", f"/databases/{known_id}")
            return known_id
        except RuntimeError:
            pass                                                     # deleted → recreate
    db = notion.call("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page},
        "title": [{"type": "text", "text": {"content": DB_TITLE}}],
        "properties": {
            "Name": {"title": {}},
            "Kurs": {"select": {}},
            "Datum": {"date": {}},
            "Typ": {"select": {}},
            "Dozent": {"rich_text": {}},
            "Dauer (min)": {"number": {}},
            "Schlüssel": {"rich_text": {}},
        },
    })
    return db["id"]


def _rich(text: str, **annotations) -> list[dict]:
    chunks = [text[i:i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)] or [""]
    return [{"type": "text", "text": {"content": c},
             **({"annotations": annotations} if annotations else {})} for c in chunks]


_LINE = re.compile(r"^(★ |⚠ )?\[(\d+:\d\d(?::\d\d)?)\]\s+(.*)$")


def blocks(text_lines: list[str]) -> list[dict]:
    """Readable transcript lines → Notion blocks (timestamps in grey)."""
    out = []
    for line in text_lines:
        if not line.strip():
            continue
        if line.startswith("— Part"):
            out.append({"type": "heading_3", "heading_3": {"rich_text": _rich(line.strip("— "))}})
            continue
        m = _LINE.match(line)
        if not m:
            out.append({"type": "paragraph", "paragraph": {"rich_text": _rich(line)}})
            continue
        mark, ts, text = m.groups()
        rich = _rich(f"{ts}  ", color="gray") + _rich(text, bold=bool(mark and "★" in mark))
        out.append({"type": "paragraph", "paragraph": {"rich_text": rich[:100]}})
    return out


def publish(groups: list[list[dict]], notion: Notion, database_id: str, lecture_text) -> int:
    """Create a page per lecture that isn't in the database yet. Returns how many were added."""
    added = 0
    for parts in groups:
        meta, key = parts[0]["meta"], parts[0]["group"]
        found = notion.call("POST", f"/databases/{database_id}/query", {
            "filter": {"property": "Schlüssel", "rich_text": {"equals": key}}})
        if found.get("results"):
            continue
        lines = lecture_text(parts).splitlines()[4:]                 # skip the text header
        props = {
            "Name": {"title": _rich(meta.get("title", "Lecture"))},
            "Kurs": {"select": {"name": _select(meta.get("course", ""))}},
            "Dauer (min)": {"number": round(sum(p["meta"].get("duration_s", 0) for p in parts) / 60)},
            "Schlüssel": {"rich_text": _rich(key)},
        }
        if meta.get("started"):
            props["Datum"] = {"date": {"start": meta["started"], "time_zone": "Europe/Berlin"}}
        if meta.get("kind"):
            props["Typ"] = {"select": {"name": _select(meta["kind"])}}
        if meta.get("lecturer"):
            props["Dozent"] = {"rich_text": _rich(meta["lecturer"])}
        body = blocks(lines)
        page = notion.call("POST", "/pages", {"parent": {"database_id": database_id},
                                              "properties": props, "children": body[:MAX_BLOCKS]})
        for i in range(MAX_BLOCKS, len(body), MAX_BLOCKS):
            notion.call("PATCH", f"/blocks/{page['id']}/children", {"children": body[i:i + MAX_BLOCKS]})
        added += 1
    return added
