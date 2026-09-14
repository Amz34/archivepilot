"""Microsoft 365 ingestion source for ArchivePilot (Microsoft Graph).

Three flavours, matching where enterprise knowledge actually lives:

    sharepoint : a SharePoint site's document libraries (drive items)
    onedrive   : one drive id (OneDrive, or a single SharePoint library)
    teams      : channel messages and their replies

``archivepilot.graph`` does the HTTP; this module only *parses and maps*
Graph entities onto the record ArchivePilot already indexes —
``Archive.add(source, text, kind, created_at)``. Only scalar fields are
copied out of the Graph payloads, so raw Graph JSON never lands in the
index, and bilingual (EN + AR) search works exactly as it does for every
other source because the text goes through the existing normalizer.

Credentials (env only, never logged): MSGRAPH_TENANT_ID, MSGRAPH_CLIENT_ID,
MSGRAPH_CLIENT_SECRET.

Usage:

    python -m archivepilot.ingest --source sharepoint --site root --dry-run
    python -m archivepilot.ingest --source sharepoint --site <site-id>
    python -m archivepilot.ingest --source onedrive --drive <drive-id>
    python -m archivepilot.ingest --source teams --team <team-id>
    python -m archivepilot.ingest --source teams --team <id> --channel <id>

Exit codes: 0 ok, 2 missing credentials / bad arguments, 3 Graph failure.
"""

from __future__ import annotations

import argparse
import html
import io
import re
import sys
import zipfile
from pathlib import Path

from .db import Archive
from .graph import (DEFAULT_TIMEOUT, MAX_FOLDER_DEPTH, GraphClient,
                    GraphCredentials, GraphError, MissingCredentials,
                    client_from_env)

DEFAULT_DB = "~/.archivepilot/archive.db"

# Extensions we can turn into searchable text with the standard library.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonl", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".tex",
    ".eml", ".html", ".htm",
}
# OOXML is a zip of XML — extractable with zipfile + re, no dependencies.
OOXML_EXTENSIONS = {".docx", ".pptx"}
INDEXABLE_EXTENSIONS = TEXT_EXTENSIONS | OOXML_EXTENSIONS

MAX_ITEM_CHARS = 200_000
_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6]|/w:p|/a:p)\s*/?>")
_REPLY_PREFIX = "  -> "


# ----------------------------------------------------------------------
# text extraction (stdlib only — the repo has no dependencies)
# ----------------------------------------------------------------------
def is_indexable(name: str) -> bool:
    """True when we know how to pull text out of this file name."""
    return Path(name or "").suffix.lower() in INDEXABLE_EXTENSIONS


def strip_markup(markup: str) -> str:
    """Turn HTML/XML into plain text (Teams bodies arrive as HTML)."""
    if not markup:
        return ""
    text = _BLOCK_TAG.sub("\n", markup)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def extract_text(name: str, data: bytes) -> str:
    """Best-effort text for a downloaded drive item ('' when nothing extractable)."""
    suffix = Path(name or "").suffix.lower()
    if suffix in OOXML_EXTENSIONS:
        text = _extract_ooxml(data)
    elif suffix in (".html", ".htm"):
        text = strip_markup(data.decode("utf-8", errors="replace"))
    else:
        text = data.decode("utf-8", errors="replace")
    return text.strip()[:MAX_ITEM_CHARS]


def _extract_ooxml(data: bytes) -> str:
    """Pull visible text out of a .docx/.pptx (zip + XML), no dependencies."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            names = [
                name for name in bundle.namelist()
                if name == "word/document.xml"
                or (re.fullmatch(r"ppt/slides/slide\d+\.xml", name) is not None)
            ]
            chunks = []
            for name in sorted(names):
                xml = bundle.read(name).decode("utf-8", errors="replace")
                chunks.append(strip_markup(xml))
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    return "\n".join(chunk for chunk in chunks if chunk)


# ----------------------------------------------------------------------
# record mapping: Graph entity -> Archive.add() kwargs
# ----------------------------------------------------------------------
def folder_path(item: dict) -> str:
    """Human folder path from a driveItem's parentReference ('' when root)."""
    parent = item.get("parentReference") if isinstance(item, dict) else None
    raw = str((parent or {}).get("path") or "")
    if "root:" in raw:
        raw = raw.split("root:", 1)[1]
    return raw.strip("/")


def map_drive_item(item: dict, *, text: str, source: str = "sharepoint",
                   path: str | None = None) -> dict | None:
    """Map a Graph driveItem + its extracted text onto an archive record.

    Returns kwargs for ``Archive.add``, or ``None`` when the item carries
    nothing worth indexing (binary type, empty body, missing name).
    """
    name = str((item or {}).get("name") or "").strip()
    if not name or not is_indexable(name):
        return None
    body = (text or "").strip()
    if not body:
        return None
    folder = folder_path(item) if path is None else path
    header = f"Document: {name}"
    if folder:
        header += f"\nFolder: {folder}"
    return {
        "source": source,
        "text": f"{header}\n\n{body[:MAX_ITEM_CHARS]}",
        "kind": "document",
        "created_at": item.get("lastModifiedDateTime") or item.get("createdDateTime"),
    }


