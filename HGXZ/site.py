from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, urlopen
from urllib.request import getproxies as url_getproxies
import json
import logging
import random
import time

from .edge_crypto import EdgeChannel


LOG = logging.getLogger(__name__)


class HuangguoClient:
    def __init__(
        self,
        base_url: str,
        request_interval: float = 2.0,
        timeout: int = 30,
        backup_urls: list[str] | None = None,
    ):
        urls = [base_url, *(backup_urls or [])]
        self.base_urls = list(dict.fromkeys(url.rstrip('/') for url in urls if url))
        if not self.base_urls:
            raise ValueError('at least one site base URL is required')
        self.base_url = self.base_urls[0]
        self.request_interval = request_interval
        self.timeout = timeout
        self.last_request = 0.0
        self.user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36'
        self._backup_requests = 0
        self._edge = EdgeChannel(self._edge_transport)
        # 显式 opener：容器里常驻代理时，urlopen 的全局默认 opener 不一定吃到环境变量，
        # 这里按环境代理构造一次，失败退化为直连。
        proxies = url_getproxies() or {}
        self._opener = build_opener(ProxyHandler(proxies)) if proxies else build_opener()

    def _throttle(self) -> None:
        wait = self.request_interval - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, min(0.25, self.request_interval / 4)))

    def _candidates(self, is_absolute: bool) -> list[str | None]:
        if is_absolute:
            return [None]
        candidates = [self.base_url, *[url for url in self.base_urls if url != self.base_url]]
        if self.base_url != self.base_urls[0]:
            # After a failover, probe the primary host now and then so a
            # transient outage does not pin us to a backup forever.
            self._backup_requests += 1
            if self._backup_requests >= 20:
                self._backup_requests = 0
                candidates = [self.base_urls[0], self.base_url, *[url for url in self.base_urls if url not in (self.base_urls[0], self.base_url)]]
        return candidates

    def _request(self, method: str, path: str, *, data: bytes | None = None, headers: dict | None = None) -> bytes:
        self._throttle()
        is_absolute = path.startswith('http')
        candidates = self._candidates(is_absolute)
        last_error: Exception | None = None
        try:
            for index, candidate in enumerate(candidates):
                url = path if is_absolute else candidate + path
                referer = self.base_url + '/' if is_absolute else candidate + '/'
                request_headers = {'User-Agent': self.user_agent, 'Referer': referer}
                request_headers.update(headers or {})
                req = Request(url, method=method, data=data, headers=request_headers)
                try:
                    with urlopen(req, timeout=self.timeout) as response:
                        body = response.read()
                    if candidate and candidate != self.base_url:
                        LOG.warning('site base URL switched to: %s', candidate)
                        self.base_url = candidate
                        self._backup_requests = 0
                    return body
                except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as error:
                    last_error = error
                    if is_absolute or index == len(candidates) - 1:
                        raise
                    LOG.warning('site request failed via %s; trying backup: %s', candidate, error)
            raise last_error or RuntimeError('site request failed')
        finally:
            self.last_request = time.monotonic()

    def get_text(self, path: str) -> str:
        return self._request('GET', path).decode('utf-8', errors='replace')

    def get_bytes(self, url: str) -> bytes:
        return self._request('GET', url)

    # -- 加密通道 ---------------------------------------------------------
    def _edge_transport(self, base_url: str, endpoint: str, body: bytes) -> tuple[int, bytes]:
        """给 EdgeChannel 用的裸传输：只发到指定的 base_url，不做故障切换。

        会话是按站点主机握手的，换主机必须重新握手，因此这里固定单主机；
        失败时由调用方决定是否换下一个候选主机重来一遍。
        """
        url = base_url.rstrip('/') + endpoint
        req = Request(url, method='POST', data=body, headers={
            'User-Agent': self.user_agent,
            'Referer': base_url.rstrip('/') + '/',
            'Origin': base_url.rstrip('/'),
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })
        # 站点边缘节点会零星回 502 / 断开 TLS，握手和调用都需要重试几次才稳。
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with self._opener.open(req, timeout=self.timeout) as response:
                    return response.status, response.read()
            except HTTPError as error:
                payload = error.read() if error.fp else b''
                if error.code in (502, 503, 504) and attempt < 3:
                    last_error = error
                    time.sleep(0.8 * (attempt + 1))
                    continue
                return error.code, payload
            except (URLError, TimeoutError, ConnectionError, OSError) as error:
                last_error = error
                if attempt < 3:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise
        raise last_error or RuntimeError('edge transport failed')

    def _edge_call(self, scope: str, method: str, path: str, *,
                   body: bytes | None = None,
                   content_type: str = '') -> bytes:
        """在当前站点主机上执行一次加密调用，主机失效时依次重试候选主机。"""
        self._throttle()
        candidates = [self.base_url, *[u for u in self.base_urls if u != self.base_url]]
        last_error: Exception | None = None
        try:
            for candidate in candidates:
                try:
                    status, payload = self._edge.call(
                        candidate, scope, method, path,
                        body=body, content_type=content_type)
                except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as error:
                    last_error = error
                    self._edge.reset()
                    continue
                except RuntimeError as error:
                    # 握手/协议错误多半是该主机不可用，换一个再试
                    last_error = error
                    self._edge.reset()
                    continue
                if candidate != self.base_url:
                    LOG.warning('site base URL switched to: %s', candidate)
                    self.base_url = candidate
                    self._backup_requests = 0
                if status != 200:
                    raise RuntimeError(
                        f'加密接口返回 HTTP {status}: '
                        f'{payload[:120].decode("utf-8", "replace")}')
                return payload
            raise last_error or RuntimeError('加密接口调用失败')
        finally:
            self.last_request = time.monotonic()

    def play_url(self, album_id: int, episode: int) -> tuple[str, dict]:
        """取某集的播放清单地址，返回 (m3u8 地址, CDN 请求头)。

        旧接口 ``GET /api/videos/<id>/play`` 已废弃（恒返回空 video_url），
        现在必须走加密通道的 ``POST /api/videos/<id>/episodes/<ep>/playback``。
        CDN 侧对清单/密钥/分片有额外的鉴权头，下载器必须带着它们请求。
        """
        path = f'/api/videos/{int(album_id)}/episodes/{int(episode)}/playback'
        raw = self._edge_call('playback', 'POST', path,
                             body=None, content_type='application/json')
        payload = json.loads(raw)
        data = payload.get('data') or payload
        streams = data.get('streams') or {}
        url = (streams.get('main') or streams.get('preview') or '').strip()
        if not url:
            raise RuntimeError('empty video_url')
        return url, self._cdn_headers(data.get('cdn') or {})

    def _cdn_headers(self, cdn: dict) -> dict:
        """把 playback 响应里的 cdn 段翻译成需要附加的请求头。

        ``x-cdn-app`` 缺了会 403 —— 前端在 xhrSetup 里成对设置，别只抄一半。
        """
        headers: dict[str, str] = {}
        auth_key = str(cdn.get('authKey') or '').strip()
        auth_value = str(cdn.get('authValue') or '').strip()
        app = str(cdn.get('app') or '').strip()
        if auth_key and auth_value:
            headers[auth_key] = auth_value
        if app:
            headers['x-cdn-app'] = app
        return headers

    def cover_url(self, album_id: int) -> str:
        body = json.dumps({'items': [{'type': 'video', 'id': int(album_id)}]},
                          ensure_ascii=False).encode()
        raw = self._edge_call('public', 'POST', '/api/media/covers',
                              body=body, content_type='application/json')
        payload = json.loads(raw)
        covers = (payload.get('data') or {}).get('covers') or {}
        return (covers.get(f'video:{int(album_id)}') or '').strip()
