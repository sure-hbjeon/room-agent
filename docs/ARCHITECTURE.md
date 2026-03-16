# Room Agent - 시스템 아키텍처

## 개요

Room Agent는 Slack 슬래시 명령어를 통해 다우오피스 회의실 예약을 자동화하는 시스템입니다.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│    Slack    │────▶│  Room Agent │────▶│  다우오피스  │────▶│   Google    │
│  /qw 명령  │◀────│   (Python)  │◀────│  (Playwright)│     │  Calendar   │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
```

## 시스템 흐름

### 1. 회의실 조회/예약 흐름

```
사용자                 Slack                Room Agent              다우오피스           Google Calendar
  │                     │                      │                       │                     │
  │  /qw 내일 2시     │                      │                       │                     │
  │────────────────────▶│                      │                       │                     │
  │                     │   Socket Mode        │                       │                     │
  │                     │─────────────────────▶│                       │                     │
  │                     │                      │                       │                     │
  │                     │  "확인 중..." 응답    │                       │                     │
  │◀────────────────────│◀─────────────────────│                       │                     │
  │                     │                      │                       │                     │
  │                     │                      │  Gemini API 파싱      │                     │
  │                     │                      │  (자연어 → JSON)      │                     │
  │                     │                      │                       │                     │
  │                     │                      │  Playwright 조회      │                     │
  │                     │                      │──────────────────────▶│                     │
  │                     │                      │◀──────────────────────│                     │
  │                     │                      │                       │                     │
  │                     │  회의실 버튼 표시     │                       │                     │
  │◀────────────────────│◀─────────────────────│                       │                     │
  │                     │                      │                       │                     │
  │  [11-3 예약] 클릭   │                      │                       │                     │
  │────────────────────▶│─────────────────────▶│                       │                     │
  │                     │                      │                       │                     │
  │                     │  모달 (회의명 입력)   │                       │                     │
  │◀────────────────────│◀─────────────────────│                       │                     │
  │                     │                      │                       │                     │
  │  "팀미팅" 입력       │                      │                       │                     │
  │────────────────────▶│─────────────────────▶│                       │                     │
  │                     │                      │                       │                     │
  │                     │  "예약 중..." DM     │                       │                     │
  │◀────────────────────│◀─────────────────────│                       │                     │
  │                     │                      │                       │                     │
  │                     │                      │  Playwright 예약      │                     │
  │                     │                      │──────────────────────▶│                     │
  │                     │                      │◀──────────────────────│                     │
  │                     │                      │                       │                     │
  │                     │                      │  Calendar 등록        │                     │
  │                     │                      │──────────────────────────────────────────▶│
  │                     │                      │◀──────────────────────────────────────────│
  │                     │                      │                       │                     │
  │                     │  채널: 완료 메시지    │                       │                     │
  │◀────────────────────│◀─────────────────────│                       │                     │
  │                     │  DM: 상세 확인       │                       │                     │
  │◀────────────────────│◀─────────────────────│                       │                     │
```

## 모듈 구조

```
room-agent/
├── src/
│   ├── main.py              # 진입점, 트레이 아이콘, 중복 실행 방지
│   ├── slack_handler.py     # Slack 이벤트 처리
│   ├── llm_parser.py        # 자연어 파싱 (Gemini)
│   ├── daou_automation.py   # 다우오피스 자동화 (Playwright)
│   ├── calendar_sync.py     # Google Calendar 연동
│   └── config.py            # 설정 관리
├── config.yaml              # 설정 파일
├── cookies.json             # 다우오피스 세션
├── credentials.json         # Google OAuth 클라이언트
├── token.json               # Google 인증 토큰
└── logs/                    # 로그 파일
```

### 모듈별 역할

| 모듈 | 역할 | 주요 의존성 |
|------|------|-------------|
| `main.py` | 앱 시작, 트레이 아이콘, 중복 실행 방지 | pystray |
| `slack_handler.py` | `/qw` 명령, 버튼/모달 처리 | slack_bolt |
| `llm_parser.py` | 자연어 → 구조화된 예약 정보 | google-generativeai |
| `daou_automation.py` | 회의실 조회/예약 자동화 | playwright |
| `calendar_sync.py` | 예약 일정 캘린더 등록 | google-api-python-client |
| `config.py` | YAML 설정 로드 및 관리 | pyyaml, pydantic |

## 핵심 데이터 구조

### ReservationRequest (예약 요청)

```python
@dataclass
class ReservationRequest:
    action: str          # "reserve" | "query"
    date: str            # "2026-02-04"
    start_time: str      # "14:00"
    end_time: str        # "15:00"
    duration_minutes: int
    preferred_room: str  # "11-3" (선택)
    purpose: str         # "팀미팅"
    time_specified: bool # 사용자가 시간을 명시했는지 여부

    @property
    is_query: bool       # action == "query"
    is_schedule_view: bool  # 시간 없이 조회만 (is_query and not time_specified)
