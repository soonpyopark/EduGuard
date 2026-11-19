"""StarPlayer / Axissoft 로컬 헬퍼 준비 여부 확인.

EduGuard 가 시스템 프록시를 켜기 전에 StarPlayer 가 기동했는지 확인한다.
프로세스 이름 위주로 검사하며, 네트워크/관리자 권한이 없어도 동작한다.

부팅 때 남은 프록시 때문에 이미 떠 버린 StarPlayer 는 연결에 실패한 채 남는다.
프록시를 지운 뒤에는 프로세스가 아니라 그 실행 파일을 다시 띄워야 한다.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Callable, Iterable, List, Sequence, Tuple

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# tasklist 출력에서 찾을 프로세스 이름 조각 (대소문자 무시)
DEFAULT_PROCESS_MARKERS = (
    "starplayer",
    "starplayerplus",
    "axissoft",
    "axisservice",
)


def _tasklist_output() -> str:
    if sys.platform != "win32":
        return ""
    try:
        p = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=8, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    raw = p.stdout or b""
    for enc in ("mbcs", "utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def list_running_process_names(text: str | None = None) -> Tuple[str, ...]:
    """tasklist CSV 출력에서 실행 파일 이름만 추출한다."""
    body = text if text is not None else _tasklist_output()
    names = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        # "Image Name","PID","Session Name","Session#","Mem Usage"
        m = re.match(r'^"([^"]+)"', line)
        if m:
            names.append(m.group(1))
            continue
        # 혹시 CSV 가 아닌 경우 첫 토큰
        parts = line.split()
        if parts:
            names.append(parts[0])
    return tuple(names)


def is_starplayer_ready(markers: Iterable[str] = DEFAULT_PROCESS_MARKERS,
                        process_names: Iterable[str] | None = None) -> bool:
    """StarPlayer/Axissoft 관련 프로세스가 하나라도 있으면 True."""
    names = list(process_names) if process_names is not None else list(list_running_process_names())
    lowered = [n.lower() for n in names]
    for marker in markers:
        m = marker.lower()
        if any(m in name for name in lowered):
            return True
    return False


def _matches_marker(name: str, markers: Iterable[str] = DEFAULT_PROCESS_MARKERS) -> bool:
    lowered = name.lower()
    return any(marker.lower() in lowered for marker in markers)


def restart_processes(
    processes: Sequence[Tuple[int, str]],
    killer: Callable[[int], None],
    launcher: Callable[[str], None],
) -> int:
    """이미 떠 있는 StarPlayer 를 끝내고 같은 실행 파일을 다시 띄운다. 다시 띄운 개수를 반환."""
    paths: List[str] = []
    for pid, exe in processes:
        if pid:
            killer(pid)
        if exe and exe not in paths:
            paths.append(exe)
    for exe in paths:
        launcher(exe)
    return len(paths)


def _query_starplayer_processes() -> List[Tuple[int, str]]:
    if sys.platform != "win32":
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId, Name, ExecutablePath | ConvertTo-Json -Compress"
    )
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = (p.stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows = parsed if isinstance(parsed, list) else [parsed]
    found = []
    for row in rows:
        name = str(row.get("Name") or "")
        if not _matches_marker(name):
            continue
        pid = int(row.get("ProcessId") or 0)
        exe = str(row.get("ExecutablePath") or "")
        if pid:
            found.append((pid, exe))
    return found


def _kill_pid(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        capture_output=True, timeout=15, creationflags=_NO_WINDOW,
    )


def _launch_exe(exe: str) -> None:
    # 플레이어 창이 보여야 하므로 콘솔 숨김 플래그는 쓰지 않는다.
    subprocess.Popen([exe], close_fds=True)  # noqa: S603


def restart_if_running() -> int:
    """남은 프록시를 지운 뒤, 이미 떠 있던 StarPlayer 를 깨끗한 네트워크에서 다시 시작한다."""
    return restart_processes(_query_starplayer_processes(), _kill_pid, _launch_exe)
