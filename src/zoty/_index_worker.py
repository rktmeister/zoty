"""Short-lived process entry point for Zoty index refreshes."""

from __future__ import annotations

import argparse
from pathlib import Path

from zoty import db


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Zoty's SQLite FTS index.")
    parser.add_argument("--zotero-db", required=True)
    parser.add_argument("--zotero-storage", required=True)
    parser.add_argument("--sidecar-root", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db._ZOTERO_DB = Path(args.zotero_db)
    db._ZOTERO_STORAGE = Path(args.zotero_storage)
    db._SIDECAR_ROOT = Path(args.sidecar_root)
    return db.run_index_refresh_worker()


if __name__ == "__main__":
    raise SystemExit(main())