```

### RoomAvailability (회의실 가용성)

```python
@dataclass
class RoomAvailability:
    room: Room           # 회의실 정보
    available: bool      # 가용 여부
    reservations: list   # 기존 예약 목록
    free_slots: list     # 빈 시간대 목록 [{"start": "09:00", "end": "12:00", "duration": 180}, ...]
```

### ReservationResult (예약 결과)

```python
@dataclass
class ReservationResult:
    success: bool
    room: Room
    date: str
    start_time: str
    end_time: str
    error_message: str   # 실패 시
```

## 회의실 우선순위

### 조회 모드
11층 회의실만 표시, ID 순서(1, 2, 3, 4)로 정렬:
```
11-1 (6인) → 11-2 (6인) → 11-3 (10인) → 11-4 (8인)
```

### 예약 모드
11층에 빈 회의실 없으면 다른 층 탐색:
```
Tier 1 (11층) ──▶ Tier 2 (10층) ──▶ Tier 3 (9층)
    │                  │                  │
    ▼                  ▼                  ▼
  11-1 (6인)        10-3 (5인)         9-1 (4인)
  11-2 (6인)        10-4 (5인)         9-3 (4인)
  11-3 (10인)       10-6 (5인)         9-4 (4인)
  11-4 (8인)
```

## 외부 서비스 연동

### Slack (Socket Mode)

```
┌──────────────────────────────────────────────────┐
│                   Slack App                       │
├──────────────────────────────────────────────────┤
│  Socket Mode (WebSocket)                         │
│  ├── /qw 슬래시 명령어                          │
│  ├── 버튼 인터랙션 (회의실 선택)                   │
│  └── 모달 제출 (회의명 입력)                      │
├──────────────────────────────────────────────────┤
│  Bot Permissions                                 │
│  ├── chat:write (메시지 전송)                     │
│  ├── commands (슬래시 명령어)                     │
│  └── im:write (DM 전송)                          │
└──────────────────────────────────────────────────┘
```

### Gemini API (자연어 파싱)

```
입력: "내일 오후 2시부터 3시까지 팀미팅"
      ↓
┌─────────────────────────────────────┐
│          Gemini 2.5 Flash           │
│  ┌─────────────────────────────┐   │
│  │   System Prompt:            │   │
│  │   - 날짜/시간 파싱 규칙      │   │
│  │   - JSON 출력 형식          │   │
│  │   - 오전/오후 해석 규칙      │   │
│  └─────────────────────────────┘   │
└─────────────────────────────────────┘
      ↓
