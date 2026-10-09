"""Lazy, isolated observation storage. Replay never creates or writes files."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

from ..errors import BackendError


class ObservationStore:
    def __init__(self, selection, *, replay=False):
        self.enabled = selection is not False and selection is not None
        self.replay = replay
        self.path = None
        if self.enabled and selection is not True:
            path = Path(selection).expanduser()
            if path.suffix == ".jsonl":
                path = path.with_name(path.stem + ".semantic-v1.jsonl")
            self.path = str(path)
        self._memory: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        if not self.enabled:
            return None
        with self._lock:
            if key in self._memory:
                self._memory.move_to_end(key)
                return self._memory[key]
            if self.path is None or not Path(self.path).exists():
                return None
            try:
                if self.path.endswith(".jsonl"):
                    value = None
                    with Path(self.path).open(encoding="utf-8") as stream:
                        for line in stream:
                            if line.strip():
                                row = json.loads(line)
                                if row["key"] == key:
                                    value = row["value"]
                else:
                    with closing(sqlite3.connect(Path(self.path).absolute().as_uri() + "?mode=ro", uri=True)) as db:
                        if not db.execute("SELECT name FROM sqlite_master WHERE name='semantic_observations_v1'").fetchone():
                            return None
                        row = db.execute("SELECT value FROM semantic_observations_v1 WHERE key=?", (key,)).fetchone()
                        value = None if row is None else json.loads(row[0])
            except (ValueError, KeyError, TypeError, sqlite3.Error, OSError):
                raise BackendError("semantic observation store is malformed or unreadable") from None
            if value is not None:
                self._remember(key, value)
            return value

    def _remember(self, key, value):
        self._memory[key] = value
        self._memory.move_to_end(key)
        while len(self._memory) > 20_000:
            self._memory.popitem(last=False)

    def put(self, key, value):
        try:
            self._put(key, value)
        except (ValueError, TypeError, sqlite3.Error, OSError):
            raise BackendError("semantic observation could not be recorded") from None

    def _put(self, key, value):
        if not self.enabled or self.replay:
            return
        with self._lock:
            if self.path is not None:
                path = Path(self.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.endswith(".jsonl"):
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({"key": key, "value": value}, ensure_ascii=False, allow_nan=False) + "\n")
                else:
                    with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                        db.execute("CREATE TABLE IF NOT EXISTS semantic_observations_v1 (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                        db.execute("INSERT OR REPLACE INTO semantic_observations_v1 VALUES (?, ?)", (key, json.dumps(value)))
            self._remember(key, value)
