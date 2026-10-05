"""Windows 시작(로그온) 시 자동 실행 등록 모듈.

레지스트리 Run 키는 관리자 권한이 필요한 프로그램을 로그온 때 실행하지 못하거나 UAC 창을 띄우므로,
'최고 권한' 작업 스케줄러 작업(로그온 트리거)으로 등록한다. 등록/삭제에는 관리자 권한이 필요하다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import List, Tuple
from xml.sax.saxutils import escape

TASK_NAME = "EduGuard"
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


def build_task_xml(exe: str, args: List[str], run_level: str = "HighestAvailable") -> str:
    """작업 스케줄러 XML. 배터리/72시간 제한 같은 기본 중단 조건을 모두 꺼서 계속 실행되게 한다."""
    arguments = subprocess.list2cmdline(args)
    workdir = os.path.dirname(args[0]) if args else os.path.dirname(exe)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>EduGuard - Windows 로그온 시 자동 실행</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
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


def register(task_name: str = TASK_NAME, run_level: str = "HighestAvailable") -> Tuple[bool, str]:
    """로그온 자동 실행 작업을 (덮어써서) 등록한다."""
    if sys.platform != "win32":
        return False, "Windows 에서만 사용할 수 있습니다."
    exe, args = launch_command()
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(build_task_xml(exe, args, run_level))
        code, out = _run(["schtasks", "/Create", "/F", "/TN", task_name, "/XML", path])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return code == 0, out


def unregister(task_name: str = TASK_NAME) -> Tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Windows 에서만 사용할 수 있습니다."
    if not is_registered(task_name):
        return True, ""
    code, out = _run(["schtasks", "/Delete", "/F", "/TN", task_name])
    return code == 0, out
