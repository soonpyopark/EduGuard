"""Build EduGuard executable and MSI installer.

Output:
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

from version import APP_NAME, APP_VERSION  # noqa: E402

PROJECT_NAME = APP_NAME
MANUFACTURER = "EduGuard"
UPGRADE_CODE = "E6B54289-5E29-47F1-9B0B-12F9543B5F15"


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
                StringStruct('FileDescription', '{PROJECT_NAME}'),
                StringStruct('FileVersion', '{APP_VERSION}'),
                StringStruct('InternalName', '{PROJECT_NAME}'),
                StringStruct('OriginalFilename', '{PROJECT_NAME}.exe'),
                StringStruct('ProductName', '{PROJECT_NAME}'),
                StringStruct('ProductVersion', '{APP_VERSION}')
              ])
            ]),
            VarFileInfo([VarStruct('Translation', [1033, 1200])])
          ]
        )
    """).strip() + "\n", encoding="utf-8")


def build_exe() -> Path:
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
        "--name", PROJECT_NAME,
        "--icon", str(ROOT / "assets" / "eduguard.ico"),
        "--version-file", str(version_file),
        "--add-data", f"{ROOT / 'assets'};assets",
        "--distpath", str(dist_dir),
        "--workpath", str(build_dir),
        "--specpath", str(ROOT / "build"),
        str(ROOT / "main.py"),
    ])
    exe = dist_dir / f"{PROJECT_NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"exe 생성 실패: {exe}")
    return exe


def write_wxs(path: Path, exe_path: Path) -> None:
    icon_path = ROOT / "assets" / "eduguard.ico"
    path.write_text(dedent(f"""
        <Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
          <Package
              Name="{PROJECT_NAME}"
              Manufacturer="{MANUFACTURER}"
              Version="{APP_VERSION}"
              UpgradeCode="{{{UPGRADE_CODE}}}"
              Scope="perMachine">
            <SummaryInformation Description="{PROJECT_NAME} installer" Manufacturer="{MANUFACTURER}" />
            <MajorUpgrade
                AllowSameVersionUpgrades="yes"
                DowngradeErrorMessage="A newer version of {PROJECT_NAME} is already installed." />
            <MediaTemplate EmbedCab="yes" />
            <Icon Id="AppIcon.ico" SourceFile="{icon_path}" />
            <Property Id="ARPPRODUCTICON" Value="AppIcon.ico" />
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

            <ComponentGroup Id="AppComponents" Directory="INSTALLFOLDER">
              <Component Id="MainExecutable" Guid="*">
                <File Id="EduGuardExe" Source="{exe_path}" KeyPath="yes" />
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
            </ComponentGroup>
          </Fragment>
        </Wix>
    """).strip() + "\n", encoding="utf-8")


def build_msi(exe: Path) -> Path:
    wix = require_tool("wix.exe")
    out_dir = ROOT / "msi"
    work_dir = ROOT / "build" / "msi"
    out_dir.mkdir(exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
    out_msi = out_dir / f"{PROJECT_NAME}_{APP_VERSION}_{timestamp}.msi"
    wxs = work_dir / f"{PROJECT_NAME}.wxs"
    write_wxs(wxs, exe)
    run([wix, "build", str(wxs), "-o", str(out_msi)])
    if not out_msi.exists():
        raise SystemExit(f"MSI 생성 실패: {out_msi}")
    return out_msi


def main() -> int:
    exe = build_exe()
    msi = build_msi(exe)
    print(f"\nMSI created: {msi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
