from __future__ import annotations
import argparse
import sqlite3
from pathlib import Path
from .db import DB_URL

def main():
    parser=argparse.ArgumentParser(description="Create a consistent SQLite backup without overwriting files")
    parser.add_argument("destination",type=Path)
    args=parser.parse_args()
    if not DB_URL.startswith("sqlite:///") or DB_URL.endswith(":memory:"):raise SystemExit("Only file-backed SQLite is supported")
    source=Path(DB_URL.removeprefix("sqlite:///"))
    target=args.destination.resolve()
    if target.exists():raise SystemExit("Destination already exists")
    if not source.is_file():raise SystemExit("Source database not found")
    target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro",uri=True) as src, sqlite3.connect(str(target)) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0]!="ok":raise SystemExit("Backup integrity check failed")
    print("Backup created and integrity checked")
if __name__=="__main__":main()
