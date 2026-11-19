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
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from version import APP_VERSION, UPDATE_MANIFEST_URL, UPDATE_REPO


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
    return tuple((parts + [0, 0, 0, 0])[:4])


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


def normalize_release_version(tag: str, name: str = "") -> str:
    """릴리스 이름 `v1.0.0.4` 또는 태그 `v1.0.0-4` 를 앱 버전 형식으로 맞춘다."""
    for raw in (name, tag):
        text = str(raw or "").strip()
        if text[:1] in ("v", "V"):
            text = text[1:]
        if re.fullmatch(r"\d+(?:\.\d+){1,3}", text):
            return text
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)-(\d+)", text)
        if match:
            return ".".join(match.groups())
    return ""


def _short_notes(body: str) -> str:
    text = str(body or "").split("## 이전 버전 이력", 1)[0].strip()
    if len(text) > 400:
        text = text[:400].rstrip() + "…"
    return text


def interpret_release(payload: dict, current_version: str = APP_VERSION, repo: str = UPDATE_REPO) -> dict:
    """GitHub 릴리스 JSON 을 업데이트 확인 결과로 바꾼다. 네트워크는 호출하지 않는다."""
    tag = str(payload.get("tag_name", "")).strip()
    latest = normalize_release_version(tag, str(payload.get("name", "")))
    if not latest:
        return {
            "available": False,
            "current": current_version,
            "latest": "",
            "url": "",
            "notes": "",
            "error": f"릴리스 버전을 해석할 수 없습니다: {tag or '(없음)'}",
        }
    url = str(payload.get("html_url", "")).strip() or f"https://github.com/{repo}/releases/latest"
    return {
        "available": is_newer(latest, current_version),
        "current": current_version,
        "latest": latest,
        "url": url,
        "notes": _short_notes(str(payload.get("body", ""))),
        "error": "",
    }


def _fetch_json(url: str, timeout: float, current_version: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"EduGuard/{current_version}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_latest(current_version: str = APP_VERSION, timeout: float = 8.0) -> dict:
    """매니페스트가 있으면 그 주소를, 없으면 GitHub 최신 릴리스를 확인한다."""
    base = {"available": False, "current": current_version, "latest": "", "url": "", "notes": "", "error": ""}
    manifest = manifest_url()
    if manifest:
        try:
            info = check_for_update(url=manifest, current_version=current_version, timeout=timeout)
        except Exception:
            return {**base, "error": "업데이트 정보를 확인하지 못했습니다."}
        if info:
            return {**base, "available": True, "latest": info.version, "url": info.url, "notes": info.notes}
        return {**base, "latest": current_version}

    repo = UPDATE_REPO.strip()
    if "/" not in repo:
        return {**base, "error": "업데이트 저장소가 설정되지 않았습니다."}
    try:
        payload = _fetch_json(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout,
            current_version,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {**base, "error": "게시된 릴리스가 아직 없습니다."}
        return {**base, "error": f"GitHub 응답 오류 (HTTP {exc.code})"}
    except Exception:
        return {**base, "error": "업데이트 정보를 확인하지 못했습니다."}
    if not isinstance(payload, dict):
        return {**base, "error": "업데이트 정보를 확인하지 못했습니다."}
    return interpret_release(payload, current_version, repo)
