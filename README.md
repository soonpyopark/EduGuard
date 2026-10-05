# EduGuard – 학습 사이트 전용 인터넷 제어 (Windows)

현재 버전: **1.0.0**

모든 인터넷 접속을 기본 차단하고, 호스트명에 **허용 키워드**(`ebs`, `sevenedu`, `starplayer`, `jwplatform`, `kollus`, `cloudfront` …)가 포함된 도메인만 통과시킵니다.

## 실행
```powershell
python main.py            # UAC 승인 후 관리자 권한으로 실행, 최초 1회 비밀번호 설정
python main.py --no-admin # 개발용: UAC 생략 (현재 로그인 사용자에게만 적용)
python main.py --restore  # 비상용: 비밀번호 확인 후 시스템 프록시 원복
python -m unittest -v test_eduguard   # 자동 테스트 (네트워크/레지스트리 미사용)
```
외부 라이브러리 없이 Python 3.10+ 표준 라이브러리만 사용합니다.

## MSI 빌드
PyInstaller와 WiX Toolset v7이 설치된 Windows에서 실행합니다.

```powershell
python scripts\build_msi.py
```

산출물은 `msi` 폴더에 아래 형식으로 생성됩니다.

```text
EduGuard_1.0.0_YYMMDD_HHMMSS.msi
```

예: `msi\EduGuard_1.0.0_261005_110236.msi`

## 업데이트 확인
프로그램 시작 후 백그라운드에서 업데이트 manifest를 확인합니다. 기본 URL은 비워 두었고, 배포 서버가 정해지면 `version.py`의 `UPDATE_MANIFEST_URL`을 설정하거나 실행 환경변수 `EDUGUARD_UPDATE_URL`로 지정할 수 있습니다.

Manifest 예시:

```json
{
  "version": "1.0.1",
  "url": "https://example.com/EduGuard_1.0.1.msi",
  "notes": "버그 수정 및 안정성 개선"
}
```

현재 버전보다 높은 `version`이 있으면 알림과 다운로드 안내가 표시됩니다.

## 구성
| 파일 | 역할 |
|---|---|
| `main.py` | Tkinter GUI, UAC 상승, 비밀번호 확인, 전체 제어 |
| `version.py` | 앱 이름/버전(`1.0.0`)/업데이트 manifest URL |
| `update_checker.py` | 시작 시 업데이트 manifest 확인 |
| `proxy_server.py` | 로컬 필터링 프록시 (HTTP + HTTPS CONNECT, 인증서 불필요) |
| `system_proxy.py` | 레지스트리 시스템 프록시 설정/원복, 변경 감시(2초) |
| `config_store.py` | `config.json` 저장 (PBKDF2 비밀번호 해시 + HMAC 무결성 + 파일 ACL) |
| `tray.py` | 트레이(알림 영역) 아이콘 – ctypes 로 구현, 좌클릭=열기 / 우클릭 메뉴=열기·종료 |
| `startup.py` | Windows 시작 시 자동 실행 – 작업 스케줄러(로그온, 최고 권한) 등록/해제 |
| `watchdog.py` | 강제 종료 감시 – 비정상 종료 시 EduGuard 재실행 |
| `scripts/build_msi.py` | PyInstaller + WiX 기반 MSI 빌드 스크립트 |
| `assets/eduguard.ico`, `eduguard.png` | 앱 아이콘 (`icon_guide.jpg` 가이드에서 생성). 창/작업표시줄/대화상자에 적용 |
| `test_eduguard.py` | 단위/통합 테스트 |

> exe 로 빌드할 때: `pyinstaller --onefile --noconsole --icon assets/eduguard.ico --add-data "assets;assets" main.py`

## 환경설정 (하단 `⚙ 환경설정`, 부모님 비밀번호 필요)
설정 창은 `시작`, `차단`, `네트워크`, `로그`, `보안` 탭으로 나뉩니다.

