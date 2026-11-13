from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..state import StateDB
from .log_handler import LogBuffer

LOG = logging.getLogger('HGXZ')

STATIC_DIR = Path(__file__).parent / 'static'

_state_db: StateDB | None = None
_log_buffer: LogBuffer | None = None
_archiver_ref: list = []
_sync_status: dict = {'running': False, 'last_sync': None, 'last_error': None}
# Shared by the scheduled loop (cli.run_daemon) and every WebUI trigger so two
# syncs can never run at once -- they would download the same episode into the
# same ".part" file.
_sync_lock = Lock()


def try_acquire_sync() -> bool:
    """Claim the single sync slot. False means one is already running."""
    if not _sync_lock.acquire(blocking=False):
        return False
    _sync_status['running'] = True
    _sync_status['last_error'] = None
    return True


def release_sync() -> None:
    _sync_status['running'] = False
    try:
        _sync_lock.release()
    except RuntimeError:
        pass


def mark_sync_finished() -> None:
    from datetime import datetime
    _sync_status['last_sync'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def set_shared_state(state: StateDB, log_buf: LogBuffer, archiver_holder: list) -> None:
    global _state_db, _log_buffer, _archiver_ref
    _state_db = state
    _log_buffer = log_buf
    _archiver_ref = archiver_holder


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    app = FastAPI(title='HGXZ - 黄果下载', lifespan=lifespan)

    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')

    @app.get('/', response_class=HTMLResponse)
    async def index():
        return FileResponse(STATIC_DIR / 'index.html')

    @app.get('/api/dashboard')
    async def dashboard():
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        stats = _state_db.dashboard_stats()
        stats['sync'] = _sync_status
        return stats

    @app.get('/api/albums')
    async def albums(
        category: str | None = Query(None),
        status: str | None = Query(None),
        search: str | None = Query(None),
    ):
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        return _state_db.all_albums(category=category, status=status, search=search)

    @app.get('/api/albums/{album_id}')
    async def album_detail(album_id: int):
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        result = _state_db.album_with_episodes(album_id)
        if not result:
            return JSONResponse({'error': 'not found'}, 404)
        result.update(_state_db.episode_stats(album_id))
        directory = result.get('directory') or ''
        result['poster_available'] = bool(directory) and (Path(directory) / 'poster.jpg').is_file()
        return result

    @app.get('/api/albums/{album_id}/poster')
    async def album_poster(album_id: int):
        """Serve the scraped poster so the library list can show real covers."""
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        row = _state_db.album_row(album_id)
        if not row or not row['directory']:
            return JSONResponse({'error': 'not found'}, 404)
        poster = Path(row['directory']) / 'poster.jpg'
        try:
            if not poster.is_file() or poster.stat().st_size == 0:
                return JSONResponse({'error': 'not found'}, 404)
        except OSError:
            return JSONResponse({'error': 'not found'}, 404)
        return FileResponse(poster, media_type='image/jpeg',
                            headers={'Cache-Control': 'max-age=3600'})

    def _start_background(name: str, work):
        """Run a blocking job once, refusing to start while another is active."""
        if not try_acquire_sync():
            return False

        def _run():
            try:
                work()
                mark_sync_finished()
            except Exception as exc:
                _sync_status['last_error'] = str(exc)
                LOG.exception('%s failed: %s', name, exc)
            finally:
                release_sync()

        import threading
        threading.Thread(target=_run, daemon=True, name=name).start()
        return True

    @app.post('/api/sync')
    async def trigger_sync(album_id: int | None = None,
                           all_subscribed: bool = False):
        """手动同步。

        album_id 指定时只下载那一部（挑着下）；
        all_subscribed=True 时只下载订阅中的专辑；
        都不给则全量同步（发现 + 下载全部，原行为）。
        """
        if not _archiver_ref:
            return JSONResponse({'error': 'not ready'}, 503)
        archiver = _archiver_ref[0]
        if not _start_background(
                'manual-sync',
                lambda: archiver.scan(only_id=album_id,
                                      subscribed_only=all_subscribed)):
            return JSONResponse({'error': '同步正在进行中'}, 409)
        return {'status': 'started', 'album_id': album_id,
                'subscribed_only': all_subscribed}

    @app.post('/api/albums/{album_id}/subscribe')
    async def set_subscribe(album_id: int, subscribed: bool = True):
        """订阅/取消订阅。订阅中的专辑会被定时任务自动追更下载。"""
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        if not _state_db.album_row(album_id):
            return JSONResponse({'error': 'not found'}, 404)
        _state_db.set_subscribed(album_id, subscribed)
        LOG.info('album=%s subscribed=%s', album_id, subscribed)
        return {'album_id': album_id, 'subscribed': bool(subscribed)}

    @app.post('/api/retry-failed')
    async def retry_failed(album_id: int | None = None):
        if not _state_db:
            return JSONResponse({'error': 'not ready'}, 503)
        count = _state_db.reset_failed(album_id)
        return {'reset': count}

    @app.post('/api/scan-existing')
    async def scan_existing_api():
        if not _archiver_ref:
            return JSONResponse({'error': 'not ready'}, 503)
        archiver = _archiver_ref[0]

        def work():
            from ..scanner import scan_existing
            result = scan_existing(archiver.root, archiver.state,
                                   archiver.minimum_duration)
            LOG.info('scan existing result: %s', result)

        if not _start_background('scan-existing', work):
            return JSONResponse({'error': '同步正在进行中'}, 409)
        return {'status': 'started'}

    @app.get('/api/logs')
    async def get_logs(n: int = Query(200, ge=1, le=2000)):
        if not _log_buffer:
            return []
        return _log_buffer.recent(n)

    @app.get('/api/settings')
    async def settings():
        """Read-only runtime settings for the WebUI footer/settings panel."""
        if not _archiver_ref:
            return JSONResponse({'error': 'not ready'}, 503)
        archiver = _archiver_ref[0]
        return {
            'base_url': archiver.client.base_url,
            'base_urls': archiver.client.base_urls,
            'media_root': str(archiver.root),
            'categories': [c['name'] for c in archiver.config['site']['categories']],
            'retries': archiver.retries,
            'minimum_duration': archiver.minimum_duration,
            'recheck_days': archiver.recheck_days,
            'failure_cooldown_hours': archiver.failure_cooldown_hours,
            'episode_timeout': archiver.episode_timeout,
        }

    @app.get('/api/sync-status')
    async def sync_status():
        return _sync_status

    @app.websocket('/ws/logs')
    async def ws_logs(websocket: WebSocket):
        if not _log_buffer:
            await websocket.close(code=1011)
            return
        await websocket.accept()
        loop, q = _log_buffer.subscribe()
        # Push whatever is already buffered so a fresh page is not blank until
        # the next log line happens to arrive.
        for entry in _log_buffer.recent(200):
            await websocket.send_json(entry)
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Keepalive: the client reconnects if this ever fails.
                    await websocket.send_json({'level': 'PING', 'message': '',
                                               'timestamp': 0, 'keepalive': True})
                    continue
                await websocket.send_json(entry)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            LOG.debug('log websocket closed unexpectedly', exc_info=True)
        finally:
            _log_buffer.unsubscribe(loop, q)

    return app
