"""EduGuard 버전/업데이트 상수."""

APP_NAME = "EduGuard"
APP_VERSION = "1.0.0"

# 배포 서버가 정해지면 HTTPS URL을 넣는다.
# 환경변수 EDUGUARD_UPDATE_URL 이 있으면 그 값을 우선 사용한다.
UPDATE_MANIFEST_URL = ""

# scripts/build_msi.py 가 빌드 직전에 build_info.py 를 만들어 심는다.
try:
    from build_info import BUILD_TIME as APP_BUILD_TIME
except ImportError:
    APP_BUILD_TIME = ""


def version_display() -> str:
    """버전과 빌드 시각을 한 줄로 표시."""
    if APP_BUILD_TIME:
        return f"{APP_VERSION} (빌드 {APP_BUILD_TIME})"
    return f"{APP_VERSION} (소스 실행)"