def _message_body_text(message: dict) -> str:
    body = (message or {}).get("body") or {}
    content = str(body.get("content") or "")
    if str(body.get("contentType") or "").lower() == "html":
        return strip_markup(content)
    return " ".join(content.split())


def _sender_name(message: dict) -> str:
    user = ((message or {}).get("from") or {}).get("user") or {}
    return str(user.get("displayName") or user.get("id") or "unknown").strip()


def map_teams_message(message: dict, *, source: str = "teams",
                      replies: list | None = None) -> dict | None:
    """Map a Graph chatMessage (+ replies) onto an archive record.

    Returns kwargs for ``Archive.add``, or ``None`` for empty/system
    messages that carry no searchable text.
    """
    body_text = _message_body_text(message)
    lines: list[str] = []
    subject = str((message or {}).get("subject") or "").strip()
    if subject:
        lines.append(f"Subject: {subject}")
    if body_text:
        lines.append(f"{_sender_name(message)}: {body_text}")
    for reply in replies or []:
        reply_text = _message_body_text(reply)
        if reply_text:
            lines.append(f"{_REPLY_PREFIX}{_sender_name(reply)}: {reply_text}")
    if not lines:
        return None
    created = (
        (message or {}).get("createdDateTime")
        or (message or {}).get("lastModifiedDateTime")
        or (message or {}).get("publishedDateTime")
    )
    return {
        "source": source,
        "text": "\n".join(lines)[:MAX_ITEM_CHARS],
        "kind": "chat",
        "created_at": created,
    }


# ----------------------------------------------------------------------
# ingestion runs
# ----------------------------------------------------------------------
def _new_stats(source: str) -> dict:
    return {"source": source, "planned": 0, "indexed": 0, "skipped": 0,
            "failed": 0}


def _considered(stats: dict) -> int:
    return stats["planned"] + stats["indexed"] + stats["skipped"] + stats["failed"]


def _remaining(limit: int | None, stats: dict) -> int | None:
    if limit is None:
        return None
    return max(0, limit - _considered(stats))


def _ingest_drive(client: GraphClient, archive, drive_id: str, drive_name: str, *,
                  source: str, dry_run: bool, limit: int | None, out) -> dict:
    stats = _new_stats(source)
    if not drive_id:
        return stats
    for ref in client.walk_drive(drive_id, limit=_remaining(limit, stats)):
        item = ref.item
        name = str(item.get("name") or "?")
        where = f"{source} / {drive_name}" + (f" / {ref.path}" if ref.path else "")
        if not is_indexable(name) or not item.get("id"):
            stats["skipped"] += 1
            continue
        if dry_run:
            stats["planned"] += 1
            out(f"[dry-run] would index {where} / {name} "
                f"({item.get('size') or 0} bytes, {item.get('file', {}).get('mimeType') or 'unknown type'})")
            continue
        if archive is None:
            raise GraphError("internal error: ingestion needs an archive unless --dry-run")
        try:
            data = client.download(drive_id, str(item["id"]))
        except GraphError as exc:
            stats["failed"] += 1
            out(f"[warn] could not download {where} / {name}: {exc}")
            continue
        record = map_drive_item(item, text=extract_text(name, data), source=source,
                                path=ref.path)
        if record is None:
            stats["skipped"] += 1
            out(f"[skip] {where} / {name} (no indexable text)")
            continue
        archive.add(**record)
        stats["indexed"] += 1
    return stats


def ingest_sharepoint(client: GraphClient, archive=None, *, site_id: str = "root",
                      site_name: str | None = None, dry_run: bool = False,
                      limit: int | None = None, out=None) -> dict:
    """Index every document library of a SharePoint site."""
    out = out or (lambda line: print(line))
    site = client.get_site(site_id)
    label = (site_name or site.get("displayName") or site.get("name")
             or site_id or "root")
    stats = _new_stats(f"sharepoint:{label}")
    for drive in client.list_drives(site_id):
        remaining = _remaining(limit, stats)
        if remaining == 0:
            break
        drive_stats = _ingest_drive(
            client, archive, str(drive.get("id") or ""),
            str(drive.get("name") or drive.get("id") or "drive"),
            source=stats["source"], dry_run=dry_run, limit=remaining, out=out,
        )
        _merge(stats, drive_stats)
    return stats


def ingest_onedrive(client: GraphClient, archive=None, *, drive_id: str,
                    label: str | None = None, dry_run: bool = False,
                    limit: int | None = None, out=None) -> dict:
    """Index a single drive (OneDrive, or one SharePoint library)."""
    out = out or (lambda line: print(line))
    drive = client.get_drive(drive_id)
    drive_name = str(drive.get("name") or label or drive_id)
    return _ingest_drive(
        client, archive, drive_id, drive_name,
        source=f"onedrive:{label or drive_name}", dry_run=dry_run, limit=limit,
        out=out,
    )


