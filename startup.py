"""Windows 시작(로그온) 시 자동 실행 등록 모듈.

레지스트리 Run 키는 관리자 권한이 필요한 프로그램을 로그온 때 실행하지 못하거나 UAC 창을 띄우므로,
'최고 권한' 작업 스케줄러 작업(로그온 트리거)으로 등록한다. 등록/삭제에는 관리자 권한이 필요하다.

- EduGuard: 본 프로그램 자동 실행
- EduGuardProxyCleanup: 로그온 즉시 잔여 시스템 프록시만 복원 (StarPlayer 등보다 먼저 뜨도록 Delay=0)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple
from xml.sax.saxutils import escape

TASK_NAME = "EduGuard"
CLEANUP_TASK_NAME = "EduGuardProxyCleanup"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def launch_command() -> Tuple[str, List[str]]:
    """자동 실행 시 사용할 (실행파일, 인자 목록). 개발 환경에서는 콘솔 창이 없는 pythonw.exe 를 쓴다."""
    if getattr(sys, "frozen", False):
        return sys.executable, []
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return exe, [main_py]


def launch_cleanup_command() -> Tuple[str, List[str]]:
    """로그온 직후 잔여 프록시만 복원하는 명령."""
    exe, args = launch_command()
    return exe, list(args) + ["--clear-stale-proxy"]


def build_task_xml(
    exe: str,
    args: List[str],
    run_level: str = "HighestAvailable",
    *,
    delay: Optional[str] = None,
    description: str = "EduGuard - Windows 로그온 시 자동 실행",
) -> str:
    """작업 스케줄러 XML. 배터리/72시간 제한 같은 기본 중단 조건을 모두 꺼서 계속 실행되게 한다."""
    arguments = subprocess.list2cmdline(args)
    # 배포(exe + --clear-stale-proxy)처럼 첫 인자가 경로가 아니면 exe 폴더를 쓴다.
    # 그렇지 않으면 WorkingDirectory 가 빈 문자열이 되어 schtasks 가 거부한다.
    workdir = os.path.dirname(exe)
    if args and not str(args[0]).startswith("-"):
        script_dir = os.path.dirname(args[0])
        if script_dir:
            workdir = script_dir
    delay_xml = f"\n      <Delay>{escape(delay)}</Delay>" if delay else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>{delay_xml}
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{run_level}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(cmd: List[str]) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    out = (p.stdout or b"") + (p.stderr or b"")
    return p.returncode, out.decode("mbcs", errors="replace").strip()


def is_registered(task_name: str = TASK_NAME) -> bool:
    if sys.platform != "win32":
        return False
    return _run(["schtasks", "/Query", "/TN", task_name])[0] == 0


def _register_xml(task_name: str, xml: str) -> Tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Windows 에서만 사용할 수 있습니다."
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        code, out = _run(["schtasks", "/Create", "/F", "/TN", task_name, "/XML", path])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return code == 0, out


def register(task_name: str = TASK_NAME, run_level: str = "HighestAvailable") -> Tuple[bool, str]:
    """로그온 자동 실행 작업을 (덮어써서) 등록한다. 잔여 프록시 정리 작업보다 조금 늦게 뜨도록 15초 지연."""
    exe, args = launch_command()
    xml = build_task_xml(
        exe, args, run_level,
        delay="PT15S",
        description="EduGuard - Windows 로그온 시 자동 실행",
    )
    return _register_xml(task_name, xml)


def register_cleanup(task_name: str = CLEANUP_TASK_NAME, run_level: str = "HighestAvailable") -> Tuple[bool, str]:
    """로그온 즉시 잔여 EduGuard 시스템 프록시를 복원하는 작업을 등록한다."""
    exe, args = launch_cleanup_command()
    xml = build_task_xml(
        exe, args, run_level,
        delay="PT0S",
        description="EduGuard - 로그온 직후 잔여 시스템 프록시 복원",
    )
    return _register_xml(task_name, xml)


def unregister(task_name: str = TASK_NAME) -> Tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Windows 에서만 사용할 수 있습니다."
    if not is_registered(task_name):
        return True, ""
    code, out = _run(["schtasks", "/Delete", "/F", "/TN", task_name])
    return code == 0, out


def unregister_all() -> Tuple[bool, str]:
    """본 프로그램/잔여 프록시 정리 작업을 모두 제거한다."""
    ok1, msg1 = unregister(TASK_NAME)
    ok2, msg2 = unregister(CLEANUP_TASK_NAME)
    return ok1 and ok2, "\n".join(m for m in (msg1, msg2) if m)
