"""Durable evidence and workflow ledger with bounded queries and retention."""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, path):
        # Evidence storage budget; lowered only to exercise storage pressure in tests.
        self.live_limit = int(os.getenv('AEGIS_STORAGE_LIMIT_MIB', '256')) * 1024**2
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=3000;
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, session_id TEXT, at REAL, node TEXT, kind TEXT, body TEXT);
        CREATE INDEX IF NOT EXISTS evidence_time ON evidence(at);
        CREATE INDEX IF NOT EXISTS evidence_session_time ON evidence(session_id, at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS actions(key TEXT PRIMARY KEY, session_id TEXT, at REAL, request TEXT, body TEXT);
        CREATE INDEX IF NOT EXISTS action_session ON actions(session_id, at);
        ''')
        self.db.commit()

    @staticmethod
    def decode_evidence(row):
        return {**dict(row), 'body': json.loads(row['body'])}

    def save(self, session):
        with self.db:
            self.db.execute('INSERT INTO sessions VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                            (session['id'], json.dumps(session)))

    def session(self, session_id):
        row = self.db.execute('SELECT body FROM sessions WHERE id=?', (session_id,)).fetchone()
        return json.loads(row['body']) if row else None

    def sessions(self):
        return [json.loads(r['body']) for r in self.db.execute(
            "SELECT body FROM sessions ORDER BY json_extract(body, '$.started_at') DESC LIMIT 100")]

    def active_session(self):
        row = self.db.execute("SELECT body FROM sessions WHERE json_extract(body, '$.status') != 'ended' ORDER BY json_extract(body, '$.started_at') DESC LIMIT 1").fetchone()
        return json.loads(row['body']) if row else None

    def evidence(self, session_id=None, limit=100):
        if session_id is None:
            rows = self.db.execute('SELECT * FROM evidence ORDER BY at DESC, id DESC LIMIT ?', (limit,))
        else:
            rows = self.db.execute('SELECT * FROM evidence WHERE session_id=? ORDER BY at DESC, id DESC LIMIT ?', (session_id, limit))
        return [self.decode_evidence(r) for r in rows]

    def evidence_by_ids(self, session_id, ids):
        if not ids:
            return []
        placeholders = ','.join('?' for _ in ids)
        return [self.decode_evidence(r) for r in self.db.execute(
            f'SELECT * FROM evidence WHERE session_id=? AND id IN ({placeholders}) ORDER BY at DESC, id DESC', (session_id, *ids))]

    def baseline_evidence(self, limit=60):
        return [self.decode_evidence(row) for row in self.db.execute(
            'SELECT * FROM evidence WHERE session_id IS NULL ORDER BY at DESC,id DESC LIMIT ?', (limit,))]

    def evidence_page(self, session_id, limit=50, before=None, node=None, kind=None):
        clauses, values = ['session_id=?'], [session_id]
        if before:
            anchor = self.db.execute('SELECT at,id FROM evidence WHERE session_id=? AND id=?', (session_id, before)).fetchone()
            if not anchor:
                raise ValueError('Evidence cursor expired or does not belong to this investigation; reload the first page')
            clauses.append('(at < ? OR (at = ? AND id < ?))')
            values.extend([anchor['at'], anchor['at'], anchor['id']])
        if node:
            clauses.append('node=?')
            values.append(node)
        if kind:
            clauses.append('kind=?')
            values.append(kind)
        rows = self.db.execute('SELECT * FROM evidence WHERE ' + ' AND '.join(clauses) + ' ORDER BY at DESC,id DESC LIMIT ?', (*values, limit + 1)).fetchall()
        return {'items': [self.decode_evidence(r) for r in rows[:limit]],
                'next_cursor': rows[limit-1]['id'] if len(rows) > limit else None}

    def operator_checks(self, session_id):
        return [{'at': r['at'], 'kind': r['kind'], 'node': r['node']} for r in self.db.execute(
            "SELECT at, kind, node FROM evidence WHERE session_id=? AND json_extract(body, '$.origin')='operator' ORDER BY at", (session_id,))]

    def evidence_series(self, session_id, kinds, limit):
        """Oldest-first observations for charts; the newest are kept when over the limit."""
        placeholders = ','.join('?' for _ in kinds)
        rows = self.db.execute(f'SELECT * FROM evidence WHERE session_id=? AND kind IN ({placeholders}) ORDER BY at DESC LIMIT ?', (session_id, *kinds, limit))
        return [self.decode_evidence(r) for r in reversed(rows.fetchall())]

    def evidence_summary(self, session_id):
        row = self.db.execute('SELECT COUNT(*) AS count, MIN(at) AS oldest, MAX(at) AS newest FROM evidence WHERE session_id=?', (session_id,)).fetchone()
        return dict(row)

    def record(self, session_id, node, kind, body):
        encoded = json.dumps(body, ensure_ascii=True)
        if len(encoded.encode()) > 32768:
            # Re-encoding an escaped prefix can expand it again. Truncate actual text instead.
            excerpt = encoded[:4096]
            body = {'ok': body.get('ok', False), 'truncated': True, 'original_bytes': len(encoded.encode()), 'excerpt': excerpt}
        item = {'id': uuid.uuid4().hex, 'session_id': session_id, 'at': time.time(), 'node': node, 'kind': kind, 'body': body}
        with self.db:
            self.db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?)', (*[item[k] for k in ('id', 'session_id', 'at', 'node', 'kind')], json.dumps(body)))
        return item

    def action(self, key):
        row = self.db.execute('SELECT * FROM actions WHERE key=?', (key,)).fetchone()
        return {**dict(row), 'body': json.loads(row['body'])} if row else None

    def actions(self, session_id):
        return [{**dict(row), 'body': json.loads(row['body'])} for row in self.db.execute('SELECT * FROM actions WHERE session_id=? ORDER BY at', (session_id,))]

    def save_action(self, key, session_id, request, body):
        with self.db:
            self.db.execute('INSERT INTO actions VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body',
                            (key, session_id, time.time(), request, json.dumps(body)))

    def reconcile(self):
        rows = self.db.execute("SELECT * FROM actions WHERE json_extract(body, '$.status')='running'").fetchall()
        for row in rows:
            body = json.loads(row['body'])
            body.update(status='unknown', detail='Controller restarted during action. Inspect actual state before retrying.')
            self.save_action(row['key'], row['session_id'], row['request'], body)

    def storage(self):
        page = self.db.execute('PRAGMA page_size').fetchone()[0]
        count = self.db.execute('PRAGMA page_count').fetchone()[0]
        free = self.db.execute('PRAGMA freelist_count').fetchone()[0]
        wal = Path(str(self.path) + '-wal')
        return {'live_bytes': (count-free)*page, 'allocated_bytes': count*page,
                'wal_bytes': wal.stat().st_size if wal.exists() else 0,
                'live_limit_bytes': self.live_limit, 'wal_limit_bytes': 128*1024**2}

    def prune(self, now=None, batch_size=1000):
        now = time.time() if now is None else now
        with self.db:
            removed = self.db.execute("""DELETE FROM evidence WHERE id IN (
                SELECT id FROM evidence WHERE at < ? AND (session_id IS NULL OR session_id NOT IN
                (SELECT id FROM sessions WHERE json_extract(body, '$.status') != 'ended')) LIMIT ?)""",
                (now-86400, batch_size)).rowcount
            # Keep actions as long as their investigation summary, including long-running sessions.
            expired = self.db.execute("""SELECT id FROM sessions
                WHERE json_extract(body,'$.status')='ended'
                AND COALESCE(json_extract(body,'$.ended_at'), json_extract(body,'$.verified_at'),
                             json_extract(body,'$.started_at')) < ? LIMIT ?""",
                (now-30*86400, batch_size)).fetchall()
            for row in expired:
                self.db.execute('DELETE FROM actions WHERE session_id=?', (row['id'],))
                self.db.execute('DELETE FROM sessions WHERE id=?', (row['id'],))
            orphaned = self.db.execute("""DELETE FROM actions WHERE key IN (
                SELECT key FROM actions WHERE at < ? AND (session_id IS NULL OR session_id NOT IN
                (SELECT id FROM sessions)) LIMIT ?)""", (now-30*86400, batch_size)).rowcount
        # TRUNCATE releases the physical WAL allocation that the storage guard measures.
        # A reader may pin frames; never stall the collector waiting for that reader.
        timeout = self.db.execute('PRAGMA busy_timeout').fetchone()[0]
        try:
            self.db.execute('PRAGMA busy_timeout=0')
            checkpoint = self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        finally:
            self.db.execute(f'PRAGMA busy_timeout={timeout}')
        return {'evidence_removed': removed, 'summaries_removed': len(expired),
                'batch_full': max(removed, len(expired), orphaned) == batch_size,
                'checkpoint_busy': bool(checkpoint[0]), 'storage': self.storage()}
