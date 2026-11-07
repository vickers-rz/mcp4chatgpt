#!/usr/bin/env python3
"""Dry-run or prune old terminal strict-file image blobs; keep ledger evidence."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from mcp4chatgpt.operations.maintenance import prune_images
from mcp4chatgpt.operations.store import OperationStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="MCP_DATA_DIR/operations")
    parser.add_argument("--older-than-days", type=int, default=90)
    parser.add_argument("--apply", action="store_true", help="Actually remove old image blobs")
    parser.add_argument("--warn-bytes", type=int, default=1024 * 1024 * 1024,
                        help="Warn when retained before/after images exceed this many bytes")
    args = parser.parse_args()
    store = OperationStore(args.state_dir / "operations.db")
    for key, _row in store.file_operations():
        print(json.dumps(prune_images(store, args.state_dir, key,
                                     older_than_days=args.older_than_days, apply=args.apply),
                         ensure_ascii=False))
    images_root = args.state_dir / "operation-files"
    retained = sum(path.stat().st_size for directory in images_root.iterdir()
                   if directory.is_dir() for name in ("before", "after")
                   if (path := directory / name).is_file()) if images_root.is_dir() else 0
    free = shutil.disk_usage(args.state_dir).free
    print(json.dumps({"retained_image_bytes": retained, "disk_free_bytes": free,
                      "capacity_warning": retained >= args.warn_bytes or free < 2 * 16 * 1024 * 1024}))


if __name__ == "__main__":
    main()
