"""Short-lived process entry point for Zoty index refreshes."""

from __future__ import annotations

import argparse
from pathlib import Path

from zoty import db


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and publish a Zoty search snapshot.")
    parser.add_argument("--zotero-db")
    parser.add_argument("--zotero-storage")
    parser.add_argument("--sidecar-root")
    parser.add_argument("--prepare-snapshot")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.prepare_snapshot:
        if args.zotero_db or args.zotero_storage or args.sidecar_root:
            raise SystemExit("--prepare-snapshot cannot be combined with refresh paths")
        return db.run_snapshot_prepare_worker(Path(args.prepare_snapshot))
    if not args.zotero_db or not args.zotero_storage or not args.sidecar_root:
        raise SystemExit(
            "--zotero-db, --zotero-storage, and --sidecar-root are required for refreshes"
        )
    db._ZOTERO_DB = Path(args.zotero_db)
    db._ZOTERO_STORAGE = Path(args.zotero_storage)
    db._SIDECAR_ROOT = Path(args.sidecar_root)
    return db.run_index_refresh_worker()


if __name__ == "__main__":
    raise SystemExit(main())