| 탭 | 항목 | 기본값 | 설명 |
|---|---|---|---|
| 시작 | Windows 시작 시 자동 실행 | 꺼짐 | 작업 스케줄러에 "최고 권한/로그온 시"로 등록 (관리자 권한으로 실행 중일 때만 변경 가능) |
| 시작 | 시작할 때 트레이 아이콘으로 실행 | 꺼짐 | 창을 띄우지 않고 트레이에서만 동작 |
| 시작 | 창 닫기(X) 시 트레이로 숨기기 | 켜짐 | 끄면 X = 프로그램 종료(비밀번호 확인) |
| 시작 | 트레이 알림 표시 | 켜짐 | 차단 시작/해제, 일시 해제, 트레이 숨김 안내 알림 |
| 시작 | 강제 종료 감시(워치독) | 꺼짐 | 작업 관리자 등으로 강제 종료되면 EduGuard를 다시 실행. 정상 종료는 재실행하지 않음 |
| 차단 | 프로그램 시작 시 자동 차단 시작 | 켜짐 | 스케줄이 꺼져 있을 때 시작 직후 차단 |
| 차단 | 시간대 스케줄 | 꺼짐 | 지정 시간대 안에서는 자동 차단, 밖에서는 자동 해제 |
| 차단 | 원격지원모드 허용 | 꺼짐 | TeamViewer 연결 유지를 위해 `teamviewer`, `dyngate` 도메인을 추가 허용 |
| 차단 | 일시 해제 기본 시간 | 30분 | `⏱ 일시 해제` 버튼의 기본 시간 |
| 차단 | 차단 페이지 문구 | 허용된 학습 사이트가 아닙니다. | HTTP 차단 페이지에 표시되는 문구 |
| 네트워크 | 필터 프록시 포트 | 8899 | 다음 차단 시작부터 적용 |
| 네트워크 | 프록시 설정 감시 주기 | 2초 | 시스템 프록시를 임의로 끄면 복구하는 주기 |
| 로그 | 반복 로그 숨기기 | 켜짐 | 같은 허용/차단 로그가 짧은 시간 안에 반복되면 다시 표시하지 않음 |
| 로그 | 반복 로그 숨김 시간 | 30초 | 중복 로그 판단 시간. 5 ~ 600초 |
| 로그 | 접속 로그 파일 저장 | 꺼짐 | `logs/eduguard-YYYYMMDD.log` 파일로 저장 |
| 로그 | 로그 보관 기간 | 30일 | 오래된 접속 로그 자동 삭제 |
| 로그 | 설정 변경 이력 기록 | 켜짐 | `logs/settings-history.log` 에 설정/키워드/차단 변경 기록 |
| 보안 | 비밀번호 실패 허용 횟수 | 5회 | 초과 시 잠금 |
| 보안 | 비밀번호 잠금 시간 | 30초 | 실패 횟수 초과 후 잠금 시간 |

> 자동 실행 작업은 **등록 시 사용된 관리자 계정**의 로그온에 묶입니다. 자녀(표준) 계정 로그온 때도 실행되는지는 PC 환경에서 꼭 확인하세요.
> 원격지원모드는 테스트/원격 유지 목적의 우회 허용입니다. 현재는 TeamViewer용 `teamviewer`, `dyngate` 계열 도메인만 추가로 열리므로 설치·점검이 끝나면 끄는 것을 권장합니다.
> StarPlayer/Axissoft 연동을 위해 `localhost.axissoft.co.kr`은 신뢰 로컬 호스트로 예외 허용합니다. 다른 루프백/사설 IP 접속은 계속 차단됩니다.

## 수동 테스트 순서
1. `python main.py` → 비밀번호 설정 → 자동으로 차단 시작
2. 브라우저에서 `https://www.google.com` → 차단 페이지/연결 오류, `https://www.ebs.co.kr` → 접속됨
3. 설정 > 네트워크 > 프록시에서 `127.0.0.1:8899` 확인. 직접 끄면 2초 안에 다시 켜짐
4. 차단 해제/키워드 추가·삭제/종료 시 비밀번호 창이 뜨는지 확인
5. 환경설정에서 트레이 시작/스케줄/로그/워치독 옵션을 켠 뒤 재시작·로그온·강제 종료 동작 확인

## 권장 운영 방법 (중요)
- **자녀 계정은 반드시 '표준 사용자'** 로 두세요. 관리자 계정이면 프로그램 종료·설정 변경·레지스트리 수정이 모두 가능해 우회됩니다.
- 프로그램은 `C:\EduGuard` 처럼 자녀가 쓰기 불가능한 폴더에 설치하세요. (사용자 폴더에 두면 파일 삭제 가능)
- 프로그램이 강제 종료돼도 프록시 설정은 죽은 포트를 가리키므로 **인터넷은 계속 차단**(fail-closed)됩니다. 복구는 `--restore`.
- 부팅 시 자동 실행은 환경설정에서 켜세요. 직접 작업 스케줄러를 만드는 것보다 현재 설치 경로와 exe/script 실행 방식을 자동으로 반영합니다.

## 한계 (키워드 방식의 본질적 특성)
- 키워드는 **부분 문자열 매칭**이라 `ebs` 는 `webserver.com`, `forebs.net` 같은 도메인도 허용합니다. 가능하면 `ebsi.co.kr` 처럼 구체적으로 등록하세요. (현재 기본 preset 은 요청 사양 그대로)
- `cloudfront` 는 무관한 사이트들의 CDN 도메인(`*.cloudfront.net`)도 모두 허용합니다.
- 프록시를 쓰지 않는 앱/VPN/Tor/브라우저 내 별도 프록시 설정은 막지 못합니다. 완전 차단이 필요하면 Windows 방화벽의 아웃바운드 규칙을 병행하세요.
- HTTPS 는 도메인만 확인하며 URL 경로·내용은 검사하지 않습니다.
