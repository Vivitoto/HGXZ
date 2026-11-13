from __future__ import annotations

from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import threading


class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        # Multiple threads share this connection (scheduled sync, manual sync,
        # request handlers); wait for a busy writer instead of failing.
        self.conn.execute('PRAGMA busy_timeout=10000')
        self._lock = threading.RLock()
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS albums (
          id INTEGER PRIMARY KEY,
          category TEXT NOT NULL,
          title TEXT NOT NULL,
          episode_count INTEGER NOT NULL DEFAULT 0,
          ended INTEGER NOT NULL DEFAULT 0,
          directory TEXT NOT NULL DEFAULT '',
          last_checked_at TEXT,
          last_changed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS episodes (
          album_id INTEGER NOT NULL,
          episode INTEGER NOT NULL,
          path TEXT NOT NULL,
          status TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          size_bytes INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(album_id, episode)
        );
        ''')
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        cols = {row[1] for row in self.conn.execute('PRAGMA table_info(albums)')}
        if 'directory' not in cols:
            self.conn.execute("ALTER TABLE albums ADD COLUMN directory TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        # subscribed: 1 = 订阅中（定时任务自动追更下载），0 = 仅发现（手动挑着下）
        if 'subscribed' not in cols:
            self.conn.execute(
                "ALTER TABLE albums ADD COLUMN subscribed INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        ep_cols = {row[1] for row in self.conn.execute('PRAGMA table_info(episodes)')}
        if 'size_bytes' not in ep_cols:
            self.conn.execute('ALTER TABLE episodes ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0')
            self.conn.commit()

    def set_subscribed(self, album_id: int, subscribed: bool) -> None:
        with self._lock:
            self.conn.execute(
                'UPDATE albums SET subscribed=? WHERE id=?',
                (1 if subscribed else 0, int(album_id)))
            self.conn.commit()

    def subscribed_albums(self) -> list[dict]:
        """所有处于订阅状态的专辑，供定时任务决定要追更哪些。"""
        with self._lock:
            rows = self.conn.execute(
                'SELECT * FROM albums WHERE subscribed=1 ORDER BY id').fetchall()
        return [dict(row) for row in rows]

    def is_subscribed(self, album_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                'SELECT subscribed FROM albums WHERE id=?', (int(album_id),)).fetchone()
        return bool(row and row['subscribed'])

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_album(self, album_id: int, category: str, title: str,
                     episode_count: int, ended: bool, directory: str = '') -> None:
        now = self._now()
        with self._lock:
            self.conn.execute('''
              INSERT INTO albums(id,category,title,episode_count,ended,directory,last_checked_at,last_changed_at)
              VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET category=excluded.category,title=excluded.title,
                episode_count=excluded.episode_count,ended=excluded.ended,
                directory=CASE WHEN excluded.directory != '' THEN excluded.directory ELSE albums.directory END,
                last_checked_at=excluded.last_checked_at,
                last_changed_at=CASE WHEN albums.episode_count != excluded.episode_count OR albums.ended != excluded.ended
                  THEN excluded.last_changed_at ELSE albums.last_changed_at END
            ''', (album_id, category, title, episode_count, int(ended), directory, now, now))
            self.conn.commit()

    def mark_episode(self, album_id: int, episode: int, path: str, status: str,
                     size_bytes: int = 0) -> None:
        with self._lock:
            self.conn.execute('''
              INSERT INTO episodes(album_id,episode,path,status,updated_at,size_bytes) VALUES(?,?,?,?,?,?)
              ON CONFLICT(album_id,episode) DO UPDATE SET path=excluded.path,status=excluded.status,
                updated_at=excluded.updated_at,size_bytes=excluded.size_bytes
            ''', (album_id, episode, path, status, self._now(), max(0, int(size_bytes))))
            self.conn.commit()

    def episode_done(self, album_id: int, episode: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                'SELECT status,path FROM episodes WHERE album_id=? AND episode=?',
                (album_id, episode)).fetchone()
            return bool(row and row['status'] == 'done' and Path(row['path']).exists())

    def episode_row(self, album_id: int, episode: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                'SELECT * FROM episodes WHERE album_id=? AND episode=?',
                (album_id, episode)).fetchone()

    def episode_failed_at(self, album_id: int, episode: int) -> str | None:
        with self._lock:
            row = self.conn.execute(
                'SELECT status,updated_at FROM episodes WHERE album_id=? AND episode=?',
                (album_id, episode)).fetchone()
            return row['updated_at'] if row and row['status'] == 'failed' else None

    def album_row(self, album_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute('SELECT * FROM albums WHERE id=?', (album_id,)).fetchone()

    def album_is_complete(self, album_id: int, episode_count: int) -> bool:
        with self._lock:
            rows = self.conn.execute(
                'SELECT episode,path,status FROM episodes WHERE album_id=?',
                (album_id,)).fetchall()
            done = {row['episode']: row['path'] for row in rows if row['status'] == 'done'}
            if episode_count < 1 or len(done) < episode_count:
                return False
            return all(number in done and Path(done[number]).exists()
                       for number in range(1, episode_count + 1))

    def unfinished_albums(self) -> list[dict]:
        with self._lock:
            return [dict(x) for x in self.conn.execute(
                'SELECT * FROM albums WHERE ended=0 ORDER BY id')]

    # ---- WebUI query methods ----

    def all_albums(self, category: str | None = None,
                   status: str | None = None,
                   search: str | None = None) -> list[dict]:
        query = '''
          SELECT a.*,
            COALESCE(e.done_count, 0) AS done_count,
            COALESCE(e.failed_count, 0) AS failed_count
          FROM albums a
          LEFT JOIN (
            SELECT album_id,
              SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_count,
              SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count
            FROM episodes GROUP BY album_id
          ) e ON a.id = e.album_id
          WHERE 1=1
        '''
        params: list = []
        if category:
            query += ' AND a.category = ?'
            params.append(category)
        if search:
            query += ' AND a.title LIKE ?'
            params.append(f'%{search}%')
        if status == 'done':
            query += (' AND a.ended = 1 AND COALESCE(e.done_count, 0) >= a.episode_count'
                      ' AND a.episode_count > 0')
        elif status == 'failed':
            query += ' AND COALESCE(e.failed_count, 0) > 0'
        elif status == 'subscribed':
            query += ' AND a.subscribed = 1'
        elif status == 'new':
            # 已发现但一集都没下过，用于「挑着下」
            query += (' AND COALESCE(e.done_count, 0) = 0'
                      ' AND a.subscribed = 0')
        elif status == 'downloading':
            query += ' AND (a.ended = 0 OR COALESCE(e.done_count, 0) < a.episode_count)'
        query += ' ORDER BY a.last_checked_at DESC'
        with self._lock:
            return [dict(row) for row in self.conn.execute(query, params)]

    def album_with_episodes(self, album_id: int) -> dict | None:
        with self._lock:
            album = self.album_row(album_id)
            if not album:
                return None
            result = dict(album)
            episodes = self.conn.execute(
                'SELECT episode, path, status, updated_at, size_bytes '
                'FROM episodes WHERE album_id=? ORDER BY episode',
                (album_id,)).fetchall()
            result['episodes'] = [dict(e) for e in episodes]
            done = sum(1 for e in episodes if e['status'] == 'done')
            failed = sum(1 for e in episodes if e['status'] == 'failed')
            result['done_count'] = done
            result['failed_count'] = failed
            return result

    def dashboard_stats(self) -> dict:
        with self._lock:
            albums = self.conn.execute('SELECT COUNT(*) AS c FROM albums').fetchone()['c']
            total = self.conn.execute(
                'SELECT COALESCE(SUM(episode_count), 0) AS c FROM albums').fetchone()['c']
            done = self.conn.execute(
                "SELECT COUNT(*) AS c FROM episodes WHERE status='done'").fetchone()['c']
            failed = self.conn.execute(
                "SELECT COUNT(*) AS c FROM episodes WHERE status='failed'").fetchone()['c']
            ended = self.conn.execute(
                'SELECT COUNT(*) AS c FROM albums WHERE ended=1').fetchone()['c']
            complete = self.conn.execute('''
                SELECT COUNT(*) AS c FROM albums a
                JOIN (SELECT album_id, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS d
                      FROM episodes GROUP BY album_id) e ON e.album_id = a.id
                WHERE a.episode_count > 0 AND e.d >= a.episode_count
            ''').fetchone()['c']
            bytes_total = self.conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS c FROM episodes WHERE status='done'"
            ).fetchone()['c']
            categories = [row['category'] for row in self.conn.execute(
                'SELECT DISTINCT category FROM albums ORDER BY category')]
            return {
                'albums': albums,
                'total_episodes': total,
                'done_episodes': done,
                'failed_episodes': failed,
                'ended_albums': ended,
                'complete_albums': complete,
                'downloaded_bytes': bytes_total,
                'categories': categories,
            }

    def episode_stats(self, album_id: int) -> dict:
        """Per-album counters plus the most recent episode activity."""
        with self._lock:
            row = self.conn.execute('''
                SELECT
                  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                  MAX(CASE WHEN status='done' THEN updated_at END) AS last_done_at,
                  COALESCE(SUM(CASE WHEN status='done' THEN size_bytes ELSE 0 END), 0) AS bytes
                FROM episodes WHERE album_id=?
            ''', (album_id,)).fetchone()
            return {
                'done': row['done'] or 0,
                'failed': row['failed'] or 0,
                'last_done_at': row['last_done_at'],
                'downloaded_bytes': row['bytes'] or 0,
            }

    def reset_failed(self, album_id: int | None = None) -> int:
        with self._lock:
            if album_id is not None:
                cur = self.conn.execute(
                    "DELETE FROM episodes WHERE album_id=? AND status='failed'",
                    (album_id,))
            else:
                cur = self.conn.execute(
                    "DELETE FROM episodes WHERE status='failed'")
            self.conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self.conn.close()
