"""EduGuard 버전/업데이트 상수."""

APP_NAME = "EduGuard"
# 점 네 자리. Git 태그는 v1.0.0-N, package.json 은 1.0.0-N. npm run version:bump 가 함께 올린다.
APP_VERSION = "1.0.0.6"

# GitHub Releases 저장소 (owner/repo). "배포해줘" 의 publish:release 가 사용한다.
UPDATE_REPO = "soonpyopark/EduGuard"

# 배포 서버가 정해지면 HTTPS URL을 넣는다.
# 환경변수 EDUGUARD_UPDATE_URL 이 있으면 그 값을 우선 사용한다.
UPDATE_MANIFEST_URL = ""

# scripts/build_msi.py 가 빌드 직전에 build_info.py 를 만들어 심는다.
try:
    from build_info import BUILD_TIME as APP_BUILD_TIME
except ImportError:
    APP_BUILD_TIME = ""


def app_semver(version: str = APP_VERSION) -> str:
    """`1.0.0.2` → `1.0.0-2`. Electron·package.json 용. 세 자리면 그대로 둔다."""
    parts = version.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return f"{parts[0]}.{parts[1]}.{parts[2]}-{parts[3]}"
    return version


def app_git_tag(version: str = APP_VERSION) -> str:
    """`1.0.0.2` → `v1.0.0-2`."""
    return f"v{app_semver(version)}"


def version_display() -> str:
    """버전과 빌드 시각을 한 줄로 표시."""
    if APP_BUILD_TIME:
        return f"{APP_VERSION} (빌드 {APP_BUILD_TIME})"
    return f"{APP_VERSION} (소스 실행)"
