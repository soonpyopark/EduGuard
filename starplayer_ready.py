"""StarPlayer / Axissoft 로컬 헬퍼 준비 여부 확인.

EduGuard 가 시스템 프록시를 켜기 전에 StarPlayer 가 기동했는지 확인한다.
프로세스 이름 위주로 검사하며, 네트워크/관리자 권한이 없어도 동작한다.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Iterable, Tuple

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
