"""Command-line interface for ArchivePilot."""

import argparse
import sys
from pathlib import Path

from . import __version__
from .ask import ask
from .db import Archive
from .importers import (import_csv, import_stdin, import_takeout,
                        import_text, import_whatsapp)

DEFAULT_DB = "~/.archivepilot/archive.db"


def _archive(args) -> Archive:
    return Archive(Path(args.db).expanduser())


def cmd_import(args) -> int:
    archive = _archive(args)
    total = 0
    if args.whatsapp:
        for p in args.whatsapp:
            total += import_whatsapp(Path(p).expanduser(), archive)
    if args.takeout:
        for p in args.takeout:
            total += import_takeout(Path(p).expanduser(), archive)
    if args.csv:
        for p in args.csv:
            total += import_csv(Path(p).expanduser(), archive)
    if args.text:
        for p in args.text:
            total += import_text(Path(p).expanduser(), archive)
    if args.stdin:
        total += import_stdin(archive)
    print(f"imported {total} items into {archive.path}")
    return 0


def cmd_search(args) -> int:
    archive = _archive(args)
    hits = archive.search(args.query, limit=args.limit)
    if not hits:
        print("no matches")
        return 1
    for h in hits:
        src = h["source"]
        date = (h["created_at"] or "")[:16]
        print(f"\n[{src} | {date}]\n{h['raw'][:400]}")
    print(f"\n--- {len(hits)} matches ---")
    return 0


def cmd_ask(args) -> int:
    archive = _archive(args)
    print(ask(archive, args.question, limit=args.limit))
    return 0


def cmd_stats(args) -> int:
    archive = _archive(args)
    s = archive.stats()
    print(f"archive: {archive.path}")
    print(f"items:   {s['items']}")
    for source, count in sorted(s["sources"].items()):
        print(f"  {source}: {count}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="archivepilot",
        description="Your data, one private AI archive (EN + AR).",
    )
    p.add_argument("--db", default=DEFAULT_DB, help="archive database path")
    p.add_argument("--version", action="version",
                   version=f"archivepilot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="import data into the archive")
    imp.add_argument("--whatsapp", nargs="+", help="WhatsApp .txt export(s)")
    imp.add_argument("--takeout", nargs="+", help="Google Takeout folder(s)")
    imp.add_argument("--csv", nargs="+", help="CSV/TSV file(s)")
    imp.add_argument("--text", nargs="+", help="plain text/markdown file(s)")
    imp.add_argument("--stdin", action="store_true", help="read from stdin")
    imp.set_defaults(fn=cmd_import)

    se = sub.add_parser("search", help="full-text search (EN + AR)")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=20)
    se.set_defaults(fn=cmd_search)

    a = sub.add_parser("ask", help="AI answer over your archive")
    a.add_argument("question")
    a.add_argument("--limit", type=int, default=8)
    a.set_defaults(fn=cmd_ask)

    st = sub.add_parser("stats", help="archive statistics")
    st.set_defaults(fn=cmd_stats)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