def ingest_teams(client: GraphClient, archive=None, *, team_id: str,
                 channel_id: str | None = None, include_replies: bool = True,
                 dry_run: bool = False, limit: int | None = None, out=None) -> dict:
    """Index Teams channel messages (and their replies)."""
    out = out or (lambda line: print(line))
    team = client.get_team(team_id)
    team_label = str(team.get("displayName") or team_id)
    if channel_id:
        channels = [{"id": channel_id, "displayName": channel_id}]
    else:
        channels = list(client.list_channels(team_id))
    stats = _new_stats(f"teams:{team_label}")
    for channel in channels:
        remaining = _remaining(limit, stats)
        if remaining == 0:
            break
        cid = str(channel.get("id") or "")
        channel_label = str(channel.get("displayName") or channel.get("name") or cid)
        source = f"{stats['source']}#{channel_label}"
        for message in client.iter_channel_messages(team_id, cid,
                                                    limit=_remaining(limit, stats)):
            mid = str(message.get("id") or "")
            text = _message_body_text(message)
            if dry_run:
                stats["planned"] += 1
                out(f"[dry-run] would index {source} / message {mid or '?'} "
                    f"({len(text)} chars, {_sender_name(message)})")
                continue
            if archive is None:
                raise GraphError("internal error: ingestion needs an archive unless --dry-run")
            replies = None
            if include_replies and mid:
                replies = list(client.iter_message_replies(team_id, cid, mid))
            record = map_teams_message(message, source=source, replies=replies)
            if record is None:
                stats["skipped"] += 1
                continue
            archive.add(**record)
            stats["indexed"] += 1
    return stats


def _merge(target: dict, extra: dict) -> None:
    for key in ("planned", "indexed", "skipped", "failed"):
        target[key] += extra.get(key, 0)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _add_arguments(parser: argparse.ArgumentParser,
                   default_db: str = DEFAULT_DB) -> None:
    parser.add_argument("--db", default=default_db,
                        help="archive database path")
    parser.add_argument("--source", required=True,
                        choices=("sharepoint", "onedrive", "teams"),
                        help="which Microsoft 365 source to ingest")
    parser.add_argument("--site", default="root",
                        help="SharePoint site id (default: root site)")
    parser.add_argument("--site-name", dest="site_name",
                        help="override the label used as the archive source")
    parser.add_argument("--drive", help="drive id (--source onedrive)")
    parser.add_argument("--team", help="team id (--source teams)")
    parser.add_argument("--channel", help="channel id (default: all channels)")
    parser.add_argument("--limit", type=int,
                        help="max items to consider in this run")
    parser.add_argument("--no-replies", dest="include_replies",
                        action="store_false", default=True,
                        help="skip Teams message replies")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be indexed; no writes, no database")


def build_parser(prog: str = "archivepilot-ingest",
                 default_db: str = DEFAULT_DB) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Ingest Microsoft 365 content (SharePoint / OneDrive / "
                    "Teams) into ArchivePilot via Microsoft Graph. "
                    "Credentials come from MSGRAPH_TENANT_ID, "
                    "MSGRAPH_CLIENT_ID and MSGRAPH_CLIENT_SECRET.",
    )
    _add_arguments(parser, default_db=default_db)
    return parser


def add_parser(subparsers, default_db: str = DEFAULT_DB) -> argparse.ArgumentParser:
    """Attach the ``ingest`` sub-command to the main archivepilot CLI."""
    parser = subparsers.add_parser(
        "ingest", help="ingest Microsoft 365 content (Graph)")
    _add_arguments(parser, default_db=default_db)
    parser.set_defaults(fn=run)
    return parser


def run(args) -> int:
    """Execute a parsed namespace. Returns the process exit code."""
    if args.source == "onedrive" and not args.drive:
        print("error: --source onedrive requires --drive <drive-id>",
              file=sys.stderr)
        return MissingCredentials.exit_code
    if args.source == "teams" and not args.team:
        print("error: --source teams requires --team <team-id>", file=sys.stderr)
        return MissingCredentials.exit_code

    try:
        credentials = GraphCredentials.from_env()
    except MissingCredentials as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code

    client = GraphClient(credentials, timeout=DEFAULT_TIMEOUT)
    archive = None if args.dry_run else Archive(str(Path(args.db).expanduser()))
    try:
        if args.source == "sharepoint":
            stats = ingest_sharepoint(
                client, archive, site_id=args.site, site_name=args.site_name,
                dry_run=args.dry_run, limit=args.limit,
            )
        elif args.source == "onedrive":
            stats = ingest_onedrive(
                client, archive, drive_id=args.drive, dry_run=args.dry_run,
                limit=args.limit,
            )
        else:
            stats = ingest_teams(
                client, archive, team_id=args.team, channel_id=args.channel,
                include_replies=args.include_replies, dry_run=args.dry_run,
                limit=args.limit,
            )
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    finally:
        if archive is not None:
            archive.close()

    count = stats["planned"] if args.dry_run else stats["indexed"]
    verb = "would be indexed" if args.dry_run else "indexed"
    suffix = " [dry-run: nothing written]" if args.dry_run else ""
    print(f"{stats['source']}: {count} item(s) {verb}, "
          f"{stats['skipped']} skipped, {stats['failed']} failed{suffix}")
    return 0


def main(argv=None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
