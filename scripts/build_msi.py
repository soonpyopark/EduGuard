"""EduGuard 실행 파일과 MSI 설치 파일을 생성한다.

구성:
  EduGuardCore.exe  Python 엔진(PyInstaller, 프록시·감시·로컬 API)
  EduGuard.exe      Electron UI(@electron/packager) + 런타임 파일들

출력:
  msi/EduGuard_1.0.0_YYMMDD_HHMMSS.msi
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version import APP_NAME, APP_VERSION, app_semver  # noqa: E402

PROJECT_NAME = APP_NAME
CORE_NAME = f"{APP_NAME}Core"
UI_DIR = ROOT / "ui"
MANUFACTURER = "EduGuard"
UPGRADE_CODE = "E6B54289-5E29-47F1-9B0B-12F9543B5F15"
BUILD_INFO_PATH = ROOT / "build_info.py"


def run(cmd: list[str], cwd: Path = ROOT) -> None:
    print(">", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"필수 빌드 도구를 찾을 수 없습니다: {name}")
    return path


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def write_build_info(path: Path, build_time: str) -> None:
    """실행 파일에 포함될 빌드 시각 모듈을 생성한다."""
    path.write_text(
        dedent(f'''
            """빌드 시 자동 생성. 직접 수정하지 마세요."""
            BUILD_TIME = {build_time!r}
        ''').strip() + "\n",
        encoding="utf-8",
    )


def write_version_info(path: Path) -> None:
    major, minor, patch, build = version_tuple(APP_VERSION)
    path.write_text(dedent(f"""
        # UTF-8
        VSVersionInfo(
          ffi=FixedFileInfo(
            filevers=({major}, {minor}, {patch}, {build}),
            prodvers=({major}, {minor}, {patch}, {build}),
            mask=0x3f,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0)
          ),
          kids=[
            StringFileInfo([
              StringTable('040904B0', [
                StringStruct('CompanyName', '{MANUFACTURER}'),
                StringStruct('FileDescription', '{PROJECT_NAME} Core'),
                StringStruct('FileVersion', '{APP_VERSION}'),
                StringStruct('InternalName', '{CORE_NAME}'),
                StringStruct('OriginalFilename', '{CORE_NAME}.exe'),
                StringStruct('ProductName', '{PROJECT_NAME}'),
                StringStruct('ProductVersion', '{APP_VERSION}')
              ])
            ]),
            VarFileInfo([VarStruct('Translation', [1033, 1200])])
          ]
        )
    """).strip() + "\n", encoding="utf-8")


def build_core() -> Path:
    require_tool("pyinstaller.exe")
    build_dir = ROOT / "build" / "pyinstaller"
    dist_dir = ROOT / "build" / "dist"
    version_file = ROOT / "build" / "version_info.txt"
    build_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    write_version_info(version_file)
    run([
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconsole",
        "--onefile",
        "--name", CORE_NAME,
        "--icon", str(ROOT / "assets" / "eduguard.ico"),
        "--version-file", str(version_file),
        "--distpath", str(dist_dir),
        "--workpath", str(build_dir),
        "--specpath", str(ROOT / "build"),
        str(ROOT / "main.py"),
    ])
    exe = dist_dir / f"{CORE_NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"exe 생성 실패: {exe}")
    return exe


def build_ui() -> Path:
    """Tailwind CSS 를 빌드하고 Electron UI 를 패키징한다."""
    node = require_tool("node.exe")
    npm = require_tool("npm.cmd")
    if not (UI_DIR / "node_modules" / "@electron" / "packager").exists():
        run([npm, "install", "--no-audit", "--no-fund"], cwd=UI_DIR)
    if not (UI_DIR / "node_modules" / "electron" / "dist" / "electron.exe").exists():
        # npm 11 은 의존성 install 스크립트를 건너뛸 수 있어 Electron 바이너리를 직접 받는다.
        run([node, str(UI_DIR / "node_modules" / "electron" / "install.js")], cwd=UI_DIR)
    run([npm, "run", "build:css"], cwd=UI_DIR)
    out_root = ROOT / "build" / "electron"
    # Electron 의 app version 은 semver 라 1.0.0.2 를 받지 않는다. MSI 는 점 네 자리를 유지한다.
    run([node, str(UI_DIR / "scripts" / "package.mjs"), str(out_root), app_semver()], cwd=UI_DIR)
    app_dir = out_root / f"{PROJECT_NAME}-win32-x64"
    if not (app_dir / f"{PROJECT_NAME}.exe").exists():
        raise SystemExit(f"Electron 패키징 실패: {app_dir}")
    return app_dir


def write_wxs(path: Path, app_dir: Path) -> None:
    icon_path = ROOT / "assets" / "eduguard.ico"
    path.write_text(dedent(f"""
        <Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
          <Package
              Name="{PROJECT_NAME}"
              Manufacturer="{MANUFACTURER}"
              Version="{APP_VERSION}"
              UpgradeCode="{{{UPGRADE_CODE}}}"
              Scope="perMachine">
            <SummaryInformation Description="{PROJECT_NAME} 설치 관리자" Manufacturer="{MANUFACTURER}" />
            <MajorUpgrade
                AllowSameVersionUpgrades="yes"
                DowngradeErrorMessage="이미 더 높은 버전의 {PROJECT_NAME}이(가) 설치되어 있습니다." />
            <MediaTemplate EmbedCab="yes" />
            <Icon Id="AppIcon.ico" SourceFile="{icon_path}" />
            <Property Id="ARPPRODUCTICON" Value="AppIcon.ico" />
            <CustomAction Id="EduGuardUninstallCleanup"
                          FileRef="EduGuardCoreExe"
                          ExeCommand="--uninstall-cleanup"
                          Execute="deferred"
                          Impersonate="no"
                          Return="ignore" />
            <InstallExecuteSequence>
              <Custom Action="EduGuardUninstallCleanup" Before="RemoveFiles" Condition="REMOVE=&quot;ALL&quot;" />
            </InstallExecuteSequence>
            <Feature Id="MainFeature" Title="{PROJECT_NAME}" Level="1">
              <ComponentGroupRef Id="AppComponents" />
            </Feature>
          </Package>

          <Fragment>
            <StandardDirectory Id="ProgramFilesFolder">
              <Directory Id="INSTALLFOLDER" Name="{PROJECT_NAME}" />
            </StandardDirectory>
            <StandardDirectory Id="ProgramMenuFolder">
              <Directory Id="ApplicationProgramsFolder" Name="{PROJECT_NAME}" />
            </StandardDirectory>
            <StandardDirectory Id="DesktopFolder" />

            <ComponentGroup Id="AppComponents" Directory="INSTALLFOLDER">
              <Component Id="MainExecutable" Guid="*">
                <File Id="EduGuardExe" Source="{app_dir / f'{PROJECT_NAME}.exe'}" KeyPath="yes" />
                <Shortcut Id="StartMenuShortcut"
                          Directory="ApplicationProgramsFolder"
                          Name="{PROJECT_NAME}"
                          Target="[INSTALLFOLDER]{PROJECT_NAME}.exe"
                          WorkingDirectory="INSTALLFOLDER"
                          Icon="AppIcon.ico" />
                <RemoveFolder Id="RemoveApplicationProgramsFolder"
                              Directory="ApplicationProgramsFolder"
                              On="uninstall" />
              </Component>
              <Component Id="DesktopShortcut" Guid="*" Directory="DesktopFolder">
                <Shortcut Id="DesktopShortcutItem"
                          Name="{PROJECT_NAME}"
                          Target="[INSTALLFOLDER]{PROJECT_NAME}.exe"
                          WorkingDirectory="INSTALLFOLDER"
                          Icon="AppIcon.ico" />
                <RegistryValue Root="HKLM"
                               Key="Software\\{MANUFACTURER}\\{PROJECT_NAME}"
                               Name="DesktopShortcut"
                               Type="integer"
                               Value="1"
                               KeyPath="yes" />
              </Component>
              <Component Id="CoreExecutable" Guid="*">
                <File Id="EduGuardCoreExe" Source="{app_dir / f'{CORE_NAME}.exe'}" KeyPath="yes" />
              </Component>
              <Files Include="{app_dir}\\**">
                <Exclude Files="{app_dir / f'{PROJECT_NAME}.exe'}" />
                <Exclude Files="{app_dir / f'{CORE_NAME}.exe'}" />
              </Files>
            </ComponentGroup>
          </Fragment>
        </Wix>
    """).strip() + "\n", encoding="utf-8")


def build_msi(app_dir: Path, file_stamp: str) -> Path:
    wix = require_tool("wix.exe")
    out_dir = ROOT / "msi"
    work_dir = ROOT / "build" / "msi"
    out_dir.mkdir(exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_msi = out_dir / f"{PROJECT_NAME}_{APP_VERSION}_{file_stamp}.msi"
    wxs = work_dir / f"{PROJECT_NAME}.wxs"
    write_wxs(wxs, app_dir)
    run([wix, "build", str(wxs), "-o", str(out_msi)])
    if not out_msi.exists():
        raise SystemExit(f"MSI 생성 실패: {out_msi}")
    return out_msi


def main() -> int:
    now = datetime.now()
    display_time = now.strftime("%Y-%m-%d %H:%M:%S")
    file_stamp = now.strftime("%y%m%d_%H%M%S")
    write_build_info(BUILD_INFO_PATH, display_time)
    try:
        core = build_core()
        app_dir = build_ui()
        shutil.copy2(core, app_dir / core.name)
        msi = build_msi(app_dir, file_stamp)
    finally:
        if BUILD_INFO_PATH.exists():
            BUILD_INFO_PATH.unlink()
    print(f"\nMSI 생성 완료: {msi}")
    print(f"빌드 시각: {display_time}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
