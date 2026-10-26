"""SQLite persistence for runs (synchronous). Each run is stored as a JSON
document, with the fields used for lookups duplicated into columns.

The store is only used from the event loop thread, and `update` callbacks never
await, so a read-modify-write is never interleaved with other run changes.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ..timeutil import now
from .model import ACTIVE_STATES, Run


class RunStore:
    def __init__(self, path: str):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode; transactions are opened explicitly.
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA busy_timeout = 5000;
            CREATE TABLE IF NOT EXISTS runs (
              id           TEXT PRIMARY KEY,
              owner_iss    TEXT NOT NULL,
              owner_sub    TEXT NOT NULL,
              state        TEXT NOT NULL,
              created_at   TEXT NOT NULL,
              updated_at   TEXT NOT NULL,
              data         TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runs_owner ON runs (owner_iss, owner_sub, created_at);
            CREATE INDEX IF NOT EXISTS runs_state ON runs (state);
            """
        )

    def close(self) -> None:
        self._db.close()

    def insert(self, run: Run) -> None:
        self._db.execute(
            'INSERT INTO runs (id, owner_iss, owner_sub, state, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                run['id'],
                run['owner']['issuer'],
                run['owner']['subject'],
                run['execution']['state'],
                run['createdAt'],
                run['updatedAt'],
                json.dumps(run),
            ),
        )

    def get(self, id: str) -> Run | None:
        row = self._db.execute('SELECT data FROM runs WHERE id = ?', (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, owner: tuple[str, str] | None, limit: int, offset: int) -> tuple[list[Run], int]:
        """Returns a page of runs, newest first, and the total count. `owner` is (issuer, subject)."""
        where = 'WHERE owner_iss = ? AND owner_sub = ?' if owner else ''
        params = owner or ()
        rows = self._db.execute(
            f'SELECT data FROM runs {where} ORDER BY created_at DESC, id LIMIT ? OFFSET ?', (*params, limit, offset)
        ).fetchall()
        (total,) = self._db.execute(f'SELECT COUNT(*) FROM runs {where}', params).fetchone()
        return [json.loads(r[0]) for r in rows], total

    def list_active(self) -> list[Run]:
        states = sorted(ACTIVE_STATES)
        rows = self._db.execute(
            f'SELECT data FROM runs WHERE state IN ({",".join("?" * len(states))})', states
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def list_collecting(self) -> list[str]:
        """IDs of runs whose result listing is marked as in progress."""
        rows = self._db.execute(
            "SELECT id FROM runs WHERE json_extract(data, '$.resultsListing.collecting') = 1"
        ).fetchall()
        return [r[0] for r in rows]

    def update(self, id: str, fn: Callable[[Run], Run | None]) -> Run | None:
        """Atomically read-modify-write a run.

        `fn` receives a copy of the run inside a write transaction; it may return
        None to leave the run unchanged. Returns the stored run after the call,
        or None if it does not exist.
        """
        self._db.execute('BEGIN IMMEDIATE')
        try:
            current = self.get(id)
            updated = fn(copy.deepcopy(current)) if current else None
            if updated:
                updated['updatedAt'] = now()
                self._db.execute(
                    'UPDATE runs SET state = ?, updated_at = ?, data = ? WHERE id = ?',
                    (updated['execution']['state'], updated['updatedAt'], json.dumps(updated), id),
                )
            self._db.execute('COMMIT')
            return updated or current
        except BaseException:
            self._db.execute('ROLLBACK')
            raise

    def delete_if(self, id: str, predicate: Callable[[Run], bool]) -> Literal['deleted', 'not_found', 'rejected']:
        """Deletes the run if `predicate` holds for its current value."""
        self._db.execute('BEGIN IMMEDIATE')
        try:
            current = self.get(id)
            outcome: Literal['deleted', 'not_found', 'rejected'] = 'not_found'
            if current:
                outcome = 'deleted' if predicate(current) else 'rejected'
                if outcome == 'deleted':
                    self._db.execute('DELETE FROM runs WHERE id = ?', (id,))
            self._db.execute('COMMIT')
            return outcome
        except BaseException:
            self._db.execute('ROLLBACK')
            raise
