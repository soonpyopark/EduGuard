"""Windows 시스템 프록시 설정 / 복원 / 감시 모듈.

- 로드된 모든 사용자 하이브(HKEY_USERS\\S-1-5-21-...)에 프록시를 적용한다.
  (자녀가 표준 계정이고 부모가 관리자 계정으로 UAC 승인한 경우에도 자녀 계정에 적용되도록)
- 변경 전 원래 설정을 proxy_backup.json 에 보관하고, 해제 시 그대로 복원한다.
  (프로그램이 비정상 종료돼도 백업이 남아 있어 나중에 복원 가능)
- ProxyGuard: 주기적으로 설정을 검사해 임의로 바뀌면 즉시 되돌린다.
- IE 정책(Control Panel\\Proxy=1)으로 '프록시 설정 변경' UI 를 잠근다. (관리자만 쓸 수 있는 Policies 키)
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
from typing import Dict, List, Optional

if sys.platform == "win32":
    import winreg
else:  # 다른 OS 에서 import 오류만 안 나게 (단위 테스트용)
    winreg = None  # type: ignore

INET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
POLICY_KEY = r"Software\Policies\Microsoft\Internet Explorer\Control Panel"
_SID_RE = re.compile(r"^S-1-(5-21|12-1)-[\d-]+$")  # 로컬/도메인/AzureAD 사용자 (_Classes 제외)
_MANAGED_VALUES = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL")

INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _loaded_user_sids() -> List[str]:
    sids: List[str] = []
    i = 0
    while True:
        try:
            name = winreg.EnumKey(winreg.HKEY_USERS, i)
        except OSError:
            break
        i += 1
        if _SID_RE.match(name):
            sids.append(name)
    return sids


def _notify_wininet() -> None:
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
        wininet.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)
    except Exception:
        pass


class SystemProxy:
    def __init__(self, backup_path: str):
        self.backup_path = os.path.abspath(backup_path)
        self._lock = threading.RLock()
        self._target: Optional[str] = None  # 현재 강제 중인 "127.0.0.1:port"

    # ------------------------------------------------------------ 레지스트리 헬퍼
    @staticmethod
    def _read_values(sid: str) -> Dict[str, Optional[list]]:
        out: Dict[str, Optional[list]] = {}
        try:
            with winreg.OpenKey(winreg.HKEY_USERS, f"{sid}\\{INET_KEY}", 0, winreg.KEY_READ) as k:
                for name in _MANAGED_VALUES:
                    try:
                        value, vtype = winreg.QueryValueEx(k, name)
                        out[name] = [vtype, value]
                    except FileNotFoundError:
                        out[name] = None
        except OSError:
            return {}
        return out

    @staticmethod
    def _write_value(sid: str, name: str, entry: Optional[list]) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_USERS, f"{sid}\\{INET_KEY}", 0, winreg.KEY_SET_VALUE) as k:
            if entry is None:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(k, name, 0, entry[0], entry[1])

    @staticmethod
    def _set_policy_lock(sid: str, locked: bool) -> None:
        try:
            if locked:
                with winreg.CreateKeyEx(winreg.HKEY_USERS, f"{sid}\\{POLICY_KEY}", 0,
                                        winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "Proxy", 0, winreg.REG_DWORD, 1)
            else:
                with winreg.OpenKey(winreg.HKEY_USERS, f"{sid}\\{POLICY_KEY}", 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, "Proxy")
        except OSError:
            pass  # 권한 부족 / 값 없음 → 무시 (best-effort)

    # ------------------------------------------------------------ 백업
    def _load_backup(self) -> Dict[str, dict]:
        try:
            with open(self.backup_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_backup(self, data: Dict[str, dict]) -> None:
        tmp = self.backup_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.backup_path)

    def has_backup(self) -> bool:
        return bool(self._load_backup())

    def _backup_missing(self, target: str) -> None:
        backup = self._load_backup()
        changed = False
        for sid in _loaded_user_sids():
            if sid in backup:
                continue
            cur = self._read_values(sid)
            if not cur:
                continue
            server = cur.get("ProxyServer")
            if server and server[1] == target:  # 이미 우리 설정 → 원본이 아니므로 백업하지 않음
                continue
            backup[sid] = cur
            changed = True
        if changed:
            self._save_backup(backup)

    # ------------------------------------------------------------ 공개 API
    def _apply_to(self, sid: str, target: str) -> None:
        self._write_value(sid, "ProxyServer", [winreg.REG_SZ, target])
        self._write_value(sid, "ProxyOverride", [winreg.REG_SZ, ""])  # 예외 없음: 전부 프록시 경유
        self._write_value(sid, "AutoConfigURL", None)  # PAC 스크립트로 우회 불가
        self._write_value(sid, "ProxyEnable", [winreg.REG_DWORD, 1])
        self._set_policy_lock(sid, True)

    def enable(self, port: int, host: str = "127.0.0.1") -> None:
        target = f"{host}:{port}"
        with self._lock:
            self._backup_missing(target)
            self._target = target
            for sid in _loaded_user_sids():
                try:
                    self._apply_to(sid, target)
                except OSError:
                    pass
        _notify_wininet()

    def is_enforced(self) -> bool:
        if not self._target:
            return False
        sids = _loaded_user_sids()
        for sid in sids:
            cur = self._read_values(sid)
            if not cur:
                continue
            en, srv, pac = cur.get("ProxyEnable"), cur.get("ProxyServer"), cur.get("AutoConfigURL")
            if not en or en[1] != 1 or not srv or srv[1] != self._target or pac:
                return False
        return True

    def enforce(self) -> bool:
        """설정이 바뀌었으면 되돌린다. 되돌렸으면 True."""
        with self._lock:
            target = self._target
            if not target:
                return False
            fixed = False
            for sid in _loaded_user_sids():
                cur = self._read_values(sid)
                if not cur:
                    continue
                en, srv, pac = cur.get("ProxyEnable"), cur.get("ProxyServer"), cur.get("AutoConfigURL")
                if not en or en[1] != 1 or not srv or srv[1] != target or pac:
                    # 새로 로그인한 사용자의 원본 설정은 아직 백업이 없을 수 있으니 먼저 보관
                    self._backup_missing(target)
                    try:
                        self._apply_to(sid, target)
                        fixed = True
                    except OSError:
                        pass
            if fixed:
                _notify_wininet()
            return fixed

    def disable(self) -> None:
        """원래 설정으로 복원."""
        with self._lock:
            backup = self._load_backup()
            for sid in _loaded_user_sids():
                self._set_policy_lock(sid, False)
                try:
                    if sid in backup:
                        for name in _MANAGED_VALUES:
                            self._write_value(sid, name, backup[sid].get(name))
                    else:
                        cur = self._read_values(sid)
                        srv = cur.get("ProxyServer") if cur else None
                        if srv and str(srv[1]).startswith("127.0.0.1:"):
                            self._write_value(sid, "ProxyEnable", [winreg.REG_DWORD, 0])
                            self._write_value(sid, "ProxyServer", None)
                except OSError:
                    pass
            self._target = None
            try:
                os.remove(self.backup_path)
            except OSError:
                pass
        _notify_wininet()


class ProxyGuard:
    """주기적으로 시스템 프록시 설정을 감시/복구하는 백그라운드 스레드."""

    def __init__(self, sysproxy: SystemProxy, interval: float = 2.0, on_fix=None):
        self.sysproxy = sysproxy
        self.interval = interval
        self.on_fix = on_fix
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="proxy-guard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if self.sysproxy.enforce() and self.on_fix:
                    self.on_fix()
            except Exception:
                pass
