"""EduGuard 강제 종료 감시 프로세스.

부모 EduGuard 프로세스가 작업 관리자 등으로 강제 종료되면 다시 실행한다.
정상 종료(비밀번호 확인 후 종료)는 main.py 가 normal-exit 플래그 파일을 만들기 때문에 재실행하지 않는다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import List

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def default_flag_path(base_dir: str) -> str:
    return os.path.join(base_dir, "watchdog_normal_exit.flag")


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            # process 가 있으면 0, 없으면 128/오류. 출력 언어와 무관하게 종료코드만 사용.
            p = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, timeout=5, creationflags=_NO_WINDOW,
            )
            return p.returncode == 0 and str(pid).encode("ascii") in p.stdout
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def relaunch(command: List[str], cwd: str) -> None:
    subprocess.Popen(command, cwd=cwd or None, creationflags=_NO_WINDOW)


def main(argv: List[str]) -> int:
    if len(argv) < 5:
        return 2
    pid = int(argv[1])
    flag_path = argv[2]
    cwd = argv[3]
    command = argv[4:]

    # 부모가 완전히 뜨기 전에 PID 확인이 흔들리지 않도록 잠깐 대기.
    time.sleep(2)
    while True:
        if os.path.exists(flag_path):
            return 0
        if not is_pid_running(pid):
            time.sleep(1)
            if not os.path.exists(flag_path):
                relaunch(command, cwd)
            return 0
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
