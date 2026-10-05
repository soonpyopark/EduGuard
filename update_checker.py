"""시작 시 업데이트 매니페스트를 확인하는 작은 클라이언트.

매니페스트 예시(JSON):
{
  "version": "1.0.1",
  "url": "https://example.com/EduGuard_1.0.1.msi",
  "notes": "버그 수정 및 안정성 개선"
}
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional

from version import APP_VERSION, UPDATE_MANIFEST_URL


@dataclass
class UpdateInfo:
    version: str
    url: str
    notes: str = ""


def manifest_url() -> str:
    return os.environ.get("EDUGUARD_UPDATE_URL", UPDATE_MANIFEST_URL).strip()


def parse_version(value: str) -> tuple:
    parts = []
    for chunk in value.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits or "0"))
    return tuple(parts + [0] * (3 - len(parts)))


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    return parse_version(candidate) > parse_version(current)


def check_for_update(url: Optional[str] = None, current_version: str = APP_VERSION,
                     timeout: float = 5.0) -> Optional[UpdateInfo]:
    target = (manifest_url() if url is None else url).strip()
    if not target:
        return None
    req = urllib.request.Request(target, headers={"User-Agent": f"EduGuard/{current_version}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    version = str(doc.get("version", "")).strip()
    download_url = str(doc.get("url", "")).strip()
    notes = str(doc.get("notes", "")).strip()
    if version and download_url and is_newer(version, current_version):
        return UpdateInfo(version=version, url=download_url, notes=notes)
    return None