출력: {
  "date": "2026-02-05",
  "start_time": "14:00",
  "end_time": "15:00",
  "purpose": "팀미팅"
}
```

### Playwright (브라우저 자동화)

```
┌─────────────────────────────────────────────────┐
│              Playwright Browser                  │
├─────────────────────────────────────────────────┤
│  1. 쿠키 기반 인증 (cookies.json)               │
│  2. 층/날짜별 페이지 네비게이션                  │
│  3. DOM 파싱으로 기존 예약 확인                  │
│  4. 더블클릭으로 예약 폼 열기                    │
│  5. 폼 입력 (시간, 목적, 참석인원)               │
│  6. 확인 버튼 클릭                              │
└─────────────────────────────────────────────────┘
```

### Google Calendar API

```
┌─────────────────────────────────────────────────┐
│           Google Calendar API                    │
├─────────────────────────────────────────────────┤
│  Scope: calendar.events                          │
│  ├── events.insert() - 일정 등록                 │
│  ├── events.delete() - 일정 삭제                 │
│  └── events.list() - 일정 조회                   │
├─────────────────────────────────────────────────┤
│  OAuth 2.0 Flow                                  │
│  credentials.json → 브라우저 인증 → token.json   │
└─────────────────────────────────────────────────┘
```

## 에러 처리

### 에러 유형별 처리

| 에러 | 처리 방식 |
|------|----------|
| 파싱 실패 | 사용자에게 예시 안내 |
| 회의실 불가 | 다른 층 회의실 제안 |
| 다우오피스 로그인 만료 | **자동 재로그인** (DM 알림 → 브라우저 열림 → 재시도) |
| 예약 충돌 | 에러 메시지 + 대안 제시 |
| Calendar 등록 실패 | 예약은 성공, 로그에 경고 |

### 자동 재로그인 흐름

```
세션 만료 감지
       │
       ▼
Slack DM: "🔐 그룹웨어 세션이 만료되었습니다"
       │
       ▼
브라우저 자동 열림 (headless=False)
       │
       ▼
사용자 로그인 대기 (최대 3분)
       │
       ├── 로그인 성공 → 쿠키 저장 → 원래 작업 재시도
       │
       └── 타임아웃 → "자동 재로그인 실패" 에러
```

### 재시도 정책

```python
defaults:
  timeout: 30      # 각 작업 타임아웃 (초)
  max_retries: 3   # 최대 재시도 횟수
```

## 메시지 흐름

### 조회 모드 (시간 미지정)

```
/qw 내일

채널 (ephemeral):  🔍 회의실 확인 중...
채널 (ephemeral):  📋 2026-02-05 11층 회의실 현황
                   11-1 (6인)
                   빈 시간: 09:00-12:00(3시간), 14:00-18:00(4시간)
                   11-2 (6인)
                   빈 시간: 09:00-18:00(9시간)
                   ...
```

### 조회 + 예약 모드 (시간 지정)

```
/qw 내일 2시

채널 (ephemeral):  🔍 회의실 확인 중...
채널 (ephemeral):  📋 2026-02-05 11층 회의실 현황
                   ... (빈 시간대 표시)
                   ────────────────
                   14:00-15:00 예약 가능:
                   [11-1 예약] [11-2 예약] [11-3 예약]
```

### 예약 모달

```
┌─────────────────────────────────────┐
│          회의실 예약                 │
├─────────────────────────────────────┤
│  11-3                               │
│  2026-02-04 14:00-15:00             │
│                                     │
│  회의 이름                          │
│  ┌─────────────────────────────┐    │
│  │ 팀미팅                      │    │
│  └─────────────────────────────┘    │
│                                     │
│  ☑ Google Calendar에 등록           │
│    (체크 상태는 config.yaml에 저장)  │
│                                     │
│        [취소]  [예약]               │
└─────────────────────────────────────┘
```

### 예약 성공 시

```
DM:               ⏳ 11-3 예약 중... (팀미팅)
채널 (public):    ✅ 회의실 예약 완료
                  시간: 2026-02-04 14:00-15:00
                  장소: 11층 회의실 11-3
                  목적: 팀미팅
DM:               ✅ 예약 완료: 11층 회의실 11-3
                  📅 Google Calendar에 등록됨  (체크한 경우만)
```

### 실패 시

```
DM:               ⚠️ room-agent 오류
                  예약 실패: 해당 시간에 이미 예약이 있습니다
```

## 설정 구조 (config.yaml)

```yaml
slack:
  app_token: "xapp-..."     # Socket Mode
  bot_token: "xoxb-..."     # Bot OAuth

gemini:
  api_key: "AIza..."
  model: "gemini-2.5-flash"

daou:
  url: "https://gw.suresofttech.com/..."

google_calendar:
  enabled: true
  calendar_id: "primary"

room_priority:
  - tier: 1
    label: "11층"
    floor_id: 27
    rooms:
      - id: "11-3"
        row_key: 85      # DOM 식별자
        capacity: 10
