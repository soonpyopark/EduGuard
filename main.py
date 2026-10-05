"""EduGuard - 학습 사이트 전용 인터넷 제어 프로그램 (Windows).

실행:   python main.py
옵션:   --no-admin   관리자 권한 자동 상승을 건너뜀 (개발/테스트용, 현재 사용자에게만 적용)
        --restore    GUI 없이 비밀번호 확인 후 시스템 프록시를 원래대로 복원 (비상용)
"""
from __future__ import annotations

import ctypes
import json
import getpass
import glob
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import time
import webbrowser
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import startup
import update_checker
from config_store import ConfigError, ConfigStore, MIN_PASSWORD_LEN, REMOTE_SUPPORT_KEYWORDS
from proxy_server import FilterProxy
from system_proxy import ProxyGuard, SystemProxy, is_admin
from tray import TrayIcon
import watchdog
from version import APP_NAME, APP_VERSION

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
BACKUP_PATH = os.path.join(BASE_DIR, "proxy_backup.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
AUDIT_LOG_PATH = os.path.join(LOG_DIR, "settings-history.log")
WATCHDOG_FLAG_PATH = watchdog.default_flag_path(BASE_DIR)

APP_ID = "EduGuard.FocusedLearning.1"  # 작업표시줄에서 python.exe 가 아닌 EduGuard 로 묶이도록
ICON_ICO = "eduguard.ico"
ICON_PNG = "eduguard.png"

MAX_FAILS = 5
LOCKOUT_SECONDS = 30
MAX_LOG_LINES = 500


# ------------------------------------------------------------------ 관리자 권한 / 단일 실행
def relaunch_as_admin() -> bool:
    """UAC 를 띄워 관리자 권한으로 자신을 다시 실행. 요청에 성공하면 True."""
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, sys.argv[1:]
    else:
        exe, args = sys.executable, [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(args), BASE_DIR, 1)
    return rc > 32


def resource_path(*parts: str) -> str:
    """assets 폴더의 리소스 경로 (PyInstaller 로 묶었을 때는 임시 해제 폴더에서 찾음)."""
    base = getattr(sys, "_MEIPASS", BASE_DIR)
    return os.path.join(base, "assets", *parts)


_mutex_handle = None


def acquire_single_instance() -> bool:
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\EduGuardFilterMutex")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ------------------------------------------------------------------ GUI
class App(tk.Tk):
    def __init__(self, config: ConfigStore):
        super().__init__()
        self.cfg = config
        self.title(f"{APP_NAME} {APP_VERSION} - 학습 사이트 전용 모드")
        self.geometry("720x720")
        self.minsize(640, 620)
        self._set_icon()

        self.events: "queue.Queue[dict]" = queue.Queue()
        self.proxy = FilterProxy(
            self._effective_keywords(), port=self.cfg.port, on_event=self.events.put,
            block_message=self.cfg.block_message,
        )
        self.sysproxy = SystemProxy(BACKUP_PATH)
        self.guard = ProxyGuard(self.sysproxy, on_fix=lambda: self.events.put(
            {"time": time.strftime("%H:%M:%S"), "action": "GUARD", "host": "시스템 프록시",
             "detail": "임의 변경 감지 → 복구"}), interval=self.cfg.guard_interval)
        self.blocking = False
        self.temp_unlock_until: float = 0.0
        self._watchdog_proc: subprocess.Popen | None = None
        self._last_log_prune = ""
        self._recent_log_events: dict[tuple[str, str, str], float] = {}
        self._log_follow_tail = True
        self._fails = 0
        self._locked_until = 0.0

        # 트레이 아이콘: 명령(열기/종료)은 트레이 스레드에서 오므로 큐로 UI 스레드에 넘긴다
        self.tray_cmds: "queue.Queue[str]" = queue.Queue()
        self.update_results: "queue.Queue[object]" = queue.Queue()
        self.tray = TrayIcon(resource_path(ICON_ICO), APP_NAME, self._status_text, self.tray_cmds.put)
        self._tray_hint_shown = False
        self._exit_prompt_open = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self._poll_events)
        self.tray.start()
        self._start_watchdog_if_needed()
        self._prune_logs()

        if not self.cfg.has_password():
            self._first_time_setup()
        elif self.cfg.start_in_tray and self.tray.available:
            self.withdraw()  # 창 없이 트레이 아이콘으로만 시작 (비밀번호 최초 설정 때는 창이 필요하므로 제외)
        if self.cfg.auto_start and self.cfg.has_password() and not self.cfg.schedule_enabled:
            self.after(300, self.start_blocking)
        self.after(1500, self._ensure_startup_task)
        self.after(1000, self._schedule_tick)
        self.after(2500, self._check_updates_async)

    def _status_text(self) -> str:
        if self.temp_unlock_until:
            return f"상태: 일시 해제 중 ({self._temp_unlock_remaining()}분 남음)"
        return "상태: 차단 중" if self.blocking else "상태: 차단 해제됨"

    def _effective_keywords(self) -> list[str]:
        keywords = self.cfg.keywords
        if self.cfg.remote_support_enabled:
            keywords = keywords + [kw for kw in REMOTE_SUPPORT_KEYWORDS if kw not in keywords]
        return keywords

    # -------------------------------------------------------------- 트레이 / 창 표시
    def show_window(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)  # 다른 창 뒤에 숨어 뜨는 것을 방지
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def hide_to_tray(self) -> None:
        self.withdraw()
        if self.cfg.tray_notifications and not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify(APP_NAME, "트레이에서 계속 실행 중입니다. 아이콘을 눌러 다시 열 수 있습니다.")

    def on_close(self) -> None:
        """창 닫기(X): 설정에 따라 트레이로 숨기거나 (비밀번호 확인 후) 종료."""
        if self.cfg.close_to_tray and self.tray.available:
            self.hide_to_tray()
        else:
            self.on_exit()

    def _handle_tray_command(self, cmd: str) -> None:
        self.show_window()  # 숨겨진 창은 비밀번호 대화상자의 부모가 될 수 없으므로 먼저 표시
        if cmd == "exit":
            self.after(50, self.on_exit)

    def _ensure_startup_task(self) -> None:
        """'Windows 시작 시 자동 실행'이 켜져 있으면 작업 스케줄러 등록을 최신 경로로 갱신 (이동/삭제 복구)."""
        if self.cfg.run_at_startup and is_admin():
            threading.Thread(target=startup.register, daemon=True).start()

    def _check_updates_async(self) -> None:
        if not update_checker.manifest_url():
            return

        def worker() -> None:
            try:
                info = update_checker.check_for_update()
            except Exception as e:
                self.update_results.put(e)
                return
            if info:
                self.update_results.put(info)

        threading.Thread(target=worker, name="update-check", daemon=True).start()

    def _handle_update_result(self, result: object) -> None:
        if isinstance(result, Exception):
            self.events.put({"time": time.strftime("%H:%M:%S"), "action": "ERROR",
                             "host": "업데이트", "detail": str(result)})
            return
        if not isinstance(result, update_checker.UpdateInfo):
            return
        msg = f"새 EduGuard 버전 {result.version} 이(가) 있습니다.\n\n현재 버전: {APP_VERSION}"
        if result.notes:
            msg += f"\n\n변경 사항:\n{result.notes}"
        msg += "\n\n다운로드 페이지를 열까요?"
        if self.cfg.tray_notifications:
            self.tray.notify(APP_NAME, f"새 버전 {result.version} 을 사용할 수 있습니다.")
        if messagebox.askyesno(APP_NAME, msg, parent=self):
            webbrowser.open(result.url)

    def _start_watchdog_if_needed(self) -> None:
        if self._watchdog_proc is not None and self._watchdog_proc.poll() is not None:
            self._watchdog_proc = None
        if not self.cfg.watchdog_enabled or self._watchdog_proc is not None:
            return
        try:
            if os.path.exists(WATCHDOG_FLAG_PATH):
                os.remove(WATCHDOG_FLAG_PATH)
        except OSError:
            pass
        exe, args = startup.launch_command()
        if getattr(sys, "frozen", False):
            wd_cmd = [sys.executable, "--watchdog-child"]
        else:
            wd_cmd = [sys.executable, os.path.join(BASE_DIR, "watchdog.py")]
        cmd = wd_cmd + [str(os.getpid()), WATCHDOG_FLAG_PATH, BASE_DIR, exe] + args
        try:
            self._watchdog_proc = subprocess.Popen(
                cmd, cwd=BASE_DIR, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.audit("WATCHDOG", "감시 프로세스 시작")
        except OSError as e:
            self.events.put({"time": time.strftime("%H:%M:%S"), "action": "ERROR",
                             "host": "워치독", "detail": str(e)})

    def _write_normal_exit_flag(self) -> None:
        try:
            with open(WATCHDOG_FLAG_PATH, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass

    def _minutes_now(self) -> int:
        now = datetime.now()
        return now.hour * 60 + now.minute

    @staticmethod
    def _parse_minutes(value: str) -> int:
        hh, mm = value.split(":", 1)
        return int(hh) * 60 + int(mm)

    def _within_schedule(self) -> bool:
        if not self.cfg.schedule_enabled:
            return True
        start = self._parse_minutes(self.cfg.schedule_start)
        end = self._parse_minutes(self.cfg.schedule_end)
        now = self._minutes_now()
        if start == end:
            return True  # 같은 시간이면 하루 종일 적용
        if start < end:
            return start <= now < end
        return now >= start or now < end  # 자정을 넘기는 시간대

    def _temp_unlock_remaining(self) -> int:
        remain = self.temp_unlock_until - time.time()
        return max(0, int(remain + 59) // 60)

    def _schedule_tick(self) -> None:
        try:
            if self.temp_unlock_until and time.time() >= self.temp_unlock_until:
                self.temp_unlock_until = 0.0
                self.events.put({"time": time.strftime("%H:%M:%S"), "action": "GUARD",
                                 "host": "일시 해제", "detail": "시간 만료"})
                if self.cfg.tray_notifications:
                    self.tray.notify(APP_NAME, "일시 해제가 끝나 차단을 다시 시작합니다.")
            if self.cfg.schedule_enabled and self.cfg.has_password():
                if self._within_schedule() and not self.temp_unlock_until and not self.blocking:
                    self.start_blocking(silent=True)
                elif (not self._within_schedule()) and self.blocking:
                    self.stop_blocking(ask=False, silent=True)
        finally:
            self._refresh_status()
            self.after(30_000, self._schedule_tick)

    def _set_icon(self) -> None:
        """창 / 작업표시줄 / 비밀번호 입력창 등 모든 창에 EduGuard 아이콘 적용 (실패해도 무시)."""
        try:
            self.iconbitmap(default=resource_path(ICON_ICO))  # 이후 생성되는 대화상자에도 적용
        except tk.TclError:
            pass
        try:
            self._icon_img = tk.PhotoImage(file=resource_path(ICON_PNG))  # 참조 유지 필요
            self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass

    # -------------------------------------------------------------- UI 구성
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        self.status_var = tk.StringVar()
        self.status_lbl = tk.Label(top, textvariable=self.status_var, font=("Malgun Gothic", 16, "bold"),
                                   fg="white", pady=10)
        self.status_lbl.pack(fill="x")
        self.counter_var = tk.StringVar(value="허용 0 / 차단 0")
        ttk.Label(top, textvariable=self.counter_var).pack(anchor="e")

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="▶ 차단 시작", command=self.start_blocking)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.stop_btn = ttk.Button(btns, text="■ 차단 해제 (비밀번호)", command=self.stop_blocking)
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=(5, 0))
        self.temp_unlock_btn = ttk.Button(btns, text="⏱ 일시 해제", command=self.temp_unlock)
        self.temp_unlock_btn.pack(side="left", expand=True, fill="x", padx=(5, 0))

        kf = ttk.LabelFrame(self, text="허용 키워드 (도메인에 포함되면 허용)")
        kf.pack(fill="both", expand=False, **pad)
        list_frame = ttk.Frame(kf)
        list_frame.pack(fill="both", expand=True, padx=8, pady=5)
        self.kw_list = tk.Listbox(list_frame, height=7, selectmode="extended")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.kw_list.yview)
        self.kw_list.configure(yscrollcommand=sb.set)
        self.kw_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        row = ttk.Frame(kf)
        row.pack(fill="x", padx=8, pady=(0, 8))
        self.kw_entry = ttk.Entry(row)
        self.kw_entry.pack(side="left", fill="x", expand=True)
        self.kw_entry.bind("<Return>", lambda _e: self.add_keyword())
        ttk.Button(row, text="추가 (비밀번호)", command=self.add_keyword).pack(side="left", padx=4)
        ttk.Button(row, text="선택 삭제 (비밀번호)", command=self.remove_keywords).pack(side="left")
        ttk.Button(row, text="기본값 복원", command=self.reset_keywords).pack(side="left", padx=4)
        ttk.Button(row, text="가져오기", command=self.import_keywords).pack(side="left")
        ttk.Button(row, text="내보내기", command=self.export_keywords).pack(side="left", padx=4)

        tf = ttk.LabelFrame(self, text="도메인 허용 여부 테스트 (비밀번호 불필요)")
        tf.pack(fill="x", **pad)
        trow = ttk.Frame(tf)
        trow.pack(fill="x", padx=8, pady=8)
        self.test_entry = ttk.Entry(trow)
        self.test_entry.pack(side="left", fill="x", expand=True)
        self.test_entry.bind("<Return>", lambda _e: self.test_domain())
        ttk.Button(trow, text="검사", command=self.test_domain).pack(side="left", padx=4)
        self.test_result = ttk.Label(tf, text="")
        self.test_result.pack(anchor="w", padx=8, pady=(0, 6))

        lf = ttk.LabelFrame(self, text="실시간 접속 로그")
        lf.pack(fill="both", expand=True, **pad)
        self.log = ScrolledText(lf, height=10, state="disabled", font=("Consolas", 9))
        self.log.configure(yscrollcommand=self._on_log_yview)
        self.log.vbar.configure(command=self._on_log_scroll)
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.tag_config("ALLOW", foreground="#0a7d2c")
        self.log.tag_config("BLOCK", foreground="#c0392b")
        self.log.tag_config("GUARD", foreground="#d68910")
        self.log.tag_config("ERROR", foreground="#7f8c8d")

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="비밀번호 변경", command=self.change_password).pack(side="left")
        ttk.Button(bottom, text="⚙ 환경설정 (비밀번호)", command=self.open_settings).pack(side="left", padx=6)
        ttk.Button(bottom, text="프로그램 종료 (비밀번호)", command=self.on_exit).pack(side="right")

        self._refresh_keywords()
        self._refresh_status()

    # -------------------------------------------------------------- 상태 표시
    def _refresh_status(self) -> None:
        if self.temp_unlock_until:
            self.status_var.set(f"⏱ 일시 해제 중 - {self._temp_unlock_remaining()}분 후 자동 차단")
            self.status_lbl.configure(bg="#d68910")
            self.start_btn.state(["!disabled"])
            self.stop_btn.state(["disabled"])
            self.temp_unlock_btn.state(["disabled"])
        elif self.blocking:
            suffix = " / 원격지원 허용" if self.cfg.remote_support_enabled else ""
            self.status_var.set(f"🔒 차단 중 - 허용 키워드 {len(self.cfg.keywords)}개만 접속 가능{suffix}")
            self.status_lbl.configure(bg="#c0392b")
            self.start_btn.state(["disabled"])
            self.stop_btn.state(["!disabled"])
            self.temp_unlock_btn.state(["!disabled"])
        else:
            self.status_var.set("🔓 차단 해제됨 - 모든 사이트 접속 가능")
            self.status_lbl.configure(bg="#7f8c8d")
            self.start_btn.state(["!disabled"])
            self.stop_btn.state(["disabled"])
            self.temp_unlock_btn.state(["disabled"])
        self.tray.set_tooltip(f"{APP_NAME} - {self._status_text()[4:]}")

    def _refresh_keywords(self) -> None:
        self.kw_list.delete(0, "end")
        for kw in self.cfg.keywords:
            self.kw_list.insert("end", kw)
        self.proxy.filter.set_keywords(self._effective_keywords())  # 실행 중에도 즉시 반영

    def _append_log(self, ev: dict) -> None:
        if self._should_suppress_log(ev):
            return
        follow_tail = self._log_follow_tail or self._log_is_at_bottom()
        if not follow_tail:
            self.log.mark_set("log_view_top", "@0,0")
            self.log.mark_gravity("log_view_top", "left")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ev['time']}] {ev['action']:<5} {ev['host']}  ({ev['detail']})\n",
                        ev["action"])
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        if follow_tail:
            self.log.see("end")
            self._log_follow_tail = True
        else:
            self.log.yview("log_view_top")
            self._log_follow_tail = False
        self.log.configure(state="disabled")
        self._write_access_log(ev)

    def _log_is_at_bottom(self) -> bool:
        try:
            return self.log.yview()[1] >= 0.999
        except tk.TclError:
            return True

    def _on_log_yview(self, first: str, last: str) -> None:
        self.log.vbar.set(first, last)
        try:
            self._log_follow_tail = float(last) >= 0.999
        except ValueError:
            pass

    def _on_log_scroll(self, *args) -> None:
        self.log.yview(*args)
        self.after_idle(lambda: setattr(self, "_log_follow_tail", self._log_is_at_bottom()))

    def _should_suppress_log(self, ev: dict) -> bool:
        """ALLOW/BLOCK 중복 로그만 숨긴다. 차단/허용 판단과 카운터에는 영향을 주지 않는다."""
        if not self.cfg.suppress_repeated_logs or ev.get("action") not in ("ALLOW", "BLOCK"):
            return False
        now = time.time()
        window = self.cfg.repeat_log_window_seconds
        key = (str(ev.get("action", "")), str(ev.get("host", "")), str(ev.get("detail", "")))
        last = self._recent_log_events.get(key)
        self._recent_log_events[key] = now
        cutoff = now - max(window * 2, 60)
        for old_key, ts in list(self._recent_log_events.items()):
            if ts < cutoff:
                self._recent_log_events.pop(old_key, None)
        return last is not None and now - last < window

    def _write_access_log(self, ev: dict) -> None:
        if not self.cfg.log_to_file:
            return
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            today = datetime.now().strftime("%Y%m%d")
            if self._last_log_prune != today:
                self._prune_logs()
            path = os.path.join(LOG_DIR, f"eduguard-{today}.log")
            line = f"{datetime.now().isoformat(timespec='seconds')}\t{ev['action']}\t{ev['host']}\t{ev['detail']}\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _prune_logs(self) -> None:
        self._last_log_prune = datetime.now().strftime("%Y%m%d")
        cutoff = time.time() - (self.cfg.log_retention_days * 86400)
        try:
            for path in glob.glob(os.path.join(LOG_DIR, "eduguard-*.log")):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass

    def audit(self, action: str, detail: str) -> None:
        if not self.cfg.audit_enabled:
            return
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{action}\t{detail}\n")
        except OSError:
            pass

    def _open_path(self, path: str) -> None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            if not os.path.exists(path):
                with open(path, "a", encoding="utf-8"):
                    pass
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as e:
            messagebox.showerror(APP_NAME, f"열 수 없습니다.\n{e}", parent=self)

    def _poll_events(self) -> None:
        try:
            for _ in range(200):
                self._append_log(self.events.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self._handle_tray_command(self.tray_cmds.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self._handle_update_result(self.update_results.get_nowait())
        except queue.Empty:
            pass
        self.counter_var.set(f"허용 {self.proxy.allowed_count} / 차단 {self.proxy.blocked_count}")
        self.after(200, self._poll_events)

    # -------------------------------------------------------------- 비밀번호
    def _first_time_setup(self) -> None:
        if self.cfg.tampered:
            messagebox.showwarning(APP_NAME, "설정 파일이 손상되었거나 변조되어 기본값으로 초기화합니다.\n"
                                   "새 부모님 비밀번호를 설정하세요.", parent=self)
        while True:
            pw1 = simpledialog.askstring(APP_NAME, f"부모님 비밀번호를 설정하세요 ({MIN_PASSWORD_LEN}자 이상):",
                                         show="*", parent=self)
            if pw1 is None:
                messagebox.showinfo(APP_NAME, "비밀번호 설정 없이는 사용할 수 없습니다.", parent=self)
                self.destroy()
                sys.exit(0)
            pw2 = simpledialog.askstring(APP_NAME, "비밀번호를 한 번 더 입력하세요:", show="*", parent=self)
            if pw1 != pw2:
                messagebox.showerror(APP_NAME, "비밀번호가 일치하지 않습니다.", parent=self)
                continue
            try:
                self.cfg.set_password(pw1)
                return
            except ConfigError as e:
                messagebox.showerror(APP_NAME, str(e), parent=self)

    def require_password(self, purpose: str) -> bool:
        remain = self._locked_until - time.time()
        if remain > 0:
            messagebox.showerror(APP_NAME, f"비밀번호를 여러 번 틀려 {int(remain) + 1}초 동안 잠겼습니다.",
                                 parent=self)
            return False
        pw = simpledialog.askstring(APP_NAME, f"{purpose}\n부모님 비밀번호를 입력하세요:", show="*", parent=self)
        if pw is None:
            return False
        if self.cfg.verify_password(pw):
            self._fails = 0
            self.audit("PASSWORD", purpose)
            return True
        self._fails += 1
        if self._fails >= self.cfg.password_max_fails:
            self._locked_until = time.time() + self.cfg.password_lockout_seconds
            self._fails = 0
            self.audit("PASSWORD_LOCK", f"{self.cfg.password_lockout_seconds}초 잠금")
        messagebox.showerror(APP_NAME, "비밀번호가 틀렸습니다.", parent=self)
        return False

    def change_password(self) -> None:
        if not self.require_password("비밀번호를 변경합니다."):
            return
        pw1 = simpledialog.askstring(APP_NAME, "새 비밀번호:", show="*", parent=self)
        if pw1 is None:
            return
        pw2 = simpledialog.askstring(APP_NAME, "새 비밀번호 확인:", show="*", parent=self)
        if pw1 != pw2:
            messagebox.showerror(APP_NAME, "비밀번호가 일치하지 않습니다.", parent=self)
            return
        try:
            self.cfg.set_password(pw1)
            messagebox.showinfo(APP_NAME, "비밀번호가 변경되었습니다.", parent=self)
        except ConfigError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)

    # -------------------------------------------------------------- 환경설정
    def open_settings(self) -> None:
        if not self.require_password("환경설정을 엽니다."):
            return

        tray_ok = self.tray.available
        registered = startup.is_registered()
        v_startup = tk.BooleanVar(value=registered)
        v_tray_start = tk.BooleanVar(value=self.cfg.start_in_tray)
        v_tray_close = tk.BooleanVar(value=self.cfg.close_to_tray)
        v_auto_block = tk.BooleanVar(value=self.cfg.auto_start)
        v_schedule = tk.BooleanVar(value=self.cfg.schedule_enabled)
        v_watchdog = tk.BooleanVar(value=self.cfg.watchdog_enabled)
        v_log_file = tk.BooleanVar(value=self.cfg.log_to_file)
        v_suppress_repeats = tk.BooleanVar(value=self.cfg.suppress_repeated_logs)
        v_tray_notify = tk.BooleanVar(value=self.cfg.tray_notifications)
        v_audit = tk.BooleanVar(value=self.cfg.audit_enabled)
        v_remote_support = tk.BooleanVar(value=self.cfg.remote_support_enabled)
        v_port = tk.StringVar(value=str(self.cfg.port))
        v_schedule_start = tk.StringVar(value=self.cfg.schedule_start)
        v_schedule_end = tk.StringVar(value=self.cfg.schedule_end)
        v_unlock_minutes = tk.StringVar(value=str(self.cfg.temp_unlock_minutes))
        v_retention = tk.StringVar(value=str(self.cfg.log_retention_days))
        v_repeat_window = tk.StringVar(value=str(self.cfg.repeat_log_window_seconds))
        v_block_message = tk.StringVar(value=self.cfg.block_message)
        v_guard_interval = tk.StringVar(value=str(self.cfg.guard_interval))
        v_pw_fails = tk.StringVar(value=str(self.cfg.password_max_fails))
        v_pw_lockout = tk.StringVar(value=str(self.cfg.password_lockout_seconds))

        dlg = tk.Toplevel(self)
        dlg.title("환경설정")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.geometry("720x620")

        notebook = ttk.Notebook(dlg)
        notebook.pack(fill="both", expand=True, padx=12, pady=12)

        def page(title: str) -> ttk.Frame:
            f = ttk.Frame(notebook)
            notebook.add(f, text=title)
            return f

        def option(parent, text: str, var, hint: str, enabled: bool = True) -> None:
            cb = ttk.Checkbutton(parent, text=text, variable=var)
            cb.pack(anchor="w", padx=10, pady=(6, 0))
            if not enabled:
                cb.state(["disabled"])
            ttk.Label(parent, text=hint, foreground="#7f8c8d").pack(anchor="w", padx=32, pady=(0, 4))

        def labeled_entry(parent, label: str, var, hint: str = "", width: int = 12) -> None:
            row = ttk.Frame(parent)
            row.pack(fill="x", padx=10, pady=(8, 0))
            ttk.Label(row, text=label, width=22).pack(side="left")
            ttk.Entry(row, textvariable=var, width=width).pack(side="left")
            if hint:
                ttk.Label(parent, text=hint, foreground="#7f8c8d").pack(anchor="w", padx=32, pady=(0, 4))

        f1 = page("시작")
        option(f1, "Windows 시작(로그온) 시 EduGuard 자동 실행", v_startup,
               "관리자 권한으로 실행해야 변경할 수 있습니다. (작업 스케줄러에 최고 권한으로 등록)",
               enabled=is_admin())
        option(f1, "시작할 때 창 없이 트레이 아이콘으로 실행", v_tray_start,
               "창을 띄우지 않고 알림 영역(트레이)에서만 동작합니다. 아이콘을 눌러 창을 엽니다.", tray_ok)
        option(f1, "창 닫기(X) 시 종료하지 않고 트레이로 숨기기", v_tray_close,
               "끄면 X 버튼이 '프로그램 종료'(비밀번호 확인)로 동작합니다.", tray_ok)
        option(f1, "트레이 알림 표시", v_tray_notify,
               "차단 시작/해제, 일시 해제, 트레이 숨김 안내를 알림으로 표시합니다.", tray_ok)
        option(f1, "강제 종료 감시(워치독) 사용", v_watchdog,
               "작업 관리자 등으로 EduGuard가 강제 종료되면 다시 실행합니다. 정상 종료는 재실행하지 않습니다.")

        f2 = page("차단")
        option(f2, "프로그램이 시작되면 자동으로 차단 시작", v_auto_block,
               "끄면 실행 후 '차단 시작' 버튼을 눌러야 차단됩니다.")
        option(f2, "시간대 스케줄 사용", v_schedule,
               "아래 시간대 안에서는 자동 차단, 밖에서는 자동 해제됩니다. 일시 해제가 켜져 있으면 일시 해제가 우선입니다.")
        option(f2, "원격지원모드 허용", v_remote_support,
               "TeamViewer 연결 유지를 위해 teamviewer/dyngate 도메인을 추가로 허용합니다. 사용 후 끄는 것을 권장합니다.")
        row = ttk.Frame(f2)
        row.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(row, text="차단 적용 시간대", width=22).pack(side="left")
        ttk.Entry(row, textvariable=v_schedule_start, width=8).pack(side="left")
        ttk.Label(row, text="  ~  ").pack(side="left")
        ttk.Entry(row, textvariable=v_schedule_end, width=8).pack(side="left")
        ttk.Label(f2, text="HH:MM 형식. 시작/끝이 같으면 하루 종일 적용, 자정을 넘기는 시간대도 가능합니다.",
                  foreground="#7f8c8d").pack(anchor="w", padx=32, pady=(0, 4))
        labeled_entry(f2, "일시 해제 기본 시간(분)", v_unlock_minutes, "1 ~ 480분", 8)
        labeled_entry(f2, "차단 페이지 문구", v_block_message, "차단 페이지에 표시됩니다. 200자 이하", 48)

        f3 = page("네트워크")
        row = ttk.Frame(f3)
        row.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(row, text="필터 프록시 포트", width=22).pack(side="left")
        ttk.Spinbox(row, from_=1024, to=65535, width=8, textvariable=v_port).pack(side="left")
        ttk.Label(f3, text="변경한 포트는 다음 차단 시작부터 적용됩니다. (기본 8899)",
                  foreground="#7f8c8d").pack(anchor="w", padx=10, pady=(0, 6))
        labeled_entry(f3, "프록시 설정 감시 주기(초)", v_guard_interval, "0.5 ~ 30초. 짧을수록 임의 변경 복구가 빠릅니다.", 8)

        f4 = page("로그")
        option(f4, "반복 로그 숨기기", v_suppress_repeats,
               "같은 허용/차단 로그가 짧은 시간 안에 반복되면 하단 로그와 파일 로그에 다시 표시하지 않습니다.")
        labeled_entry(f4, "반복 로그 숨김 시간(초)", v_repeat_window, "5 ~ 600초. 기본 30초", 8)
        option(f4, "접속 로그를 파일로 저장", v_log_file,
               "logs 폴더에 날짜별 파일로 저장합니다.")
        labeled_entry(f4, "접속 로그 보관 기간(일)", v_retention, "1 ~ 365일", 8)
        option(f4, "설정 변경 이력 기록", v_audit,
               "환경설정, 키워드 변경, 차단 시작/해제 등을 settings-history.log 에 기록합니다.")
        log_btns = ttk.Frame(f4)
        log_btns.pack(fill="x", padx=10, pady=10)
        ttk.Button(log_btns, text="로그 폴더 열기", command=lambda: self._open_path(LOG_DIR)).pack(side="left")
        ttk.Button(log_btns, text="설정 변경 이력 열기",
                   command=lambda: self._open_path(AUDIT_LOG_PATH)).pack(side="left", padx=6)

        f5 = page("보안")
        labeled_entry(f5, "비밀번호 실패 허용 횟수", v_pw_fails, "1 ~ 20회", 8)
        labeled_entry(f5, "비밀번호 잠금 시간(초)", v_pw_lockout, "5 ~ 3600초", 8)

        if not tray_ok:
            ttk.Label(dlg, text="※ 트레이 아이콘을 사용할 수 없어 트레이 관련 옵션이 비활성화되었습니다.",
                      foreground="#c0392b").pack(anchor="w", padx=12, pady=(8, 0))

        def save() -> None:
            want_startup = v_startup.get()
            if want_startup != registered:
                ok, msg = startup.register() if want_startup else startup.unregister()
                if not ok:
                    messagebox.showerror(APP_NAME, f"자동 실행 설정을 변경하지 못했습니다.\n{msg}", parent=dlg)
                    return
            try:
                self.cfg.set_settings(
                    run_at_startup=want_startup,
                    start_in_tray=v_tray_start.get(),
                    close_to_tray=v_tray_close.get(),
                    auto_start=v_auto_block.get(),
                    schedule_enabled=v_schedule.get(),
                    schedule_start=v_schedule_start.get().strip(),
                    schedule_end=v_schedule_end.get().strip(),
                    temp_unlock_minutes=v_unlock_minutes.get().strip(),
                    watchdog_enabled=v_watchdog.get(),
                    log_to_file=v_log_file.get(),
                    log_retention_days=v_retention.get().strip(),
                    suppress_repeated_logs=v_suppress_repeats.get(),
                    repeat_log_window_seconds=v_repeat_window.get().strip(),
                    block_message=v_block_message.get().strip(),
                    guard_interval=v_guard_interval.get().strip(),
                    tray_notifications=v_tray_notify.get(),
                    password_max_fails=v_pw_fails.get().strip(),
                    password_lockout_seconds=v_pw_lockout.get().strip(),
                    audit_enabled=v_audit.get(),
                    remote_support_enabled=v_remote_support.get(),
                    port=v_port.get().strip(),
                )
            except ConfigError as e:
                messagebox.showerror(APP_NAME, str(e), parent=dlg)
                return
            self.guard.interval = self.cfg.guard_interval
            self.proxy.set_block_message(self.cfg.block_message)
            self._refresh_keywords()
            if self.cfg.watchdog_enabled:
                self._start_watchdog_if_needed()
            else:
                self._write_normal_exit_flag()
            self.audit("SETTINGS", "환경설정 저장")
            if self.blocking and self.cfg.port != self.proxy.port:
                messagebox.showinfo(APP_NAME, "포트 변경은 차단을 해제했다가 다시 시작하면 적용됩니다.", parent=dlg)
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=12, pady=12)
        ttk.Button(btns, text="취소", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="저장", command=save).pack(side="right", padx=6)

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dlg.grab_set()
        self.wait_window(dlg)

    # -------------------------------------------------------------- 차단 제어
    def start_blocking(self, silent: bool = False) -> None:
        if self.blocking:
            return
        try:
            self.proxy.port = self.cfg.port
            self.proxy.filter.set_keywords(self._effective_keywords())
            self.proxy.set_block_message(self.cfg.block_message)
            self.guard.interval = self.cfg.guard_interval
            self.proxy.start()
        except OSError as e:
            messagebox.showerror(APP_NAME, f"프록시를 시작할 수 없습니다 (포트 {self.cfg.port} 사용 중?)\n{e}",
                                 parent=self)
            return
        self.sysproxy.enable(self.proxy.port)
        self.guard.start()
        self.blocking = True
        self.temp_unlock_until = 0.0
        self.audit("BLOCK_START", f"port={self.proxy.port}")
        self._refresh_status()
        if self.cfg.tray_notifications and not silent:
            self.tray.notify(APP_NAME, "차단을 시작했습니다.")
        if not is_admin():
            self.events.put({"time": time.strftime("%H:%M:%S"), "action": "GUARD", "host": "권한",
                             "detail": "관리자 권한이 아님: 다른 사용자 계정에는 적용되지 않을 수 있음"})

    def stop_blocking(self, ask: bool = True, silent: bool = False) -> bool:
        if not self.blocking:
            return True
        if ask and not self.require_password("차단을 해제합니다."):
            return False
        self.guard.stop()
        self.sysproxy.disable()
        self.proxy.stop()
        self.blocking = False
        self.audit("BLOCK_STOP", "차단 해제")
        self._refresh_status()
        if self.cfg.tray_notifications and not silent:
            self.tray.notify(APP_NAME, "차단을 해제했습니다.")
        return True

    def temp_unlock(self) -> None:
        if not self.blocking:
            return
        minutes = simpledialog.askinteger(
            APP_NAME, "몇 분 동안 차단을 해제할까요?",
            initialvalue=self.cfg.temp_unlock_minutes, minvalue=1, maxvalue=480, parent=self,
        )
        if minutes is None:
            return
        if not self.require_password(f"{minutes}분 동안 차단을 일시 해제합니다."):
            return
        if self.stop_blocking(ask=False, silent=True):
            self.temp_unlock_until = time.time() + minutes * 60
            self.audit("TEMP_UNLOCK", f"{minutes}분")
            self._refresh_status()
            if self.cfg.tray_notifications:
                self.tray.notify(APP_NAME, f"{minutes}분 동안 차단을 일시 해제했습니다.")

    # -------------------------------------------------------------- 키워드 관리
    def add_keyword(self) -> None:
        raw = self.kw_entry.get()
        if not raw.strip():
            return
        if not self.require_password(f"키워드 '{raw.strip()}' 를 추가합니다."):
            return
        try:
            self.cfg.add_keyword(raw)
        except ConfigError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        self.audit("KEYWORD_ADD", raw.strip())
        self.kw_entry.delete(0, "end")
        self._refresh_keywords()
        self._refresh_status()

    def remove_keywords(self) -> None:
        selected = [self.kw_list.get(i) for i in self.kw_list.curselection()]
        if not selected:
            messagebox.showinfo(APP_NAME, "삭제할 키워드를 선택하세요.", parent=self)
            return
        if not self.require_password(f"키워드 {', '.join(selected)} 를 삭제합니다."):
            return
        for kw in selected:
            self.cfg.remove_keyword(kw)
        self.audit("KEYWORD_REMOVE", ", ".join(selected))
        self._refresh_keywords()
        self._refresh_status()

    def reset_keywords(self) -> None:
        if not self.require_password("키워드를 기본값으로 되돌립니다."):
            return
        self.cfg.reset_keywords()
        self.audit("KEYWORD_RESET", "기본값 복원")
        self._refresh_keywords()
        self._refresh_status()

    def export_keywords(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="허용 키워드 내보내기", defaultextension=".json",
            filetypes=[("JSON 파일", "*.json"), ("텍스트 파일", "*.txt"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(self.cfg.keywords) + "\n")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"keywords": self.cfg.keywords}, f, ensure_ascii=False, indent=2)
            self.audit("KEYWORD_EXPORT", path)
            messagebox.showinfo(APP_NAME, "허용 키워드를 내보냈습니다.", parent=self)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"내보내기 실패\n{e}", parent=self)

    def import_keywords(self) -> None:
        if not self.require_password("허용 키워드 목록을 가져옵니다. 기존 목록을 교체합니다."):
            return
        path = filedialog.askopenfilename(
            parent=self, title="허용 키워드 가져오기",
            filetypes=[("JSON/텍스트 파일", "*.json *.txt"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                if path.lower().endswith(".json"):
                    doc = json.load(f)
                    values = doc["keywords"] if isinstance(doc, dict) else doc
                else:
                    values = [line.strip() for line in f if line.strip()]
            self.cfg.set_keywords(values)
        except (OSError, ValueError, TypeError, KeyError, ConfigError) as e:
            messagebox.showerror(APP_NAME, f"가져오기 실패\n{e}", parent=self)
            return
        self.audit("KEYWORD_IMPORT", path)
        self._refresh_keywords()
        self._refresh_status()
        messagebox.showinfo(APP_NAME, "허용 키워드를 가져왔습니다.", parent=self)

    def test_domain(self) -> None:
        text = self.test_entry.get().strip()
        if not text:
            return
        host = text
        if "://" in text:
            from urllib.parse import urlsplit
            host = urlsplit(text).hostname or text
        allowed, why = self.proxy.filter.check(host.split("/")[0].split(":")[0])
        if allowed:
            self.test_result.configure(text=f"✅ 허용 - 키워드 '{why}' 포함", foreground="#0a7d2c")
        else:
            self.test_result.configure(text=f"🚫 차단 - {why}", foreground="#c0392b")

    # -------------------------------------------------------------- 종료
    def on_exit(self) -> None:
        if self._exit_prompt_open:
            return
        self._exit_prompt_open = True
        try:
            if not self.require_password("프로그램을 종료합니다. (종료 시 차단도 해제됩니다)"):
                return
        finally:
            self._exit_prompt_open = False
        self._write_normal_exit_flag()
        self.stop_blocking(ask=False)
        self.tray.stop()
        self.destroy()


# ------------------------------------------------------------------ 진입점
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


def uninstall_cleanup() -> None:
    """MSI 제거 시 가능한 범위에서 런타임 잔여물과 시스템 설정을 정리한다."""
    try:
        SystemProxy(BACKUP_PATH).disable()
    except Exception:
        pass
    try:
        startup.unregister()
    except Exception:
        pass
    try:
        with open(WATCHDOG_FLAG_PATH, "w", encoding="utf-8") as f:
            f.write("uninstall")
    except OSError:
        pass
    for path in (CONFIG_PATH, CONFIG_PATH + ".bak", BACKUP_PATH, WATCHDOG_FLAG_PATH):
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
        ctypes.windll.user32.MessageBoxW(None, "관리자 권한이 필요합니다. UAC 창에서 '예'를 선택해 주세요.",
                                         APP_NAME, 0x10)
        sys.exit(1)

    if "--restore" in args:
        restore_cli()
        return

    if not acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(None, "이미 실행 중입니다.\n창이 보이지 않으면 작업표시줄 오른쪽 트레이(^)의 EduGuard 아이콘을 눌러 주세요.",
                                         APP_NAME, 0x40)
        sys.exit(0)

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

    config = ConfigStore(CONFIG_PATH, protect_file=is_admin())
    App(config).mainloop()


if __name__ == "__main__":
    main()
