"""黄果站点（huangguoai.com）端到端加密接口通道。

站点在 2026-11 之后把播放/封面等接口收进了一条加密通道：
明文接口（如 ``GET /api/videos/<id>/play?ep=N``）仍然返回 200，但 ``video_url``
恒为空字符串，靠它已经取不到任何播放地址。真正可用的接口在 ``/api/<scope>/request``
后面，走一套标准的 ECDH + HKDF + AES-256-GCM 信封。

协议（站点前端 ``static/web/js/edge-api-crypto.js``，protocol 标识 ``dx150-edge-v1``）::

    1. 客户端生成 ECDH P-256 密钥对
    2. POST {v:1, type:'hello', client_pub=<b64url>}
       → {v, type:'hello', kid, sid, site, server_pub, salt, server_time, expires_at}
    3. shared = ECDH(client_priv, server_pub)
       info   = protocol \\n scope \\n site \\n kid \\n sid
       reqKey = HKDF-SHA256(salt, info + '\\nreq', 32)
       resKey = HKDF-SHA256(salt, info + '\\nres', 32)
    4. 请求：iv = 0x00*8 || seq(u32be)
       aad = info \\n 'req' \\n seq \\n ts
       ciphertext = AES-256-GCM(reqKey, iv, JSON{request, authorization?}, aad)
       POST {v:1, type:'call', kid, sid, seq, ts, ciphertext=<b64url>}
    5. 响应：aad 换成 'res'，用 resKey 解密，内层是
       {status, headers, body=<base64 标准编码>}

这是标准密码学而不是混淆，因此可以照原样在 Python 侧复刻。字段名（``v``/``type``/
``kid``/``sid``/``seq``/``ts``/``ciphertext``）必须与前端一致——服务端会校验信封形状。
"""
from __future__ import annotations

import base64
import json
import time
from typing import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL = 'dx150-edge-v1'
SCOPES = {
    'user': '/api/user/request',
    'playback': '/api/playback/request',
    'public': '/api/public/request',
    'dynamic': '/api/dynamic/request',
}
MAX_SEQUENCE = 16384

# transport(base_url, scope_endpoint_path, body_bytes) -> (status, body_bytes)
Transport = Callable[[str, str, bytes], 'tuple[int, bytes]']


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class _Session:
    __slots__ = ('info', 'kid', 'sid', 'clock_offset', 'expires_at',
                 'request_key', 'response_key', 'sequence')

    def __init__(self, info: str, kid: str, sid: str, clock_offset: int,
                 expires_at: int, request_key: bytes, response_key: bytes):
        self.info = info
        self.kid = kid
        self.sid = sid
        self.clock_offset = clock_offset
        self.expires_at = expires_at
        self.request_key = request_key
        self.response_key = response_key
        self.sequence = 0


