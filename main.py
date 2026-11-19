"""EduGuard - 학습 사이트 전용 인터넷 제어 프로그램 (Windows) - 엔진(코어) 진입점.

UI 는 Electron(ui/) 이 담당하고, 이 프로세스는 프록시·시스템 프록시·설정을 관리하는 엔진과
로컬 API(127.0.0.1, 토큰 인증)를 실행한다.

실행:   python main.py                엔진 실행 + Electron UI 열기 (자동 실행/워치독도 이 형태로 실행)
옵션:   --backend               UI 없이 엔진만 실행 (Electron 이 엔진을 띄울 때 사용)
        --no-admin              관리자 권한 자동 상승을 건너뜀 (개발/테스트용, 현재 사용자에게만 적용)
        --restore               비밀번호 확인 후 시스템 프록시를 원래대로 복원 (비상용, 콘솔)
        --clear-stale-proxy     잔여 EduGuard 시스템 프록시만 복원 (로그온 정리 작업용)
"""
from __future__ import annotations

import ctypes
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys

import startup
import watchdog
from api_server import ApiServer
from config_store import ConfigStore
from engine import Engine
from session_guard import SessionEndGuard
from system_proxy import SystemProxy, is_admin
from version import APP_NAME

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
BACKUP_PATH = os.path.join(BASE_DIR, "proxy_backup.json")
RUNTIME_PATH = os.path.join(BASE_DIR, "runtime.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
WATCHDOG_FLAG_PATH = watchdog.default_flag_path(BASE_DIR)
UI_EXE_NAME = "EduGuard.exe"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ------------------------------------------------------------------ 관리자 권한 / 단일 실행
def relaunch_as_admin() -> bool:
    """UAC 를 띄워 관리자 권한으로 자신을 다시 실행. 요청에 성공하면 True."""
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, sys.argv[1:]
    else:
        exe, args = sys.executable, [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(args), BASE_DIR, 1)
    return rc > 32


_mutex_handle = None


def acquire_single_instance() -> bool:
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\EduGuardFilterMutex")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def message_box(text: str, flags: int = 0x40) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)


# ------------------------------------------------------------------ 런타임 정보 (Electron 이 엔진을 찾는 용도)
def write_runtime(port: int, token: str) -> None:
    tmp = RUNTIME_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, f)
    os.replace(tmp, RUNTIME_PATH)
    if is_admin():
        # 토큰이 담긴 파일이므로 표준 사용자 계정은 읽지 못하게 한다. (UI 는 관리자 권한으로 실행됨)
        try:
            subprocess.run(
                ["icacls", RUNTIME_PATH, "/inheritance:r", "/grant:r", "*S-1-5-18:(F)", "*S-1-5-32-544:(F)"],
                capture_output=True, timeout=15, creationflags=_NO_WINDOW,
            )
        except Exception:
            pass


def remove_runtime() -> None:
    try:
        os.remove(RUNTIME_PATH)
    except OSError:
        pass


# ------------------------------------------------------------------ UI 실행
def ui_command(hidden: bool) -> list[str] | None:
    args = ["--attach"] + (["--hidden"] if hidden else [])
    if getattr(sys, "frozen", False):
        exe = os.path.join(BASE_DIR, UI_EXE_NAME)
        return [exe] + args if os.path.exists(exe) else None
    electron = os.path.join(BASE_DIR, "ui", "node_modules", "electron", "dist", "electron.exe")
    if not os.path.exists(electron):
        return None
    return [electron, os.path.join(BASE_DIR, "ui")] + args


def launch_ui(hidden: bool) -> bool:
    cmd = ui_command(hidden)
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, cwd=os.path.dirname(cmd[0]), creationflags=_NO_WINDOW)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ 실행 모드
def run_engine(open_ui: bool) -> None:
    if not acquire_single_instance():
        # 엔진은 이미 실행 중 → UI 만 붙인다.
        if open_ui and not launch_ui(hidden=False):
            message_box("이미 실행 중입니다.\n작업표시줄 오른쪽 트레이(^)의 EduGuard 아이콘을 눌러 주세요.")
        return

    config = ConfigStore(CONFIG_PATH, protect_file=is_admin())
    engine = Engine(config, BASE_DIR)
    token = secrets.token_urlsafe(32)
    server = ApiServer(engine, token)
    server.start()
    write_runtime(server.port, token)
    session_guard = SessionEndGuard(engine.restore_for_session_end)
    session_guard.start()
    engine.start()
    if open_ui and not launch_ui(hidden=config.start_in_tray and config.has_password()):
        engine._log("ERROR", "UI", "Electron UI 를 찾을 수 없습니다. ui 폴더에서 npm install 을 실행하세요.")
    try:
        engine.wait_for_exit()
    except KeyboardInterrupt:
        engine.shutdown()
    finally:
        remove_runtime()
        server.stop()
        session_guard.stop()
        engine.close()


def restore_cli() -> None:
    cfg = ConfigStore(CONFIG_PATH)
    if not cfg.has_password():
        print("설정된 비밀번호가 없습니다. 프로그램을 먼저 한 번 실행하세요.")
        return
    if not cfg.verify_password(getpass.getpass("부모님 비밀번호: ")):
        print("비밀번호가 틀렸습니다.")
        return
    SystemProxy(BACKUP_PATH).disable()
    print("시스템 프록시를 원래대로 복원했습니다.")
    input("Enter 키를 누르면 창이 닫힙니다...")  # UAC 로 뜬 새 콘솔이 바로 닫히지 않게


def clear_stale_proxy_cli() -> int:
    """로그온 직후 잔여 EduGuard 시스템 프록시만 복원하고 종료한다."""
    try:
        sp = SystemProxy(BACKUP_PATH)
        if sp.has_stale_proxy():
            sp.disable()
        return 0
    except Exception:
        return 1


def uninstall_cleanup() -> None:
    """MSI 제거 시 가능한 범위에서 런타임 잔여물과 시스템 설정을 정리한다."""
    try:
        SystemProxy(BACKUP_PATH).disable()
    except Exception:
        pass
    try:
        startup.unregister_all()
    except Exception:
        pass
    try:
        with open(WATCHDOG_FLAG_PATH, "w", encoding="utf-8") as f:
            f.write("uninstall")
    except OSError:
        pass
    for path in (CONFIG_PATH, CONFIG_PATH + ".bak", BACKUP_PATH, WATCHDOG_FLAG_PATH, RUNTIME_PATH):
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        shutil.rmtree(LOG_DIR)
    except OSError:
        pass


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--watchdog-child":
        raise SystemExit(watchdog.main([sys.argv[0]] + sys.argv[2:]))

    if sys.platform != "win32":
        print("이 프로그램은 Windows 전용입니다.")
        sys.exit(1)

    args = set(sys.argv[1:])
    if "--uninstall-cleanup" in args:
        uninstall_cleanup()
        return

    if "--no-admin" not in args and not is_admin():
        if relaunch_as_admin():
            sys.exit(0)
        message_box("관리자 권한이 필요합니다. UAC 창에서 '예'를 선택해 주세요.", 0x10)
        sys.exit(1)

    if "--clear-stale-proxy" in args:
        raise SystemExit(clear_stale_proxy_cli())

    if "--restore" in args:
        restore_cli()
        return

    run_engine(open_ui="--backend" not in args)


if __name__ == "__main__":
    main()