```

## 로깅

### 로그 파일 위치

```
logs/qw-agent-YYYY-MM-DD.log
```

### 로그 레벨

- `INFO`: 정상 동작 (명령 수신, 예약 성공)
- `WARNING`: 비정상 상황 (Calendar 등록 실패 등)
- `ERROR`: 오류 발생 (예약 실패, 연결 오류)
- `DEBUG`: 상세 디버깅 (DOM 파싱 결과 등)

## 보안 고려사항

### 민감 정보 보호

- `config.yaml`: API 토큰 (git 제외)
- `cookies.json`: 다우오피스 세션 (git 제외)
- `credentials.json`: Google OAuth (git 제외)
- `token.json`: Google 인증 (git 제외)

### 네트워크 보안

- Slack: Socket Mode (WebSocket, 인바운드 포트 불필요)
- 다우오피스: HTTPS
- Google API: OAuth 2.0 + HTTPS

## 오류 분석 프로세스

### Claude Code + Playwright 방식

#### 핵심 개념: 사전 계획된 디버깅 포인트

Claude Code는 **코드 작성 시점**에 디버깅을 위한 스크린샷/로그 저장 코드를 미리 삽입합니다.
이후 오류 발생 시, 저장된 아티팩트를 분석하여 원인을 파악합니다.

```
[코드 작성 단계 - AI가 디버깅 포인트 설계]

async def reserve_room(...):
    # 디버깅 포인트 1: 클릭 전 상태
    await page.screenshot(path="debug/reserve_before_click.png")

    await row.dblclick(...)

    # 디버깅 포인트 2: 클릭 후 폼 상태
    await page.screenshot(path="debug/reserve_after_click.png")

    # 디버깅 포인트 3: DOM 구조 저장
    html = await page.content()
    Path("debug/reserve_form.html").write_text(html)

    # 종료 시간 설정
    await end_time_input.fill(end_time)

    # 디버깅 포인트 4: 저장 직전 상태
    await page.screenshot(path="debug/reserve_before_save.png")
```

#### 오류 발생 시 분석 흐름

```
[사용자: "예약이 30분만 됐어요"]
           │
           ▼
[AI: 스크린샷 분석 요청]
    Read("debug/reserve_before_save.png")
           │
           ▼
[AI가 이미지를 시각적으로 분석]
    "스크린샷을 보니 종료 시간 필드가 '13:30'으로
     되어있습니다. '16:00'이어야 하는데 드롭다운
     선택이 제대로 안 된 것 같습니다."
           │
           ▼
[AI: HTML 덤프 분석]
    Grep("endTime", "debug/reserve_form.html")
           │
           ▼
[AI: 원인 파악]
    "HTML을 보니 #endTime 필드가 드롭다운 형식입니다.
     fill()만 하면 안 되고, 드롭다운 옵션을 클릭해야 합니다."
           │
           ▼
[AI: 코드 수정]
    # 드롭다운 옵션 클릭 코드 추가
    dropdown_option = page.locator(f"li:has-text('{end_time}')")
    await dropdown_option.click()
```

#### 실제 사례: 종료 시간 30분 문제

1. **문제 발생**: 1시간 예약 요청했는데 30분만 예약됨

2. **스크린샷 확인** (AI가 Read 도구로 이미지 분석):
   ```
   reserve_before_save.png 분석 결과:
   - 시작 시간: 15:00 ✓
   - 종료 시간: 15:30 ✗ (16:00이어야 함)
   - 드롭다운 메뉴가 열려있는 상태
   ```

3. **HTML 분석** (AI가 Grep 도구로 검색):
   ```html
   <input id="endTime" name="end_time" data-prev="15:30" type="text">
   <!-- 드롭다운 옵션들 -->
   <li>15:30(30분)</li>
   <li>16:00(1시간)</li>  <!-- 이걸 클릭해야 함 -->
   ```

4. **원인 파악**: `fill()`만으로는 드롭다운 선택 안 됨

5. **수정**: 드롭다운 옵션 클릭 로직 추가

---

### Computer Use 방식

#### 핵심 개념: 실시간 화면 관찰 및 조작

Computer Use는 **실행 시점**에 실시간으로 화면을 보면서 조작합니다.
사람이 컴퓨터를 사용하는 것과 동일한 방식입니다.

```
[실행 단계 - AI가 실시간으로 화면 관찰]

