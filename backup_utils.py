"""Consistent SQLite backups without importing the app or applying migrations."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from time_utils import japan_now


def backup_database(source, output=None):
    source = Path(source).resolve()
    if not source.is_file():
        raise RuntimeError("元のデータベースが見つかりません。保存先を確認してください。")
    destination = (Path(output).expanduser() if output else source.parent / "backups" /
                   f"training-{japan_now():%Y%m%d-%H%M%S-%f}.db").resolve()
    if destination == source:
        raise RuntimeError("バックアップ先に元のデータベースを指定することはできません。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)) as original:
            with closing(sqlite3.connect(destination, timeout=15)) as copy:
                original.backup(copy)
                if copy.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("バックアップの整合性を確認できませんでした。")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination
