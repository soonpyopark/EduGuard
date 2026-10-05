"""키워드 화이트리스트 기반 로컬 HTTP/HTTPS 필터링 프록시 (표준 라이브러리만 사용).

동작 방식
- HTTPS: 브라우저가 보내는 `CONNECT host:443` 의 호스트명만 검사한다.
         (복호화/인증서 설치 없이 도메인 단위 차단이 가능 → 가장 안정적)
- HTTP : `GET http://host/path` 의 호스트를 검사한다.
- 호스트명에 허용 키워드가 하나라도 포함되면 통과, 아니면 403 Forbidden.
- IP 주소 직접 접속 / localhost 는 항상 차단 (키워드 우회 방지).
- 허용 도메인이라도 DNS 결과가 사설/루프백 IP 이면 연결하지 않음 (로컬 터널 우회 방지).
"""
from __future__ import annotations

import html
import ipaddress
import select
import socket
import socketserver
import threading
import time
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

MAX_HEAD_BYTES = 64 * 1024
IDLE_TIMEOUT = 120  # 초: 양방향 무통신 시 연결 종료
CONNECT_TIMEOUT = 10
TRUSTED_LOCAL_HOSTS = {"localhost.axissoft.co.kr"}

_HOP_HEADERS = {
    b"proxy-connection", b"connection", b"keep-alive", b"proxy-authorization",
    b"te", b"trailer", b"upgrade", b"host",
}


# --------------------------------------------------------------------------- 필터
class KeywordFilter:
    """호스트명에 키워드가 포함되는지 검사."""

    def __init__(self, keywords: Iterable[str] = ()):
        self._lock = threading.RLock()
        self._keywords: Tuple[str, ...] = ()
        self.set_keywords(keywords)

    def set_keywords(self, keywords: Iterable[str]) -> None:
        cleaned = tuple(sorted({k.strip().lower() for k in keywords if k and k.strip()}))
        with self._lock:
            self._keywords = cleaned

    @property
    def keywords(self) -> List[str]:
        with self._lock:
            return list(self._keywords)

    @staticmethod
    def normalize_host(host: str) -> str:
        host = (host or "").strip().lower().rstrip(".")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        return host

    def check(self, host: str) -> Tuple[bool, str]:
        """(허용 여부, 근거) 반환. 근거는 매칭된 키워드 또는 차단 사유."""
        host = self.normalize_host(host)
        if not host:
            return False, "빈 호스트"
        try:
            ipaddress.ip_address(host)
            return False, "IP 직접 접속"
        except ValueError:
            pass
        if host == "localhost" or host.endswith(".localhost"):
            return False, "localhost"
        with self._lock:
            for kw in self._keywords:
                if kw in host:
                    return True, kw
        return False, "키워드 불일치"

    def is_allowed(self, host: str) -> bool:
        return self.check(host)[0]


# --------------------------------------------------------------------------- 유틸
def _split_host_port(target: str, default_port: int) -> Tuple[str, int]:
    target = target.strip()
    if target.startswith("["):  # [IPv6]:port
        end = target.find("]")
        host = target[1:end]
        rest = target[end + 1:]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    if target.count(":") == 1:
        host, _, p = target.partition(":")
        return host, int(p) if p.isdigit() else default_port
    return target, default_port


def _is_trusted_local_host(host: str, trusted_hosts: Iterable[str]) -> bool:
    return KeywordFilter.normalize_host(host) in {KeywordFilter.normalize_host(h) for h in trusted_hosts}