1. AI: 화면 스크린샷 촬영 (실시간)
2. AI: "회의실 예약 페이지가 보입니다. 11-3 회의실 행을 더블클릭합니다."
3. AI: 마우스 이동 → 더블클릭
4. AI: 화면 스크린샷 촬영 (실시간)
5. AI: "예약 폼이 열렸습니다. 종료 시간이 13:30으로 되어있네요. 16:00으로 변경합니다."
6. AI: 종료 시간 필드 클릭 → 드롭다운에서 16:00 선택
7. AI: 화면 스크린샷 촬영 (실시간)
8. AI: "16:00으로 변경되었습니다. 확인 버튼을 클릭합니다."
```

#### 오류 발생 시 분석 흐름

```
[실행 중 오류 감지]
    AI가 실시간으로 화면을 보고 있음
           │
           ▼
[AI: 즉시 상황 인식]
    "화면을 보니 에러 메시지가 표시되었습니다:
     '해당 시간에 이미 예약이 있습니다'"
           │
           ▼
[AI: 즉시 대응]
    "다른 시간대를 시도해보겠습니다.
     14:00-15:00으로 변경합니다."
           │
           ▼
[AI: 재시도]
    시간 필드 수정 → 다시 예약 시도
```

---

### 두 방식 상세 비교

| 구분 | Claude Code + Playwright | Computer Use |
|------|--------------------------|--------------|
| **화면 인식 시점** | 사후 (저장된 스크린샷) | 실시간 |
| **조작 방식** | 코드로 DOM 직접 조작 | 마우스/키보드 시뮬레이션 |
| **디버깅 준비** | 미리 스크린샷 저장 코드 작성 | 필요 없음 (실시간 관찰) |
| **속도** | 빠름 (직접 DOM 조작) | 느림 (화면 인식 + 마우스 이동) |
| **안정성** | 높음 (선택자 기반) | 중간 (화면 변화에 민감) |
| **유연성** | 낮음 (예상된 흐름만) | 높음 (예외 상황 즉시 대응) |
| **디버깅 정보** | 코드 라인, 스택 트레이스 | 화면 상태만 |

#### 각 방식이 적합한 상황

**Claude Code + Playwright 적합:**
- 반복적인 자동화 작업
- 안정적인 UI 구조
- 빠른 실행 속도 필요
- 상세한 에러 로그 필요

**Computer Use 적합:**
- 일회성 작업
- UI가 자주 변경되는 경우
- 예외 상황이 많은 경우
- 사람처럼 유연한 대응 필요

---

### Claude Code 디버깅 전략

#### 1. 스크린샷 저장 포인트 설계

```python
# 주요 액션 전후로 스크린샷 저장
SCREENSHOT_POINTS = {
    "before_click": "클릭 전 - 올바른 요소를 찾았는지 확인",
    "after_click": "클릭 후 - 예상한 반응이 있는지 확인",
    "before_save": "저장 전 - 입력값이 올바른지 확인",
    "after_save": "저장 후 - 성공/실패 확인",
    "on_error": "에러 시 - 에러 메시지 캡처",
}
```

#### 2. HTML 덤프 저장 시점

```python
# DOM 구조 파악이 필요한 시점에 저장
async def save_html_for_debug(page, filename, reason):
    """
    저장 시점:
    - 새로운 페이지/모달이 열렸을 때
    - 예상한 요소를 찾지 못했을 때
    - 선택자가 작동하지 않을 때
    """
    html = await page.content()
    Path(f"debug/{filename}").write_text(html, encoding="utf-8")
    logger.info(f"HTML 저장: {filename} - {reason}")
