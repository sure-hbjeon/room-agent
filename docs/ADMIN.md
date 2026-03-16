# Room Agent - 관리자 가이드

이 문서는 room-agent 설치, 설정, 유지보수를 담당하는 관리자를 위한 가이드입니다.

## 초기 설정

### 1. Slack App 설정

#### 방법 A: Manifest로 한 번에 생성 (권장)

프로젝트 루트의 `slack-app-manifest.json`을 사용하면 모든 설정이 자동 적용됩니다.

1. [Slack API](https://api.slack.com/apps) 접속
2. **Create New App** > **From a manifest**
3. Workspace 선택
4. **JSON** 탭 선택 → `slack-app-manifest.json` 내용 붙여넣기
5. **Create** 클릭
6. **Install to Workspace** 클릭
7. 토큰 복사:
   - **Settings > Basic Information > App-Level Tokens** > Generate Token (scope: `connections:write`)
   - `xapp-` 로 시작하는 토큰 → `config.yaml`의 `slack.app_token`에 입력
   - **OAuth & Permissions** 페이지에서 `xoxb-` 로 시작하는 토큰 → `config.yaml`의 `slack.bot_token`에 입력

#### 방법 B: 수동 설정

manifest 없이 직접 설정하는 방법입니다.

1. [Slack API](https://api.slack.com/apps) 접속
2. **Create New App** > **From scratch**
3. App Name: `room-agent`, Workspace 선택

**Socket Mode 활성화:**
- **Settings > Socket Mode** > Enable Socket Mode
- Token Name: `room-agent-socket` > Generate
- `xapp-` 로 시작하는 토큰 복사 → `config.yaml`의 `slack.app_token`에 입력

**Bot Token 설정:**
- **OAuth & Permissions > Scopes > Bot Token Scopes** 추가:
  - `chat:write` - 메시지 전송
  - `commands` - 슬래시 명령어
  - `im:write` - DM 전송
- **Install to Workspace** 클릭
- `xoxb-` 로 시작하는 토큰 복사 → `config.yaml`의 `slack.bot_token`에 입력

**Slash Command 등록:**
- **Features > Slash Commands > Create New Command**
  - Command: `/qw`
  - Description: `회의실 예약`
  - Usage Hint: `내일 2시 팀미팅`

**Event Subscriptions (선택):**
- **Features > Event Subscriptions** > Enable Events
- **Subscribe to bot events** 추가:
  - `app_mention` (멘션 응답용)

### 2. Google Cloud 설정 (Calendar 연동)

1. [Google Cloud Console](https://console.cloud.google.com) 접속
2. 프로젝트 생성 또는 선택
3. **API 및 서비스 > 라이브러리** > "Google Calendar API" 검색 > 사용 설정

#### OAuth 클라이언트 생성
- **API 및 서비스 > 사용자 인증 정보 > OAuth 클라이언트 ID 만들기**
- 애플리케이션 유형: **데스크톱 앱**
- 이름: `room-agent`
- `credentials.json` 다운로드 → room-agent 폴더에 저장

#### OAuth 동의 화면
- **API 및 서비스 > OAuth 동의 화면**
- User Type: **외부** (또는 내부 - 조직에 따라)
- 앱 이름: `room-agent`
- 사용자 지원 이메일, 개발자 연락처 입력
- **테스트 사용자** 섹션에 사용할 Google 계정 이메일 추가

### 3. Gemini API 설정

1. [Google AI Studio](https://aistudio.google.com/apikey) 접속
2. **Get API Key** > Create API key
3. 생성된 키 복사 → `config.yaml`의 `gemini.api_key`에 입력

## 설정 파일 (config.yaml)

```yaml
slack:
  app_token: "xapp-..."      # Socket Mode 토큰
  bot_token: "xoxb-..."      # Bot OAuth 토큰

gemini:
  api_key: "AIza..."         # Gemini API 키
  model: "gemini-2.5-flash"  # 사용 모델

daou:
  url: "https://gw.suresofttech.com/app/asset/27/list/reservation"

google_calendar:
  enabled: true              # Calendar 연동 기본값 (사용자가 모달에서 변경 가능)
  credentials_file: "credentials.json"
  token_file: "token.json"
  calendar_id: "primary"     # 또는 특정 캘린더 ID

# 참고: google_calendar.enabled 값은 사용자가 예약 모달에서
# "Google Calendar에 등록" 체크박스를 변경하면 자동 저장됩니다.

defaults:
  attendees: "-"             # 참석인원 기본값
  timeout: 30                # 타임아웃 (초)
  max_retries: 3             # 재시도 횟수
  headless: true             # 브라우저 숨김 모드

room_priority:               # 회의실 우선순위 (아래 참조)
```

### 회의실 설정

```yaml
room_priority:
  - tier: 1
    label: "11층"
    floor_id: 27            # 다우오피스 floor ID
    rooms:
      - id: "11-3"
        name: "회의실 11-3"
        capacity: 10
        row_key: 85         # 다우오피스 DOM row key
```

#### row_key 확인 방법
1. 다우오피스 회의실 예약 페이지 접속
2. 브라우저 개발자 도구 (F12) > Elements 탭
3. 회의실 행에서 `data-row-key` 속성 값 확인

## 인증 설정

### 다우오피스 로그인
```bash
python login.py
```
- 브라우저가 열리면 다우오피스에 로그인
- 로그인 완료 후 `cookies.json` 자동 생성
- 세션 만료 시 **자동 재로그인** 지원 (아래 참조)

#### 자동 재로그인 기능
세션 만료 시 수동으로 `login.py`를 실행할 필요 없이 자동 처리됩니다:

1. 회의실 조회/예약 중 세션 만료 감지
2. 사용자에게 Slack DM 알림: "🔐 그룹웨어 세션이 만료되었습니다"
3. 브라우저 창 자동 열림 (headless=False)
4. 사용자가 로그인 완료 (3분 내)
5. 새 쿠키 자동 저장 → 원래 작업 자동 재개

### Google Calendar 인증
```bash
python google_auth.py
```
- 브라우저에서 Google 로그인
- "확인되지 않은 앱" 경고 시: 고급 > 계속
- `token.json` 자동 생성
- 토큰은 자동 갱신됨

## 실행 및 관리

### 시작
```bash
python -m src.main
```
- 시스템 트레이에 아이콘 표시
- 중복 실행 자동 방지

### 로그 확인
```bash
# 오늘 로그
type logs\room-agent-2026-02-04.log

# 실시간 모니터링 (PowerShell)
Get-Content logs\room-agent-*.log -Wait -Tail 50
```

### 로그 레벨
- `INFO`: 일반 동작
- `WARNING`: 비정상 상황 (예약은 성공)
- `ERROR`: 오류 발생

## 트러블슈팅

### Slack 연결 안 됨
```
slack_bolt.App: Starting to receive messages...
```
이 메시지가 안 나오면:
- `app_token`, `bot_token` 확인
- Socket Mode 활성화 확인
- Slack App이 워크스페이스에 설치되었는지 확인

### 다우오피스 로그인 만료
```
로그인 페이지로 리다이렉트됨
```
해결: 자동 재로그인이 동작합니다.
- Slack DM 알림 확인
- 자동으로 열린 브라우저에서 로그인 (3분 내)
- 원래 작업이 자동으로 재개됩니다

자동 재로그인이 실패한 경우: `python login.py` 수동 실행

### Google Calendar 오류
```
HttpError 403: insufficientPermissions
```
해결: `token.json` 삭제 후 `python google_auth.py` 재실행

### 회의실을 찾을 수 없음
```
회의실 11-3을 찾을 수 없습니다
```
- `config.yaml`의 `row_key` 값 확인
- 다우오피스에서 회의실 구조가 변경되었을 수 있음

### 브라우저 디버깅
`config.yaml`에서:
```yaml
defaults:
  headless: false  # 브라우저 창 표시
```

## 보안 주의사항

### 민감한 파일 (절대 공유 금지)
- `config.yaml` - API 토큰 포함
- `credentials.json` - Google OAuth 클라이언트
- `token.json` - Google 인증 토큰
- `cookies.json` - 다우오피스 세션

### .gitignore 필수 항목
```
config.yaml
credentials.json
token.json
cookies.json
logs/
__pycache__/
```

## 배포 시 체크리스트

- [ ] Python 3.11+ 설치
- [ ] `pip install -r requirements.txt`
- [ ] Playwright 브라우저 설치: `playwright install chromium`
- [ ] `config.yaml` 설정 완료
- [ ] `credentials.json` 배치
- [ ] `python login.py` 실행 (다우오피스 로그인)
- [ ] `python google_auth.py` 실행 (Calendar 연동)
- [ ] `python -m src.main` 실행 확인
- [ ] Slack에서 `/qw 테스트` 명령 테스트

## 유지보수

### 정기 작업
- 로그 파일 정리 (logs/ 폴더)
- (다우오피스 쿠키는 자동 재로그인으로 갱신됨)

### 모니터링 포인트
- Slack 봇 응답 여부
- 다우오피스 로그인 상태 (자동 재로그인 동작 확인)
- Google Calendar 동기화 상태