def default_connect(host: str, port: int, trusted_local_hosts: Iterable[str] = ()) -> socket.socket:
    """DNS 조회 후 공인 IP 로만 연결한다."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    last_err: Optional[Exception] = None
    saw_public = False
    allow_local = _is_trusted_local_host(host, trusted_local_hosts)
    for family, socktype, proto, _, sockaddr in infos:
        try:
            if not ipaddress.ip_address(sockaddr[0]).is_global and not allow_local:
                continue
        except ValueError:
            continue
        saw_public = True
        s = socket.socket(family, socktype, proto)
        s.settimeout(CONNECT_TIMEOUT)
        try:
            s.connect(sockaddr)
            return s
        except OSError as e:
            last_err = e
            s.close()
    if not saw_public:
        raise PermissionError(f"{host} 는 공인 IP 로 해석되지 않아 차단됨")
    raise last_err or OSError("연결 실패")


def _block_page(host: str, message: str = "허용된 학습 사이트가 아닙니다.") -> bytes:
    safe_message = html.escape(message.strip() or "허용된 학습 사이트가 아닙니다.")
    body = (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
        "<title>차단됨</title></head>"
        "<body style='font-family:Malgun Gothic,sans-serif;text-align:center;margin-top:15%'>"
        "<h1>🚫 접속이 차단되었습니다</h1>"
        f"<p><b>{html.escape(host)}</b> 은(는) {safe_message}</p>"
        "<p style='color:#888'>EduGuard 학습 모드</p></body></html>"
    ).encode("utf-8")
    head = (
        "HTTP/1.1 403 Forbidden\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    return head + body


def _simple_response(code: int, reason: str, text: str = "") -> bytes:
    body = text.encode("utf-8")
    return (
        f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode("ascii") + body


def _pipe(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    while True:
        readable, _, errored = select.select(socks, [], socks, IDLE_TIMEOUT)
        if errored or not readable:
            return
        for s in readable:
            data = s.recv(65536)
            if not data:
                return
            (b if s is a else a).sendall(data)


# --------------------------------------------------------------------------- 서버
class _Handler(socketserver.BaseRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        proxy = self.server.proxy
        client: socket.socket = self.request
        upstream: Optional[socket.socket] = None
        try:
            client.settimeout(30)
            head, rest = self._read_head(client)
            if head is None:
                return
            lines = head.split(b"\r\n")
            parts = lines[0].decode("latin-1").split(" ")
            if len(parts) != 3:
                client.sendall(_simple_response(400, "Bad Request"))
                return
            method, target, version = parts

            if method.upper() == "CONNECT":
                upstream = self._handle_connect(proxy, client, target, rest)
            else:
                upstream = self._handle_http(proxy, client, method, target, version, lines[1:], rest)
            if upstream is not None:
                upstream.settimeout(30)
                _pipe(client, upstream)
        except (OSError, ValueError):
            pass
        finally:
            for s in (upstream, client):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    @staticmethod
    def _read_head(client: socket.socket) -> Tuple[Optional[bytes], bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > MAX_HEAD_BYTES:
                return None, b""
            chunk = client.recv(8192)
            if not chunk:
                return None, b""
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _open_upstream(self, proxy: "FilterProxy", client, host: str, port: int) -> Optional[socket.socket]:
        try:
            return proxy.connect(host, port)
        except PermissionError:
            proxy._emit("BLOCK", host, "사설/루프백 IP")
            client.sendall(proxy.block_page(host))
        except OSError:
            proxy._emit("ERROR", host, "연결 실패")
            client.sendall(_simple_response(502, "Bad Gateway", "Upstream connection failed"))
        return None

    def _handle_connect(self, proxy, client, target: str, rest: bytes) -> Optional[socket.socket]:
        host, port = _split_host_port(target, 443)
        allowed, why = proxy.filter.check(host)
        if not allowed:
            proxy._emit("BLOCK", host, why)
            client.sendall(proxy.block_page(host))
            return None
        upstream = self._open_upstream(proxy, client, host, port)
        if upstream is None:
            return None
        proxy._emit("ALLOW", host, why)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if rest:
            upstream.sendall(rest)
        return upstream

    def _handle_http(self, proxy, client, method, target, version, header_lines, rest) -> Optional[socket.socket]:
        if not target.lower().startswith("http://"):
            client.sendall(_simple_response(400, "Bad Request", "This is a filtering proxy."))
            return None
        u = urlsplit(target)
        host = u.hostname or ""
        port = u.port or 80
        allowed, why = proxy.filter.check(host)
        if not allowed:
            proxy._emit("BLOCK", host, why)
            client.sendall(proxy.block_page(host))
            return None
        upstream = self._open_upstream(proxy, client, host, port)
        if upstream is None:
            return None
        proxy._emit("ALLOW", host, why)

        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        netloc = host if port == 80 else f"{host}:{port}"
        out = [f"{method} {path} {version}".encode("latin-1"), f"Host: {netloc}".encode("latin-1")]
        for line in header_lines:
            name = line.split(b":", 1)[0].strip().lower()
            if name and name not in _HOP_HEADERS:
                out.append(line)
        out.append(b"Connection: close")  # 한 연결에 한 요청만 → 다른 호스트로의 우회 방지
        upstream.sendall(b"\r\n".join(out) + b"\r\n\r\n" + rest)
        return upstream


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # Windows 에서 SO_REUSEADDR 는 '사용 중인 포트에도 바인드' 를 허용하므로 끈다.
    allow_reuse_address = False
    request_queue_size = 256

    def __init__(self, addr, handler, proxy: "FilterProxy"):
        self.proxy = proxy
        super().__init__(addr, handler)


class FilterProxy:
    """start()/stop() 으로 제어하는 필터링 프록시."""

    def __init__(
        self,
        keywords: Iterable[str] = (),
        host: str = "127.0.0.1",
        port: int = 8899,
        on_event: Optional[Callable[[dict], None]] = None,
        connect_func: Optional[Callable[[str, int], socket.socket]] = None,
        block_message: str = "허용된 학습 사이트가 아닙니다.",
        trusted_local_hosts: Iterable[str] = TRUSTED_LOCAL_HOSTS,
    ):
        self.filter = KeywordFilter(keywords)
        self.host = host
        self.port = port
        self.on_event = on_event
        self._connect_func = connect_func
        self.trusted_local_hosts = tuple(trusted_local_hosts)
        self.block_message = block_message
        self.allowed_count = 0
        self.blocked_count = 0
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._count_lock = threading.Lock()

    def set_block_message(self, message: str) -> None:
        self.block_message = message

    def block_page(self, host: str) -> bytes:
        return _block_page(host, self.block_message)

    def connect(self, host: str, port: int) -> socket.socket:
        if self._connect_func:
            return self._connect_func(host, port)
        return default_connect(host, port, self.trusted_local_hosts)

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self._server is not None:
            return
        server = _Server((self.host, self.port), _Handler, self)  # 포트 사용 중이면 OSError
        self.port = server.server_address[1]  # port=0 이면 실제 포트 반영
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="proxy", daemon=True,
                                        kwargs={"poll_interval": 0.3})
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _emit(self, action: str, host: str, detail: str) -> None:
        with self._count_lock:
            if action == "ALLOW":
                self.allowed_count += 1
            elif action == "BLOCK":
                self.blocked_count += 1
        if self.on_event:
            try:
                self.on_event({"time": time.strftime("%H:%M:%S"), "action": action,
                               "host": host, "detail": detail})
            except Exception:
                pass


if __name__ == "__main__":  # 단독 실행 테스트: python proxy_server.py
    import sys

    kws = sys.argv[1:] or ["ebs", "sevenedu", "kollus"]
    p = FilterProxy(kws, on_event=lambda e: print(e["time"], e["action"], e["host"], e["detail"]))
    p.start()
    print(f"프록시 실행 중 127.0.0.1:{p.port}  키워드={kws}  (Ctrl+C 종료)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        p.stop()
