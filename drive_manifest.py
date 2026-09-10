#!/usr/bin/env python3
"""Inventory an external drive (or any folder) into a CSV manifest.

Dependency-free on purpose: standard library only, Python 3.9+, so it runs on a
lab Mac with no setup beyond a Python install. Read-only — it never modifies the
drive.

Per file it records: path (relative to the root), top-level folder, extension,
size in bytes, modified time, whether the file's header matches its extension
(catches the 131 KB stub cluster: a "PDF" whose first bytes aren't ``%PDF`` is
not a PDF), an MD5 for files under the size cap (Zotero stores MD5 per
attachment, so this lets us match drive files to library attachments), and the
macOS Finder tags on the file (Sha marked triaged folders green / problem
folders red).

Usage:

    python3 drive_manifest.py "/Volumes/MS TRANCHE" --out tranche_manifest.csv

    # faster first pass, no hashing:
    python3 drive_manifest.py "/Volumes/MS TRANCHE" --out tranche_manifest.csv --no-hash

Progress prints to stderr every 500 files. Re-running overwrites the output.
"""



from __future__ import annotations



import argparse
import csv
import hashlib
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Magic bytes for the document formats we care about. Anything else is "n/a".
MAGIC = {
    ".pdf": [b"%PDF"],
    ".epub": [b"PK\x03\x04"],
    ".docx": [b"PK\x03\x04"],
    ".zip": [b"PK\x03\x04"],
    ".djvu": [b"AT&TFORM"],
    ".png": [b"\x89PNG"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".gif": [b"GIF8"],
    ".mp4": [b"\x00\x00\x00", b"ftyp"],  # loose: ftyp box near start
    ".mov": [b"\x00\x00\x00", b"ftyp"],
}

SKIP_NAMES = {".DS_Store", ".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems"}
DEFAULT_HASH_CAP = 200 * 1024 * 1024  # 200 MB; skip hashing videos etc.


def header_check(path: Path, ext: str) -> str:
    """Return 'ok', 'MISMATCH', 'empty', 'unreadable', or 'n/a'."""
    patterns = MAGIC.get(ext)
    if not patterns:
        return "n/a"
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return "unreadable"
    if not head:
        return "empty"
    return "ok" if any(p in head for p in patterns) else "MISMATCH"


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def finder_tags(path: Path) -> str:
    """macOS Finder tags (colour labels) via the xattr CLI; '' elsewhere or if none."""
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run(
            ["xattr", "-px", "com.apple.metadata:_kMDItemUserTags", str(path)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        return ""
    try:
        raw = bytes.fromhex(out.stdout.replace(" ", "").replace("\n", ""))
        tags = plistlib.loads(raw)
        # Entries look like "Green\n2" (name, newline, colour index).
        return ";".join(str(t).split("\n")[0] for t in tags)
    except Exception:  # noqa: BLE001 — best-effort metadata, never fail the walk
        return ""


def walk(root: Path, do_hash: bool, hash_cap: int, writer: csv.DictWriter) -> dict:
    counts = {"files": 0, "dirs": 0, "mismatch": 0, "errors": 0, "bytes": 0}
    started = time.time()
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: counts.__setitem__("errors", counts["errors"] + 1)):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_NAMES and not d.startswith("._"))
        counts["dirs"] += 1
        dpath = Path(dirpath)
        dir_tags = finder_tags(dpath)
        for name in sorted(filenames):
            if name in SKIP_NAMES or name.startswith("._"):
                continue
            fpath = dpath / name
            try:
                st = fpath.stat()
            except OSError:
                counts["errors"] += 1
                continue
            rel = fpath.relative_to(root)
            ext = fpath.suffix.lower()
            hdr = header_check(fpath, ext)
            if hdr == "MISMATCH":
                counts["mismatch"] += 1
            digest = ""
            if do_hash and st.st_size <= hash_cap:
                digest = md5_of(fpath)
            writer.writerow({
                "path": str(rel),
                "top_folder": rel.parts[0] if len(rel.parts) > 1 else "",
                "filename": name,
                "ext": ext,
                "size_bytes": st.st_size,
                "size_kb": round(st.st_size / 1024),
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d"),
                "header_check": hdr,
                "md5": digest,
                "finder_tags_file": finder_tags(fpath),
                "finder_tags_folder": dir_tags,
            })
            counts["files"] += 1
            counts["bytes"] += st.st_size
            if counts["files"] % 500 == 0:
                print(f"  {counts['files']} files, {time.time() - started:.0f}s elapsed", file=sys.stderr)
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help='drive or folder to inventory, e.g. "/Volumes/MS TRANCHE"')
    ap.add_argument("--out", default="drive_manifest.csv", help="output CSV path")
    ap.add_argument("--no-hash", action="store_true", help="skip MD5 hashing (much faster)")
    ap.add_argument("--hash-cap-mb", type=int, default=DEFAULT_HASH_CAP // (1024 * 1024),
                    help="don't hash files larger than this (default 200)")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"not a folder: {root}")

    fields = ["path", "top_folder", "filename", "ext", "size_bytes", "size_kb", "modified",
              "header_check", "md5", "finder_tags_file", "finder_tags_folder"]
    print(f"Inventorying {root} → {args.out}", file=sys.stderr)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        counts = walk(root, not args.no_hash, args.hash_cap_mb * 1024 * 1024, writer)

    gb = counts["bytes"] / 1e9
    print(
        f"Done: {counts['files']} files in {counts['dirs']} folders, {gb:.1f} GB; "
        f"{counts['mismatch']} header mismatches (likely corrupt/stub files); "
        f"{counts['errors']} unreadable entries.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()