"""config.json 저장/로드 모듈.

- 비밀번호: PBKDF2-HMAC-SHA256 (솔트 + 20만 회) 해시로만 저장 (평문 저장 안 함)
- 무결성: 설정 내용 전체에 HMAC-SHA256 서명을 붙여, 수동 편집(변조)을 감지
- 변조 감지 시: 마지막 정상본(config.json.bak)으로 복구, 없으면 기본값 + 비밀번호 재설정 요구
- Windows 관리자 권한 실행 시 icacls 로 'Users' 그룹은 읽기 전용으로 잠금 (자녀 계정이 수정/삭제 불가)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from typing import Iterable, List, Optional

DEFAULT_KEYWORDS = [
    "ebs",
    "sevenedu",
    "megastudy",
    "etoos",
    "mimacstudy",
    "kollus",
    "cloudfront",
]

REMOTE_SUPPORT_KEYWORDS = [
    # Chrome Remote Desktop / Google 로그인·API·CDN
    "remotedesktop.google",
    "chromoting",
    "accounts.google",
    "googleapis",
    "gstatic",
    "googleusercontent",
    "google",
    # TeamViewer
    "teamviewer",
    "dyngate",
]

DEFAULT_PORT = 8899
DEFAULT_BLOCK_MESSAGE = "허용된 학습 사이트가 아닙니다."
SETTING_KEYS = (
    "port", "auto_start", "run_at_startup", "start_in_tray", "close_to_tray",
    "schedule_enabled", "schedule_start", "schedule_end", "temp_unlock_minutes",
    "watchdog_enabled", "log_to_file", "log_retention_days", "block_message",
    "guard_interval", "tray_notifications", "password_max_fails", "password_lockout_seconds",
    "audit_enabled", "remote_support_enabled",
)
PBKDF2_ITERATIONS = 200_000
MIN_KEYWORD_LEN = 3
MIN_PASSWORD_LEN = 4

# 너무 광범위해서 사실상 모든 사이트를 허용하게 되는 키워드는 등록 금지
FORBIDDEN_KEYWORDS = {
    "com", "net", "org", "www", "http", "https", "html", "web", "kr", "co.kr",
    "go.kr", "or.kr", "app", "cdn", "api", "www.", ".com", ".net", ".kr",
}

_KEYWORD_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_APP_SECRET = b"EduGuard-config-integrity-v1"


class ConfigError(Exception):
    pass


def normalize_keyword(raw: str) -> str:
    """키워드를 정규화하고, 사용할 수 없으면 ConfigError 를 발생시킨다."""
    kw = (raw or "").strip().lower()
    if not kw:
        raise ConfigError("키워드가 비어 있습니다.")
    if not _KEYWORD_RE.match(kw):
        raise ConfigError("키워드는 영문 소문자/숫자/'.'/'-' 만 사용할 수 있습니다.")
    if len(kw) < MIN_KEYWORD_LEN:
        raise ConfigError(f"키워드는 최소 {MIN_KEYWORD_LEN}자 이상이어야 합니다.")
    if kw in FORBIDDEN_KEYWORDS:
        raise ConfigError(f"'{kw}' 는 너무 광범위한 키워드라 등록할 수 없습니다.")
    return kw


def normalize_time(raw: str) -> str:
    value = (raw or "").strip()
    if not _TIME_RE.match(value):
        raise ConfigError("시간은 HH:MM 형식이어야 합니다. 예: 16:00")
    return value


def normalize_keywords(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values:
        kw = normalize_keyword(str(raw))
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out


def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


class ConfigStore:
    """스레드 안전한 설정 저장소."""

    def __init__(self, path: str, protect_file: bool = False):
        self.path = os.path.abspath(path)
        self.bak_path = self.path + ".bak"
        self.protect_file = protect_file
        self._lock = threading.RLock()
        self.tampered = False  # 로드 시 변조/손상이 감지되었는지
        self._data: dict = {}
        self._load()

    # ------------------------------------------------------------------ 로드/저장
    @staticmethod
    def _default_data() -> dict:
        return {
            "keywords": list(DEFAULT_KEYWORDS),
            "port": DEFAULT_PORT,
            "auto_start": True,         # 프로그램 실행 시 자동으로 차단 시작
            "run_at_startup": False,    # Windows 시작(로그온) 시 자동 실행
            "start_in_tray": False,     # 실행할 때 창을 띄우지 않고 트레이 아이콘으로만 시작
            "close_to_tray": True,      # 창 닫기(X) 시 종료하지 않고 트레이로 숨김
            "schedule_enabled": False,  # 지정 시간대에만 자동 차단
            "schedule_start": "16:00",
            "schedule_end": "22:00",
            "temp_unlock_minutes": 30,  # 일시 해제 기본 시간
            "watchdog_enabled": False,  # 강제 종료 시 자동 재실행
            "log_to_file": False,
            "log_retention_days": 30,
            "block_message": DEFAULT_BLOCK_MESSAGE,
            "guard_interval": 2.0,
            "tray_notifications": True,
            "password_max_fails": 5,
            "password_lockout_seconds": 30,
            "audit_enabled": True,
            "remote_support_enabled": False,
            "pw_salt": "",
            "pw_hash": "",
            "pw_iter": PBKDF2_ITERATIONS,
        }

    @staticmethod
    def _mac(payload: dict) -> str:
        pw_hash = payload.get("pw_hash", "")
        key = hashlib.sha256(_APP_SECRET + pw_hash.encode("ascii", "ignore")).digest()
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hmac.new(key, body, hashlib.sha256).hexdigest()

    def _try_read(self, path: str) -> Optional[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            payload = doc["payload"]
            if not hmac.compare_digest(doc["mac"], self._mac(payload)):
                return None
            merged = self._default_data()
            merged.update(payload)
            if not isinstance(merged["keywords"], list):
                return None
            return merged
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path) and not os.path.exists(self.bak_path):
                self._data = self._default_data()  # 최초 실행
                return
            data = self._try_read(self.path)
            if data is None:
                data = self._try_read(self.bak_path)
                if data is not None:
                    self.tampered = True
                    self._data = data
                    self._save()  # 정상본으로 되돌림
                    return
                # 변조/손상 + 백업 없음 → 기본값, 비밀번호 재설정 필요
                self.tampered = True
                self._data = self._default_data()
                return
            self._data = data

    def _save(self) -> None:
        with self._lock:
            payload = dict(self._data)
            doc = {"payload": payload, "mac": self._mac(payload)}
            tmp = self.path + ".tmp"
            if os.path.exists(self.path) and self._try_read(self.path) is not None:
                try:
                    self._write_replace(self.path, self.bak_path, copy=True)
                except OSError:
                    pass
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._lock_acl(self.path)
            self._lock_acl(self.bak_path)

    @staticmethod
    def _write_replace(src: str, dst: str, copy: bool) -> None:
        with open(src, "rb") as f:
            blob = f.read()
        tmp = dst + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, dst)

    def _lock_acl(self, path: str) -> None:
        """관리자/SYSTEM 만 쓰기, 일반 사용자는 읽기 전용으로 (best-effort)."""
        if not self.protect_file or sys.platform != "win32" or not os.path.exists(path):
            return
        try:
            subprocess.run(
                [
                    "icacls", path, "/inheritance:r",
                    "/grant:r", "*S-1-5-18:(F)", "*S-1-5-32-544:(F)", "*S-1-5-32-545:(R)",
                ],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ 비밀번호
    def has_password(self) -> bool:
        with self._lock:
            return bool(self._data.get("pw_hash"))

    def set_password(self, new_password: str) -> None:
        if len(new_password) < MIN_PASSWORD_LEN:
            raise ConfigError(f"비밀번호는 최소 {MIN_PASSWORD_LEN}자 이상이어야 합니다.")
        with self._lock:
            salt = secrets.token_bytes(16)
            self._data["pw_salt"] = _b64(salt)
            self._data["pw_iter"] = PBKDF2_ITERATIONS
            self._data["pw_hash"] = _b64(_hash_password(new_password, salt, PBKDF2_ITERATIONS))
            self.tampered = False
            self._save()

    def verify_password(self, password: str) -> bool:
        with self._lock:
            if not self.has_password():
                return False
            try:
                salt = _unb64(self._data["pw_salt"])
                expected = _unb64(self._data["pw_hash"])
                actual = _hash_password(password, salt, int(self._data["pw_iter"]))
            except Exception:
                return False
            return hmac.compare_digest(expected, actual)

    # ------------------------------------------------------------------ 키워드
    @property
    def keywords(self) -> List[str]:
        with self._lock:
            return list(self._data["keywords"])

    def add_keyword(self, raw: str) -> str:
        kw = normalize_keyword(raw)
        with self._lock:
            if kw in self._data["keywords"]:
                raise ConfigError(f"'{kw}' 는 이미 등록되어 있습니다.")
            self._data["keywords"].append(kw)
            self._save()
        return kw

    def set_keywords(self, values: Iterable[str]) -> None:
        keywords = normalize_keywords(values)
        if not keywords:
            raise ConfigError("허용 키워드는 최소 1개 이상 필요합니다.")
        with self._lock:
            self._data["keywords"] = keywords
            self._save()

    def remove_keyword(self, kw: str) -> None:
        with self._lock:
            if kw in self._data["keywords"]:
                self._data["keywords"].remove(kw)
                self._save()

    def reset_keywords(self) -> None:
        with self._lock:
            self._data["keywords"] = list(DEFAULT_KEYWORDS)
            self._save()

    # ------------------------------------------------------------------ 기타 설정
    @property
    def port(self) -> int:
        with self._lock:
            try:
                p = int(self._data.get("port", DEFAULT_PORT))
            except (TypeError, ValueError):
                p = DEFAULT_PORT
            return p if 1024 <= p <= 65535 else DEFAULT_PORT

    @property
    def auto_start(self) -> bool:
        """프로그램을 실행하면 자동으로 차단을 시작할지."""
        with self._lock:
            return bool(self._data.get("auto_start", True))

    @property
    def run_at_startup(self) -> bool:
        with self._lock:
            return bool(self._data.get("run_at_startup", False))

    @property
    def start_in_tray(self) -> bool:
        with self._lock:
            return bool(self._data.get("start_in_tray", False))

    @property
    def close_to_tray(self) -> bool:
        with self._lock:
            return bool(self._data.get("close_to_tray", True))

    @property
    def schedule_enabled(self) -> bool:
        with self._lock:
            return bool(self._data.get("schedule_enabled", False))

    @property
    def schedule_start(self) -> str:
        with self._lock:
            try:
                return normalize_time(str(self._data.get("schedule_start", "16:00")))
            except ConfigError:
                return "16:00"

    @property
    def schedule_end(self) -> str:
        with self._lock:
            try:
                return normalize_time(str(self._data.get("schedule_end", "22:00")))
            except ConfigError:
                return "22:00"

    @property
    def temp_unlock_minutes(self) -> int:
        with self._lock:
            return self._bounded_int("temp_unlock_minutes", 30, 1, 480)

    @property
    def watchdog_enabled(self) -> bool:
        with self._lock:
            return bool(self._data.get("watchdog_enabled", False))

    @property
    def log_to_file(self) -> bool:
        with self._lock:
            return bool(self._data.get("log_to_file", False))

    @property
    def log_retention_days(self) -> int:
        with self._lock:
            return self._bounded_int("log_retention_days", 30, 1, 365)

    @property
    def block_message(self) -> str:
        with self._lock:
            msg = str(self._data.get("block_message", DEFAULT_BLOCK_MESSAGE)).strip()
            return msg[:200] or DEFAULT_BLOCK_MESSAGE

    @property
    def guard_interval(self) -> float:
        with self._lock:
            try:
                v = float(self._data.get("guard_interval", 2.0))
            except (TypeError, ValueError):
                v = 2.0
            return v if 0.5 <= v <= 30.0 else 2.0

    @property
    def tray_notifications(self) -> bool:
        with self._lock:
            return bool(self._data.get("tray_notifications", True))

    @property
    def password_max_fails(self) -> int:
        with self._lock:
            return self._bounded_int("password_max_fails", 5, 1, 20)

    @property
    def password_lockout_seconds(self) -> int:
        with self._lock:
            return self._bounded_int("password_lockout_seconds", 30, 5, 3600)

    @property
    def audit_enabled(self) -> bool:
        with self._lock:
            return bool(self._data.get("audit_enabled", True))

    @property
    def remote_support_enabled(self) -> bool:
        with self._lock:
            return bool(self._data.get("remote_support_enabled", False))

    def _bounded_int(self, key: str, default: int, min_value: int, max_value: int) -> int:
        try:
            v = int(self._data.get(key, default))
        except (TypeError, ValueError):
            v = default
        return v if min_value <= v <= max_value else default

    def set_settings(self, **changes) -> None:
        """환경설정 값을 한 번에 검증 후 저장한다. 잘못된 값이 있으면 아무것도 저장하지 않는다."""
        unknown = set(changes) - set(SETTING_KEYS)
        if unknown:
            raise ConfigError(f"알 수 없는 설정: {', '.join(sorted(unknown))}")
        clean: dict = {}
        for key, value in changes.items():
            if key == "port":
                try:
                    port = int(value)
                except (TypeError, ValueError):
                    raise ConfigError("포트는 숫자여야 합니다.")
                if not 1024 <= port <= 65535:
                    raise ConfigError("포트는 1024 ~ 65535 사이여야 합니다.")
                clean[key] = port
            elif key in ("schedule_start", "schedule_end"):
                clean[key] = normalize_time(str(value))
            elif key in ("temp_unlock_minutes", "log_retention_days", "password_max_fails",
                         "password_lockout_seconds"):
                ranges = {
                    "temp_unlock_minutes": (1, 480, "일시 해제 시간은 1 ~ 480분 사이여야 합니다."),
                    "log_retention_days": (1, 365, "로그 보관 기간은 1 ~ 365일 사이여야 합니다."),
                    "password_max_fails": (1, 20, "비밀번호 실패 허용 횟수는 1 ~ 20회 사이여야 합니다."),
                    "password_lockout_seconds": (5, 3600, "비밀번호 잠금 시간은 5 ~ 3600초 사이여야 합니다."),
                }
                try:
                    iv = int(value)
                except (TypeError, ValueError):
                    raise ConfigError(ranges[key][2])
                if not ranges[key][0] <= iv <= ranges[key][1]:
                    raise ConfigError(ranges[key][2])
                clean[key] = iv
            elif key == "guard_interval":
                try:
                    fv = float(value)
                except (TypeError, ValueError):
                    raise ConfigError("감시 주기는 숫자여야 합니다.")
                if not 0.5 <= fv <= 30.0:
                    raise ConfigError("감시 주기는 0.5 ~ 30초 사이여야 합니다.")
                clean[key] = fv
            elif key == "block_message":
                msg = str(value).strip()
                if not msg:
                    raise ConfigError("차단 페이지 문구는 비워 둘 수 없습니다.")
                if len(msg) > 200:
                    raise ConfigError("차단 페이지 문구는 200자 이하여야 합니다.")
                clean[key] = msg
            else:
                clean[key] = bool(value)
        with self._lock:
            self._data.update(clean)
            self._save()