```

#### 3. 단계별 검증 로그

```python
# 각 단계에서 상태를 로그로 기록
logger.info(f"[1단계] 페이지 이동: {url}")
logger.info(f"[2단계] 회의실 행 발견: row_key={room.row_key}")
logger.info(f"[3단계] 더블클릭 완료, 폼 대기 중...")
logger.info(f"[4단계] 종료 시간 입력: {current} -> {end_time}")
logger.info(f"[5단계] 저장 버튼 클릭")
logger.info(f"[결과] 예약 성공: {room.id}")
```

### 원인 분석 실전 가이드

#### 1. 스택 트레이스 분석
```python
# 에러 예시
File "H:\room-agent\src\daou_automation.py", line 256, in _is_time_slot_available
    start = datetime.strptime(start_time, "%H:%M")
TypeError: strptime() argument 1 must be str, not None
```

**분석 순서:**
1. 에러 타입 확인: `TypeError` → 타입 불일치
2. 위치 확인: `daou_automation.py:256` → `_is_time_slot_available` 함수
3. 변수 확인: `start_time`이 `None`
4. 호출 추적: 어디서 `None`이 전달되었는지 역추적

#### 2. 디버그 아티팩트 종류

| 아티팩트 | 위치 | 용도 | AI 분석 방법 |
|----------|------|------|--------------|
| 로그 파일 | `logs/*.log` | 실행 흐름 추적 | Grep으로 검색 |
| 스크린샷 | `debug/*.png` | 시각적 상태 확인 | Read로 이미지 분석 |
| HTML 덤프 | `debug/*.html` | DOM 구조 분석 | Grep으로 요소 검색 |
| 파싱 결과 | 로그 내 JSON | LLM 출력 검증 | 로그에서 추출 |

#### 3. 단계별 검증 포인트

```
[Slack 입력]
    │
    ▼ 로그: "[user_id] /qw 명령: {입력값}"
[LLM 파싱]
    │
    ▼ 로그: "파싱 결과: {JSON}"
    │   → start_time, end_time, date 값 확인
[다우오피스 접속]
    │
    ▼ 스크린샷: floor_*.png
    │   → 올바른 층/날짜로 이동했는지 확인
[예약 시도]
    │
    ▼ 스크린샷: reserve_*.png
    │   → 폼이 열렸는지, 값이 입력되었는지 확인
[결과]
    │
    ▼ 로그: "예약 성공/실패"
```

#### 4. 흔한 오류 패턴과 원인

| 오류 패턴 | 가능한 원인 | 확인 방법 |
|-----------|-------------|-----------|
| `None` 타입 에러 | LLM 파싱 실패 | 로그에서 파싱 결과 확인 |
| `TimeoutError` | 페이지 로딩 지연 | 스크린샷으로 상태 확인 |
| `channel_not_found` | 채널 ID 누락 | 버튼 값에 channel_id 포함 여부 |
| 30분만 예약됨 | 종료 시간 미설정 | reserve_before_save.png 확인 |
| 다음 날 데이터 충돌 | CSS left >= 100% | HTML 덤프에서 left 값 확인 |

### 코드 기반 디버깅 명령어

```bash
# 로그 실시간 확인
Get-Content logs\room-agent-*.log -Wait -Tail 50

# 특정 에러 검색
Select-String -Path logs\*.log -Pattern "ERROR"

# 최근 스크린샷 확인
ls debug\*.png | Sort-Object LastWriteTime -Descending | Select-Object -First 5
```

### HTML/DOM 분석 예시

```python
# 문제: 회의실 버튼 선택자가 작동 안 함

# 1. HTML 덤프 저장 (코드에 추가)
html = await page.content()
Path("debug/page.html").write_text(html, encoding="utf-8")

# 2. 특정 요소 검색 (Grep 사용)
# Grep: pattern="btn_ic_next", path="debug/page.html"

# 3. 구조 분석
# - class 이름 확인
# - data-* 속성 확인
# - 부모/자식 관계 확인
```

### 문제 해결 체크리스트

```
□ 에러 메시지의 정확한 내용 확인
□ 스택 트레이스에서 파일:라인 확인
□ 해당 코드 읽기 (Read tool)
□ 변수 값 추적 (어디서 None/잘못된 값이 왔는지)
□ 관련 로그 확인
□ 스크린샷/HTML 덤프 확인 (UI 관련 문제)
□ 비슷한 이슈가 TROUBLESHOOTING.md에 있는지 확인
□ 수정 후 테스트
```
