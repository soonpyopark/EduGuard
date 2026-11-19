"""Electron UI 가 엔진을 호출하는 로컬 HTTP API (127.0.0.1 전용, 토큰 인증).

- 모든 요청은 X-EduGuard-Token 헤더가 맞아야 한다. 브라우저 페이지는 사용자 지정 헤더를
  교차 출처로 보낼 수 없고(CORS 응답을 주지 않음), Host 헤더도 검사하므로 웹 페이지가 API 를 대신 호출할 수 없다.
- 비밀번호가 필요한 동작은 요청 본문의 password 로 매번 엔진이 검증한다.
"""
from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from engine import Engine, EngineError

MAX_BODY = 1024 * 1024
TOKEN_HEADER = "X-EduGuard-Token"

Route = Callable[[Engine, dict, dict], dict]


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _routes() -> Dict[Tuple[str, str], Route]:
    return {
        ("GET", "/api/ping"): lambda e, b, q: {"version": e.state()["version"]},
        ("GET", "/api/poll"): lambda e, b, q: e.poll(_int(q.get("since"))),
        ("GET", "/api/keywords"): lambda e, b, q: {"keywords": e.cfg.keywords},
        ("GET", "/api/proxy-status"): lambda e, b, q: {"report": e.proxy_status()},
        ("POST", "/api/setup-password"): lambda e, b, q: e.setup_password(b.get("password", "")) or {},
        ("POST", "/api/password/change"): lambda e, b, q: e.change_password(
            b.get("password"), b.get("new_password", "")) or {},
        ("POST", "/api/block/start"): lambda e, b, q: e.start_blocking() or {},
        ("POST", "/api/block/stop"): lambda e, b, q: e.stop_blocking(b.get("password")) or {},
        ("POST", "/api/block/temp-unlock"): lambda e, b, q: e.temp_unlock(b.get("password"), b.get("minutes")) or {},
        ("POST", "/api/keywords/add"): lambda e, b, q: e.add_keyword(b.get("password"), b.get("keyword", "")) or {},
        ("POST", "/api/keywords/remove"): lambda e, b, q: e.remove_keywords(b.get("password"), b.get("keywords")) or {},
        ("POST", "/api/keywords/reset"): lambda e, b, q: e.reset_keywords(b.get("password")) or {},
        ("POST", "/api/keywords/import"): lambda e, b, q: e.import_keywords(
            b.get("password"), b.get("keywords"), str(b.get("source", ""))) or {},
        ("POST", "/api/test-domain"): lambda e, b, q: e.test_domain(b.get("domain", "")),
        ("POST", "/api/verify"): lambda e, b, q: e.verify(b.get("password"), str(b.get("purpose", "확인"))) or {},
        ("POST", "/api/settings/get"): lambda e, b, q: e.get_settings(b.get("password")),
        ("POST", "/api/settings/save"): lambda e, b, q: e.save_settings(b.get("password"), b.get("settings")),
        ("POST", "/api/open"): lambda e, b, q: e.open_target(str(b.get("target", ""))) or {},
        ("POST", "/api/exit"): lambda e, b, q: e.request_exit(b.get("password")) or {},
        ("POST", "/api/update/check"): lambda e, b, q: e.check_update_now(),
    }


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - 콘솔 없는 실행에서 stderr 출력 방지
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        token = self.headers.get(TOKEN_HEADER) or ""
        return hmac.compare_digest(token.encode("utf-8"), self.server.token.encode("utf-8"))

    def _dispatch(self, method: str) -> None:
        if not self._authorized():
            self._send(403, {"ok": False, "error": "인증되지 않은 요청입니다."})
            return
        parts = urlsplit(self.path)
        route = self.server.routes.get((method, parts.path))
        if route is None:
            self._send(404, {"ok": False, "error": "알 수 없는 요청입니다."})
            return
        body: dict = {}
        if method == "POST":
            length = _int(self.headers.get("Content-Length"))
            if length > MAX_BODY:
                self._send(413, {"ok": False, "error": "요청이 너무 큽니다."})
                return
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"ok": False, "error": "잘못된 JSON 입니다."})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"ok": False, "error": "잘못된 요청 본문입니다."})
                    return
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            result = route(self.server.engine, body, query) or {}
        except EngineError as e:
            self._send(e.status, {"ok": False, "error": str(e), "status": e.status})
            return
        except Exception as e:  # 예상하지 못한 오류도 UI 에 메시지로 돌려준다
            self._send(500, {"ok": False, "error": f"내부 오류: {e}"})
            return
        self._send(200, {"ok": True, **result})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, engine: Engine, token: str, port: int):
        self.engine = engine
        self.token = token
        self.routes = _routes()
        super().__init__(("127.0.0.1", port), _Handler)


class ApiServer:
    def __init__(self, engine: Engine, token: str, port: int = 0):
        self._server = _Server(engine, token, port)
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="api-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