class EdgeChannel:
    """维护每个 scope 的加密会话，并把一次调用封装成普通 HTTP 语义。

    ``transport`` 由调用方提供，负责真正的网络收发（以便复用站点客户端的
    代理、重试和故障切换逻辑）。它收到的是完整 URL 与已序列化的 body。
    """

    def __init__(self, transport: Transport):
        self._transport = transport
        self._sessions: dict[str, _Session] = {}

    # -- 会话管理 ---------------------------------------------------------
    def _open_session(self, base_url: str, scope: str) -> _Session:
        private_key = ec.generate_private_key(ec.SECP256R1())
        client_pub = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        hello_body = json.dumps(
            {'v': 1, 'type': 'hello', 'client_pub': _b64url_encode(client_pub)},
            separators=(',', ':'),
        ).encode()
        status, raw = self._transport(base_url, SCOPES[scope], hello_body)
        if status != 200:
            raise RuntimeError(f'加密握手失败 (HTTP {status})')

        try:
            hello = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError('加密握手响应不是合法 JSON') from exc
        if hello.get('v') != 1 or hello.get('type') != 'hello':
            raise RuntimeError('加密握手响应无效')

        try:
            server_pub = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), _b64url_decode(hello['server_pub']))
            salt = _b64url_decode(hello['salt'])
        except (KeyError, ValueError) as exc:
            raise RuntimeError('加密握手响应缺少密钥材料') from exc
        if len(salt) != 32:
            raise RuntimeError('加密握手 salt 长度异常')

        shared = private_key.exchange(ec.ECDH(), server_pub)
        info = '\n'.join([PROTOCOL, scope, hello['site'], hello['kid'], hello['sid']])

        def derive(direction: str) -> bytes:
            return HKDF(
                algorithm=hashes.SHA256(), length=32, salt=salt,
                info=f'{info}\n{direction}'.encode(),
            ).derive(shared)

        now = int(time.time())
        session = _Session(
            info=info,
            kid=hello['kid'],
            sid=hello['sid'],
            clock_offset=int(hello['server_time']) - now,
            expires_at=now + (int(hello['expires_at']) - int(hello['server_time'])),
            request_key=derive('req'),
            response_key=derive('res'),
        )
        self._sessions[scope] = session
        return session

    def _session(self, base_url: str, scope: str) -> _Session:
        existing = self._sessions.get(scope)
        if (existing
                and existing.expires_at > int(time.time()) + 5
                and existing.sequence < MAX_SEQUENCE):
            return existing
        return self._open_session(base_url, scope)

    def reset(self) -> None:
        """丢弃全部会话，下次调用重新握手。"""
        self._sessions.clear()

    # -- 调用 -------------------------------------------------------------
    def call(self, base_url: str, scope: str, method: str, path: str, *,
             body: bytes | None = None, content_type: str = '',
             authorization: str = '') -> tuple[int, bytes]:
        """在加密通道上执行一次请求，返回 (status, 响应体)。

        ``authorization`` 是明文 Bearer token（未登录时留空）。
        """
        if scope not in SCOPES:
            raise ValueError(f'未知的加密 scope: {scope}')

        session = self._session(base_url, scope)
        session.sequence += 1
        sequence = session.sequence
        timestamp = int(time.time()) + session.clock_offset
        iv = b'\x00' * 8 + sequence.to_bytes(4, 'big')

        packet: dict = {
            'request': {
                'method': method,
                'path': path,
                'body': base64.b64encode(body).decode('ascii') if body else '',
                'content_type': content_type,
            }
        }
        if scope != 'public' and authorization:
            packet['authorization'] = authorization

        request_aad = '\n'.join(
            [session.info, 'req', str(sequence), str(timestamp)]).encode()
        ciphertext = AESGCM(session.request_key).encrypt(
            iv, json.dumps(packet, separators=(',', ':')).encode(), request_aad)

        call_body = json.dumps({
            'v': 1, 'type': 'call', 'kid': session.kid, 'sid': session.sid,
            'seq': sequence, 'ts': timestamp,
            'ciphertext': _b64url_encode(ciphertext),
        }, separators=(',', ':')).encode()

        status, raw = self._transport(base_url, SCOPES[scope], call_body)
        if status != 200:
            # 会话过期时服务端回 session_expired，丢弃后由外层重试重新握手。
            if status in (401, 403) or b'session_expired' in raw[:512]:
                self._sessions.pop(scope, None)
            raise RuntimeError(
                f'加密接口请求失败 (HTTP {status}): '
                f'{raw[:120].decode("utf-8", "replace")}')

        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError('加密接口响应不是合法 JSON') from exc
        if (result.get('v') != 1 or result.get('type') != 'result'
                or result.get('sid') != session.sid
                or result.get('kid') != session.kid
                or result.get('seq') != sequence):
            raise RuntimeError('加密接口响应不匹配')

        response_aad = '\n'.join(
            [session.info, 'res', str(sequence), str(timestamp)]).encode()
        try:
            plaintext = AESGCM(session.response_key).decrypt(
                iv, _b64url_decode(result['ciphertext']), response_aad)
            inner = json.loads(plaintext)
        except Exception as exc:  # noqa: BLE001 - 解密失败一律视为会话失效
            self._sessions.pop(scope, None)
            raise RuntimeError('加密接口响应解密失败') from exc

        try:
            inner_status = int(inner['status'])
            inner_body = base64.b64decode(inner.get('body') or '')
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError('加密接口响应格式无效') from exc
        return inner_status, inner_body
