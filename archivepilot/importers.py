"""Importers: turn messy exports into clean archive items.

Supported today:
  * WhatsApp chat .txt exports
  * Google Takeout (JSON files, walked recursively)
  * CSV / TSV files (each row -> one item)
  * Plain text / markdown files (each line or paragraph -> one item)
  * stdin (pipe anything in)
"""

import csv
import json
import re
from pathlib import Path

_WHATSAPP_LINE = re.compile(
    r"^\s*\[(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),\s*([0-9:]+)\]\s*(.+?):\s(.*)$"
)


def import_whatsapp(path: Path, archive, source_label: str | None = None):
    """Parse a WhatsApp exported chat .txt into items."""
    label = source_label or f"whatsapp:{path.parent.name}"
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _WHATSAPP_LINE.match(line)
        if m:
            date, time_, sender, text = m.groups()
            archive.add(label, text, kind="chat",
                        created_at=f"{date} {time_}")
            n += 1
    return n


def import_takeout(path: Path, archive, source_label: str | None = None):
    """Walk a Google Takeout folder and index every JSON payload."""
    label = source_label or "google-takeout"
    n = 0
    for f in sorted(path.rglob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        for chunk in _flatten(data, max_depth=3):
            if isinstance(chunk, str) and len(chunk.strip()) > 1:
                archive.add(f"{label}:{f.parent.name}", chunk.strip())
                n += 1
    return n


def import_csv(path: Path, archive, source_label: str | None = None):
    """Each CSV row becomes one searchable item."""
    label = source_label or f"csv:{path.stem}"
    n = 0
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if not row:
                continue
            text = " | ".join(f"{k}: {v}" for k, v in row.items()
                              if v is not None and str(v).strip())
            if text.strip():
                archive.add(label, text, kind="record")
                n += 1
    return n


def import_text(path: Path, archive, source_label: str | None = None):
    """Index a plain text/markdown file (paragraph per item)."""
    label = source_label or f"file:{path.name}"
    n = 0
    text = path.read_text(encoding="utf-8", errors="replace")
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        # skip markdown headings and image-only lines
        if not para or para.lstrip().startswith("#") or \
                para.startswith("!["):
            continue
        archive.add(label, para, kind="note")
        n += 1
    return n


def import_stdin(archive, source_label: str = "stdin"):
    import sys
    text = sys.stdin.read().strip()
    if not text:
        return 0
    archive.add(source_label, text, kind="note")
    return 1


def _flatten(data, max_depth=2, depth=0):
    """Yield all leaf strings from nested JSON."""
    if depth > max_depth:
        return
    if isinstance(data, dict):
        for v in data.values():
            yield from _flatten(v, max_depth, depth + 1)
    elif isinstance(data, list):
        for v in data:
            yield from _flatten(v, max_depth, depth + 1)
    elif isinstance(data, str):
        yield data
