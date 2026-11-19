"""EduGuard 헤드리스 엔진: 차단 / 일시 해제 / 스케줄 / 자동 차단 / 로그 / 비밀번호 검증.

UI(Electron)는 api_server 를 통해 이 엔진만 호출한다. UI 창이 꺼져도 엔진은 계속 동작한다.
모든 공개 메서드는 HTTP 스레드에서 동시에 호출될 수 있으므로 스레드 안전해야 한다.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

import starplayer_ready
import startup
import update_checker
import watchdog
from config_store import ConfigError, ConfigStore, MIN_PASSWORD_LEN, REMOTE_SUPPORT_KEYWORDS, SETTING_KEYS
from proxy_server import FilterProxy
from system_proxy import ProxyGuard, SystemProxy, is_admin
from version import APP_BUILD_TIME, APP_NAME, APP_VERSION

DEVELOPER_URL = "https://note4all.tistory.com"
MAX_EVENTS = 500
SCHEDULE_CHECK_SECONDS = 30
STARPLAYER_RETRY_SECONDS = 5


class EngineError(Exception):
    status = 400


class AuthError(EngineError):
    status = 401


class LockedError(AuthError):
    status = 423


class Engine:
    def __init__(
        self,
        cfg: ConfigStore,
        base_dir: str,
        *,
        proxy=None,
        sysproxy=None,
        admin: Optional[bool] = None,
        starplayer_check: Optional[Callable[[], bool]] = None,
        startup_module=startup,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.base_dir = base_dir
        self.backup_path = os.path.join(base_dir, "proxy_backup.json")
        self.log_dir = os.path.join(base_dir, "logs")
        self.audit_log_path = os.path.join(self.log_dir, "settings-history.log")
        self.watchdog_flag_path = watchdog.default_flag_path(base_dir)
        self.admin = is_admin() if admin is None else admin
        self._clock = clock
        self._startup = startup_module
        self._starplayer_check = starplayer_check or starplayer_ready.is_starplayer_ready

        self._lock = threading.RLock()
        self._auth_lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._events: deque = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._recent_log_events: dict[tuple[str, str, str], float] = {}
        self._last_log_prune = ""

        self.proxy = proxy or FilterProxy(
            self._effective_keywords(), port=cfg.port, on_event=self._on_proxy_event,
            block_message=cfg.block_message,
        )
        self.sysproxy = sysproxy or SystemProxy(self.backup_path)
        self.guard = ProxyGuard(self.sysproxy, interval=cfg.guard_interval,
                                on_fix=lambda: self._log("GUARD", "시스템 프록시", "임의 변경 감지 → 복구"))

        self.blocking = False
        self.temp_unlock_until = 0.0
        self.update_info: Optional[dict] = None
        self._fails = 0
        self._locked_until = 0.0
        self._auto_block_waiting = False
        self._auto_block_started_at = 0.0
        self._auto_block_next_try = 0.0
        self._auto_block_logged_wait = False
        self._last_schedule_check = 0.0
        self._session_restored = False
        self._watchdog_proc: Optional[subprocess.Popen] = None

        self._stop = threading.Event()
        self.exit_requested = threading.Event()
        self._ticker: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ 수명 주기
    def start(self) -> None:
        """백그라운드 동작 시작: 잔여 프록시 복원 → 워치독 → 자동 차단 예약 → 주기 작업."""
        self._prune_logs()
        self.clear_stale_proxy_if_needed()
        self._start_watchdog_if_needed()
        if self.cfg.auto_start and self.cfg.has_password() and not self.cfg.schedule_enabled:
            self._schedule_auto_block()
        threading.Thread(target=self._ensure_startup_task, name="startup-task", daemon=True).start()
        threading.Thread(target=self._check_updates, name="update-check", daemon=True).start()
        self._ticker = threading.Thread(target=self._run_ticker, name="engine-ticker", daemon=True)
        self._ticker.start()

    def close(self) -> None:
        self._stop.set()
        if self._ticker:
            self._ticker.join(timeout=3)
            self._ticker = None

    def wait_for_exit(self) -> None:
        while not self.exit_requested.wait(0.5):
            pass

    def _run_ticker(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception as e:  # 주기 작업 하나가 실패해도 엔진은 계속 돈다
                self._log("ERROR", "엔진", str(e))

    def tick(self) -> None:
        now = self._clock()
        with self._lock:
            if self._auto_block_waiting and now >= self._auto_block_next_try:
                self._try_auto_block()
            if self.temp_unlock_until and now >= self.temp_unlock_until:
                self.temp_unlock_until = 0.0
                self._log("GUARD", "일시 해제", "시간 만료")
                if not self.cfg.schedule_enabled or self._within_schedule():
                    self._start_blocking_quietly()
                    self._notify("일시 해제가 끝나 차단을 다시 시작했습니다.")
            if now - self._last_schedule_check >= SCHEDULE_CHECK_SECONDS:
                self._last_schedule_check = now
                if self.cfg.schedule_enabled and self.cfg.has_password():
                    if self._within_schedule() and not self.temp_unlock_until and not self.blocking:
                        self._start_blocking_quietly()
                    elif not self._within_schedule() and self.blocking:
                        self._stop_blocking_locked(silent=True)
        if self.cfg.log_to_file and self._last_log_prune != datetime.now().strftime("%Y%m%d"):
            self._prune_logs()

    # ------------------------------------------------------------------ 이벤트 / 로그
    def _append(self, item: dict) -> None:
        with self._events_lock:
            self._seq += 1
            item["id"] = self._seq
            self._events.append(item)

    def _log(self, action: str, host: str, detail: str) -> None:
        self._on_proxy_event({"time": time.strftime("%H:%M:%S"), "action": action, "host": host, "detail": detail})

    def _on_proxy_event(self, ev: dict) -> None:
        if self._should_suppress_log(ev):
            return
        self._append({"kind": "log", **ev})
        self._write_access_log(ev)

    def _notify(self, message: str) -> None:
        if self.cfg.tray_notifications:
            self._append({"kind": "notify", "time": time.strftime("%H:%M:%S"), "title": APP_NAME, "detail": message})

    def poll(self, since: int = 0) -> dict:
        with self._events_lock:
            if since > self._seq:  # 엔진이 재시작되어 번호가 처음부터 다시 시작된 경우
                since = 0
            items = [e for e in self._events if e["id"] > since]
            last = self._seq
        return {"state": self.state(), "events": items, "last_id": last}

    def _should_suppress_log(self, ev: dict) -> bool:
        """ALLOW/BLOCK 중복 로그만 숨긴다. 차단/허용 판단과 카운터에는 영향을 주지 않는다."""
        if not self.cfg.suppress_repeated_logs or ev.get("action") not in ("ALLOW", "BLOCK"):
            return False
        now = time.time()
        window = self.cfg.repeat_log_window_seconds
        key = (str(ev.get("action", "")), str(ev.get("host", "")), str(ev.get("detail", "")))
        with self._events_lock:
            last = self._recent_log_events.get(key)
            self._recent_log_events[key] = now
            cutoff = now - max(window * 2, 60)
            for old_key, ts in list(self._recent_log_events.items()):
                if ts < cutoff:
                    self._recent_log_events.pop(old_key, None)
        return last is not None and now - last < window

    def _write_access_log(self, ev: dict) -> None:
        if not self.cfg.log_to_file or ev.get("action") not in ("ALLOW", "BLOCK", "GUARD", "ERROR"):
            return
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            today = datetime.now().strftime("%Y%m%d")
            if self._last_log_prune != today:
                self._prune_logs()
            path = os.path.join(self.log_dir, f"eduguard-{today}.log")
            line = f"{datetime.now().isoformat(timespec='seconds')}\t{ev['action']}\t{ev['host']}\t{ev['detail']}\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _prune_logs(self) -> None:
        self._last_log_prune = datetime.now().strftime("%Y%m%d")
        cutoff = time.time() - (self.cfg.log_retention_days * 86400)
        for path in glob.glob(os.path.join(self.log_dir, "eduguard-*.log")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    def audit(self, action: str, detail: str) -> None:
        if not self.cfg.audit_enabled:
            return
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{action}\t{detail}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ 상태
    def status(self) -> str:
        with self._lock:
            if self.temp_unlock_until:
                return "temp_unlock"
            if self.blocking:
                return "blocking"
            if self._auto_block_waiting:
                return "waiting"
            return "off"

    def _temp_unlock_remaining(self) -> int:
        remain = self.temp_unlock_until - self._clock()
        return max(0, int(remain + 59) // 60)

    def status_text(self) -> str:
        status = self.status()
        if status == "temp_unlock":
            return f"일시 해제 중 ({self._temp_unlock_remaining()}분 남음)"
        if status == "blocking":
            return "차단 중"
        if status == "waiting":
            return "자동 차단 대기 중"
        return "차단 해제됨"

    def state(self) -> dict:
        with self._lock:
            return {
                "status": self.status(),
                "status_text": self.status_text(),
                "temp_unlock_remaining": self._temp_unlock_remaining() if self.temp_unlock_until else 0,
                "temp_unlock_minutes": self.cfg.temp_unlock_minutes,
                "keywords": self.cfg.keywords,
                "remote_support": self.cfg.remote_support_enabled,
                "allowed": self.proxy.allowed_count,
                "blocked": self.proxy.blocked_count,
                "has_password": self.cfg.has_password(),
                "tampered": self.cfg.tampered,
                "lock_remaining": max(0, int(self._locked_until - self._clock()) + 1) if self._locked_until > self._clock() else 0,
                "admin": self.admin,
                "close_to_tray": self.cfg.close_to_tray,
                "start_in_tray": self.cfg.start_in_tray,
                "tray_notifications": self.cfg.tray_notifications,
                "version": APP_VERSION,
                "build_time": APP_BUILD_TIME,
                "developer_url": DEVELOPER_URL,
                "min_password_len": MIN_PASSWORD_LEN,
                "update": self.update_info,
            }

    def _effective_keywords(self) -> list[str]:
        keywords = self.cfg.keywords
        if self.cfg.remote_support_enabled:
            keywords = keywords + [kw for kw in REMOTE_SUPPORT_KEYWORDS if kw not in keywords]
        return keywords

    def _refresh_keywords(self) -> None:
        self.proxy.filter.set_keywords(self._effective_keywords())  # 실행 중에도 즉시 반영

    # ------------------------------------------------------------------ 비밀번호
    def verify(self, password: Optional[str], purpose: str) -> None:
        """틀리면 AuthError, 잠금 중이면 LockedError. 실패 횟수 초과 시 일정 시간 잠근다."""
        with self._auth_lock:
            remain = self._locked_until - self._clock()
            if remain > 0:
                raise LockedError(f"비밀번호를 여러 번 틀려 {int(remain) + 1}초 동안 잠겼습니다.")
            if self.cfg.verify_password(password or ""):
                self._fails = 0
                self.audit("PASSWORD", purpose)
                return
            self._fails += 1
            if self._fails >= self.cfg.password_max_fails:
                self._locked_until = self._clock() + self.cfg.password_lockout_seconds
                self._fails = 0
                self.audit("PASSWORD_LOCK", f"{self.cfg.password_lockout_seconds}초 잠금")
                raise LockedError(f"비밀번호를 여러 번 틀려 {self.cfg.password_lockout_seconds}초 동안 잠겼습니다.")
            raise AuthError("비밀번호가 틀렸습니다.")

    def setup_password(self, password: str) -> None:
        """최초 1회 비밀번호 설정 (이미 설정되어 있으면 거부)."""
        with self._lock:
            if self.cfg.has_password():
                raise EngineError("이미 비밀번호가 설정되어 있습니다.")
            try:
                self.cfg.set_password(password or "")
            except ConfigError as e:
                raise EngineError(str(e))
            self.audit("PASSWORD_SETUP", "최초 비밀번호 설정")
            if self.cfg.auto_start and not self.cfg.schedule_enabled and not self.blocking:
                self._schedule_auto_block()

    def change_password(self, password: str, new_password: str) -> None:
        self.verify(password, "비밀번호를 변경합니다.")
        try:
            self.cfg.set_password(new_password or "")
        except ConfigError as e:
            raise EngineError(str(e))
        self.audit("PASSWORD_CHANGE", "비밀번호 변경")

    # ------------------------------------------------------------------ 차단 제어
    def start_blocking(self, silent: bool = False) -> None:
        with self._lock:
            if self.blocking:
                return
            self._auto_block_waiting = False
            try:
                self.proxy.port = self.cfg.port
                self._refresh_keywords()
                self.proxy.set_block_message(self.cfg.block_message)
                self.guard.interval = self.cfg.guard_interval
                self.proxy.start()
            except OSError as e:
                raise EngineError(f"프록시를 시작할 수 없습니다 (포트 {self.cfg.port} 사용 중?)\n{e}")
            self.sysproxy.enable(self.proxy.port)
            self.guard.start()
            self.blocking = True
            self.temp_unlock_until = 0.0
            self._session_restored = False
            self.audit("BLOCK_START", f"port={self.proxy.port}")
            if not silent:
                self._notify("차단을 시작했습니다.")
            if not self.admin:
                self._log("GUARD", "권한", "관리자 권한이 아님: 다른 사용자 계정에는 적용되지 않을 수 있음")

    def _start_blocking_quietly(self) -> None:
        try:
            self.start_blocking(silent=True)
        except EngineError as e:
            self._log("ERROR", "차단 시작", str(e))

    def stop_blocking(self, password: str) -> None:
        self.verify(password, "차단을 해제합니다.")
        with self._lock:
            self._auto_block_waiting = False
            self._stop_blocking_locked(silent=False)

    def _stop_blocking_locked(self, silent: bool) -> None:
        if not self.blocking:
            return
        self.guard.stop()
        self.sysproxy.disable()
        self.proxy.stop()
        self.blocking = False
        self.audit("BLOCK_STOP", "차단 해제")
        if not silent:
            self._notify("차단을 해제했습니다.")

    def temp_unlock(self, password: str, minutes) -> None:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise EngineError("일시 해제 시간은 숫자여야 합니다.")
        if not 1 <= minutes <= 480:
            raise EngineError("일시 해제 시간은 1 ~ 480분 사이여야 합니다.")
        with self._lock:
            if not self.blocking:
                raise EngineError("차단 중일 때만 일시 해제할 수 있습니다.")
        self.verify(password, f"{minutes}분 동안 차단을 일시 해제합니다.")
        with self._lock:
            self._stop_blocking_locked(silent=True)
            self.temp_unlock_until = self._clock() + minutes * 60
            self.audit("TEMP_UNLOCK", f"{minutes}분")
            self._notify(f"{minutes}분 동안 차단을 일시 해제했습니다.")

    # ------------------------------------------------------------------ 자동 차단 / 스케줄
    def _schedule_auto_block(self) -> None:
        """자동 차단: 지연 대기 → (옵션) StarPlayer 준비 확인 → 차단 시작."""
        now = self._clock()
        delay = self.cfg.block_start_delay_seconds
        self._auto_block_started_at = now
        self._auto_block_waiting = True
        self._auto_block_logged_wait = False
        self._auto_block_next_try = now + delay
        if delay > 0:
            self._log("GUARD", "자동 차단", f"{delay}초 후 차단 시작 예정"
                      + (" (StarPlayer 준비 확인)" if self.cfg.wait_for_starplayer else ""))

    def _try_auto_block(self) -> None:
        if self.blocking or not self._auto_block_waiting:
            return
        if self.cfg.wait_for_starplayer and not self._starplayer_check():
            elapsed = self._clock() - self._auto_block_started_at
            deadline = self.cfg.block_start_delay_seconds + self.cfg.starplayer_wait_timeout_seconds
            if elapsed < deadline:
                if not self._auto_block_logged_wait:
                    self._auto_block_logged_wait = True
                    self._log("GUARD", "StarPlayer", "미기동 감지 → 차단 지연 (준비되면 자동 시작)")
                    self._notify("StarPlayer 기동을 기다리는 중입니다.")
                self._auto_block_next_try = self._clock() + STARPLAYER_RETRY_SECONDS
                return
            self._log("GUARD", "StarPlayer", "대기 시간 초과 → 차단을 강제 시작합니다")
        self._auto_block_waiting = False
        self._start_blocking_quietly()

    @staticmethod
    def _parse_minutes(value: str) -> int:
        hh, mm = value.split(":", 1)
        return int(hh) * 60 + int(mm)

    def _within_schedule(self) -> bool:
        if not self.cfg.schedule_enabled:
            return True
        start = self._parse_minutes(self.cfg.schedule_start)
        end = self._parse_minutes(self.cfg.schedule_end)
        now_dt = datetime.fromtimestamp(self._clock())
        now = now_dt.hour * 60 + now_dt.minute
        if start == end:
            return True  # 같은 시간이면 하루 종일 적용
        if start < end:
            return start <= now < end
        return now >= start or now < end  # 자정을 넘기는 시간대

    # ------------------------------------------------------------------ 시스템 프록시 정리
    def clear_stale_proxy_if_needed(self) -> None:
        """차단 중이 아닐 때 이전 세션의 EduGuard 시스템 프록시를 복원한다."""
        with self._lock:
            if self.blocking:
                return
            try:
                if not self.sysproxy.has_stale_proxy():
                    return
                self.sysproxy.disable()
                self._log("GUARD", "시스템 프록시", "이전 실행 잔여 설정 감지 → 원래대로 복원")
                self.audit("STALE_PROXY", "잔여 시스템 프록시 복원")
                self._notify("이전 실행에 남은 프록시 설정을 복원했습니다.")
                # 프록시가 켜진 채 부팅되면 StarPlayer 는 이미 연결 실패 상태다. 프로세스가 있는 것만으로는
                # 복구되지 않으므로, 프록시를 지운 뒤에 같은 실행 파일을 다시 띄운다.
                restarted = starplayer_ready.restart_if_running()
                if restarted:
                    self._log("GUARD", "StarPlayer", f"잔여 프록시로 기동한 플레이어 {restarted}개를 다시 시작")
                    self._notify("StarPlayer 를 프록시가 없는 상태에서 다시 시작했습니다.")
            except Exception as e:
                self._log("ERROR", "시스템 프록시", f"잔여 설정 복원 실패: {e}")

    def restore_for_session_end(self) -> None:
        """종료/재시작/로그오프 직전: 시스템 프록시를 남겨 두지 않는다."""
        if self._session_restored:
            return
        self._session_restored = True
        try:
            self.guard.stop()
        except Exception:
            pass
        try:
            self.sysproxy.disable()
            self.blocking = False
        except Exception:
            pass

    def proxy_status(self) -> str:
        with self._lock:
            return self.sysproxy.status_report(
                app_blocking=self.blocking,
                expected_port=self.cfg.port if self.blocking else None,
            )

    # ------------------------------------------------------------------ 키워드
    def _keyword_change(self, password: str, purpose: str, action: str, detail: str, apply) -> None:
        self.verify(password, purpose)
        try:
            apply()
        except ConfigError as e:
            raise EngineError(str(e))
        self.audit(action, detail)
        with self._lock:
            self._refresh_keywords()

    def add_keyword(self, password: str, keyword: str) -> None:
        raw = (keyword or "").strip()
        if not raw:
            raise EngineError("키워드를 입력하세요.")
        self._keyword_change(password, f"키워드 '{raw}' 를 추가합니다.", "KEYWORD_ADD", raw,
                             lambda: self.cfg.add_keyword(raw))

    def remove_keywords(self, password: str, keywords: Iterable[str]) -> None:
        selected = [str(k) for k in keywords or []]
        if not selected:
            raise EngineError("삭제할 키워드를 선택하세요.")

        def apply() -> None:
            for kw in selected:
                self.cfg.remove_keyword(kw)

        self._keyword_change(password, f"키워드 {', '.join(selected)} 를 삭제합니다.", "KEYWORD_REMOVE",
                             ", ".join(selected), apply)

    def reset_keywords(self, password: str) -> None:
        self._keyword_change(password, "키워드를 기본값으로 되돌립니다.", "KEYWORD_RESET", "기본값 복원",
                             self.cfg.reset_keywords)

    def import_keywords(self, password: str, keywords: Iterable[str], source: str = "") -> None:
        values = list(keywords or [])
        self._keyword_change(password, "허용 키워드 목록을 가져옵니다. 기존 목록을 교체합니다.", "KEYWORD_IMPORT",
                             source or f"{len(values)}개", lambda: self.cfg.set_keywords(values))

    def test_domain(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise EngineError("검사할 도메인을 입력하세요.")
        host = text
        if "://" in text:
            host = urlsplit(text).hostname or text
        host = host.split("/")[0].split(":")[0]
        allowed, why = self.proxy.filter.check(host)
        return {"host": host, "allowed": allowed, "reason": why}

    # ------------------------------------------------------------------ 환경설정
    def get_settings(self, password: str) -> dict:
        self.verify(password, "환경설정을 엽니다.")
        values = {key: getattr(self.cfg, key) for key in SETTING_KEYS}
        values["run_at_startup"] = self._startup.is_registered()
        return {"settings": values, "admin": self.admin}

    def save_settings(self, password: str, values: dict) -> dict:
        self.verify(password, "환경설정을 저장합니다.")
        values = {k: v for k, v in (values or {}).items() if k in SETTING_KEYS}
        registered = self._startup.is_registered()
        want_startup = bool(values.get("run_at_startup", registered))
        if self.admin:
            ok, msg = self._startup.register_cleanup()
            if not ok:
                raise EngineError(f"잔여 프록시 정리 작업을 등록하지 못했습니다.\n{msg}")
            if want_startup != registered:
                ok, msg = self._startup.register() if want_startup else self._startup.unregister()
                if not ok:
                    raise EngineError(f"자동 실행 설정을 변경하지 못했습니다.\n{msg}")
        elif want_startup != registered:
            raise EngineError("자동 실행은 관리자 권한으로 실행 중일 때만 변경할 수 있습니다.")
        values["run_at_startup"] = want_startup
        try:
            self.cfg.set_settings(**values)
        except ConfigError as e:
            raise EngineError(str(e))
        with self._lock:
            self.guard.interval = self.cfg.guard_interval
            self.proxy.set_block_message(self.cfg.block_message)
            self._refresh_keywords()
            port_pending = self.blocking and self.cfg.port != self.proxy.port
        if self.cfg.watchdog_enabled:
            self._start_watchdog_if_needed()
        else:
            self._write_normal_exit_flag()
        self.audit("SETTINGS", "환경설정 저장")
        message = "환경설정을 저장했습니다."
        if port_pending:
            message += " 포트 변경은 차단을 해제했다가 다시 시작하면 적용됩니다."
        return {"message": message}

    # ------------------------------------------------------------------ 열기 / 종료
    def open_target(self, target: str) -> None:
        try:
            if target == "logs":
                os.makedirs(self.log_dir, exist_ok=True)
                os.startfile(self.log_dir)  # type: ignore[attr-defined]
            elif target == "audit":
                os.makedirs(self.log_dir, exist_ok=True)
                if not os.path.exists(self.audit_log_path):
                    with open(self.audit_log_path, "a", encoding="utf-8"):
                        pass
                os.startfile(self.audit_log_path)  # type: ignore[attr-defined]
            elif target == "services":
                os.startfile("services.msc")  # type: ignore[attr-defined]
            else:
                raise EngineError("알 수 없는 대상입니다.")
        except OSError as e:
            raise EngineError(f"열 수 없습니다.\n{e}")

    def request_exit(self, password: str) -> None:
        self.verify(password, "프로그램을 종료합니다. (종료 시 차단도 해제됩니다)")
        self.shutdown()

    def shutdown(self) -> None:
        """정상 종료: 워치독 재실행을 막고 시스템 프록시를 원복한다."""
        self._write_normal_exit_flag()
        with self._lock:
            self._auto_block_waiting = False
            self.temp_unlock_until = 0.0
            self._stop_blocking_locked(silent=True)
        self.exit_requested.set()

    # ------------------------------------------------------------------ 보조 작업
    def _ensure_startup_task(self) -> None:
        """로그온 잔여 프록시 정리 작업은 항상 갱신하고, 자동 실행이 켜져 있으면 본 작업도 갱신."""
        if not self.admin:
            return
        try:
            self._startup.register_cleanup()
            if self.cfg.run_at_startup:
                self._startup.register()
        except Exception as e:
            self._log("ERROR", "자동 실행", str(e))

    def _check_updates(self) -> None:
        if not update_checker.manifest_url():
            return
        try:
            info = update_checker.check_for_update()
        except Exception as e:
            self._log("ERROR", "업데이트", str(e))
            return
        if info:
            self.update_info = {"version": info.version, "url": info.url, "notes": info.notes}
            self._notify(f"새 버전 {info.version} 을 사용할 수 있습니다.")

    def _start_watchdog_if_needed(self) -> None:
        if self._watchdog_proc is not None and self._watchdog_proc.poll() is not None:
            self._watchdog_proc = None
        if not self.cfg.watchdog_enabled or self._watchdog_proc is not None:
            return
        try:
            if os.path.exists(self.watchdog_flag_path):
                os.remove(self.watchdog_flag_path)
        except OSError:
            pass
        exe, args = self._startup.launch_command()
        if getattr(sys, "frozen", False):
            wd_cmd = [sys.executable, "--watchdog-child"]
        else:
            wd_cmd = [sys.executable, os.path.join(self.base_dir, "watchdog.py")]
        cmd = wd_cmd + [str(os.getpid()), self.watchdog_flag_path, self.base_dir, exe] + args
        try:
            self._watchdog_proc = subprocess.Popen(
                cmd, cwd=self.base_dir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.audit("WATCHDOG", "감시 프로세스 시작")
        except OSError as e:
            self._log("ERROR", "워치독", str(e))

    def _write_normal_exit_flag(self) -> None:
        try:
            with open(self.watchdog_flag_path, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
