# Room Agent - 문제 해결 기록

개발 중 발생했던 문제들과 해결 방법을 정리한 문서입니다.

---

## 1. 회의실 가용성 오감지

### 증상
- 11-3, 11-4 회의실이 실제로는 비어있는데 "예약됨"으로 표시
- 13:00-14:00 조회 시 13:30-14:30 예약이 충돌로 감지됨

### 원인
다우오피스 캘린더 DOM 구조에서 **다음 날 예약 데이터**가 현재 날짜 뷰에 포함됨.

```html
<!-- 현재 날짜 예약: left 0~100% -->
<div data-matrix-item style="left: 56.25%; width: 4.16%">...</div>

<!-- 다음 날 예약: left 100% 이상 -->
<div data-matrix-item style="left: 156.25%; width: 4.16%">...</div>
```

### 해결
`get_room_reservations()` 함수에서 CSS `left` 값이 100% 이상인 항목 필터링:

```python
# src/daou_automation.py
style = await item.get_attribute("style") or ""
left_match = re.search(r"left:\s*([\d.]+)%", style)
if left_match:
    left_pct = float(left_match.group(1))
    if left_pct >= 100:
        # 다음 날 데이터 - 건너뜀
        continue
```

---

## 2. 날짜 네비게이션 실패

### 증상
- URL 파라미터 `?date=2026-02-04`가 무시됨
- 항상 오늘 날짜의 데이터만 표시

### 원인
다우오피스가 URL 파라미터를 무시하고 자체 상태 관리 사용.

### 해결
버튼 클릭으로 날짜 이동:

```python
# src/daou_automation.py
async def _navigate_to_date(self, date: str) -> None:
    # 날짜 차이 계산 후 버튼 클릭
    for i in range(abs(days_diff)):
        if days_diff > 0:
            btn = self.page.locator(".btn_ic_next2").first  # 다음
        else:
            btn = self.page.locator(".btn_ic_prev2").first  # 이전
        await btn.click()
        await asyncio.sleep(0.5)
```

---

## 3. Google Calendar 인증 - 인코딩 오류

### 증상
```
UnicodeEncodeError: 'cp949' codec can't encode character '\u26a0' in position 0
```

### 원인
Windows 콘솔의 기본 인코딩(cp949)이 이모지 문자를 지원하지 않음.

### 해결
이모지를 ASCII 문자로 대체:

| 이전 | 이후 |
|------|------|
| ✅ | [OK] |
| ❌ | [X] |
| ⚠️ | [!] |

```python
# google_auth.py
print("[OK] 토큰 저장 완료")  # ✅ 대신
print("[X] 연결 실패")        # ❌ 대신
print("[!] 경고 메시지")      # ⚠️ 대신
```

---

## 4. Google Calendar API 권한 오류

### 증상
```
HttpError 403: Request had insufficient authentication scopes.
insufficientPermissions
```

### 원인
`calendar.events` 스코프로는 `calendarList().list()` API 호출 불가.

### 해결
연결 테스트를 `events().list()`로 변경:

```python
# google_auth.py - 변경 전
calendar_list = service.calendarList().list().execute()  # 권한 오류

# google_auth.py - 변경 후
events_result = service.events().list(
    calendarId="primary",
    timeMin=now,
    maxResults=5
).execute()  # calendar.events 스코프로 가능
```

---

## 5. 예약 시간이 30분만 적용됨

### 증상
- 1시간 예약 요청해도 30분만 예약됨
- 다우오피스에서 확인하면 종료 시간이 시작+30분

### 원인
더블클릭으로 예약 폼 열면 기본 30분 설정. 종료 시간 필드를 수정하지 않음.

### 해결
종료 시간 필드(`#endTime`)를 찾아서 드롭다운에서 선택:

```python
# src/daou_automation.py
end_time_input = self.page.locator("#endTime")
if await end_time_input.is_visible():
    current_end = await end_time_input.input_value()

    if current_end != end_time:
        await end_time_input.click()
        await end_time_input.fill(end_time)
        await asyncio.sleep(0.5)

        # 드롭다운에서 옵션 선택
        dropdown_option = self.page.locator(f"li:has-text('{end_time}')").first
        if await dropdown_option.is_visible():
            await dropdown_option.click()
```

---

## 6. 채널 메시지 전송 실패

### 증상
```
SlackApiError: channel_not_found
```
예약 완료 메시지가 채널에 전송되지 않음.

### 원인
1. 버튼 값에 `channel_id`가 포함되지 않음
2. 모달 → 예약 처리 과정에서 원래 채널 정보 유실

### 해결
버튼 생성 시 `channel_id` 포함:

```python
# src/slack_handler.py - 버튼 생성
buttons.append({
    "value": json.dumps({
        "room_id": room.id,
        "date": request.date,
        "start_time": request.start_time,
        "end_time": request.end_time,
        "purpose": request.purpose,
        "channel_id": channel_id,  # 추가
    })
})

# 모달 metadata에 전달
"private_metadata": json.dumps({
    "room_id": room_id,
    "date": date,
    "channel_id": channel_id,  # 추가
})
```

---

## 7. "확인 중" 메시지 지연

### 증상
`/qw` 명령어 입력 후 "확인 중" 메시지가 늦게 표시됨.

### 원인
자연어 파싱 완료 후에 메시지 전송.

```python
# 이전 흐름
1. /qw 명령 수신
2. 백그라운드 작업 시작
3. Gemini API 파싱 (1-2초)
4. "확인 중" 메시지 전송  # 늦음
```

### 해결
명령어 수신 즉시 응답:

```python
# src/slack_handler.py
def handle_room_command(ack, command, client, respond):
    ack()

    # 즉시 응답
    respond(
        text="🔍 회의실 확인 중...",
        response_type="ephemeral"
    )

    # 이후 백그라운드 처리
    _executor.submit(_process_room_command, ...)
```

---

## 8. Playwright triple_click 오류

### 증상
```
AttributeError: 'Locator' object has no attribute 'triple_click'
```

### 원인
Playwright Locator에는 `triple_click` 메서드가 없음.

### 해결
`click(click_count=3)` 사용:

```python
# 변경 전
await element.triple_click()

# 변경 후
await element.click(click_count=3)
```

---

## 디버깅 팁

### 1. 스크린샷 확인
예약 실패 시 `debug/` 폴더의 스크린샷 확인:
- `reserve_before_click.png` - 클릭 전
- `reserve_after_click.png` - 클릭 후
- `reserve_before_save.png` - 저장 전
- `reserve_after_save.png` - 저장 후

### 2. HTML 덤프 확인
DOM 구조 분석이 필요할 때:
```python
html_content = await self.page.content()
(debug_dir / "page.html").write_text(html_content, encoding="utf-8")
```

### 3. 브라우저 UI 표시
`config.yaml`에서 headless 모드 비활성화:
```yaml
defaults:
  headless: false  # 브라우저 창 표시
```

### 4. 로그 레벨 조정
상세 로그 확인:
```python
logging.getLogger().setLevel(logging.DEBUG)
```

---

## 9. start_time이 None으로 전달됨

### 증상
```
TypeError: strptime() argument 1 must be str, not None
```

### 원인
LLM이 필수 필드(`start_time`, `end_time`)에 `null` 값을 반환.

### 해결
필수 필드 검증 강화:

```python
# src/llm_parser.py
required_fields = ["date", "start_time", "end_time"]
for field in required_fields:
    if field not in data or data[field] is None:
        raise ValueError(f"필수 필드 누락 또는 null: {field}")
    if not isinstance(data[field], str) or not data[field].strip():
        raise ValueError(f"필수 필드가 비어있음: {field}")
```

---

## 10. 세션 만료 자동 재로그인

### 기능
세션 만료 시 수동으로 `login.py`를 실행할 필요 없이 자동 처리됩니다.

### 흐름
1. 회의실 조회/예약 중 세션 만료 감지
2. Slack DM 알림: "🔐 그룹웨어 세션이 만료되었습니다"
3. 브라우저 창 자동 열림 (headless=False)
4. 사용자가 로그인 완료 (3분 내)
5. 새 쿠키 자동 저장 → 원래 작업 자동 재개

### 타임아웃 발생 시
3분 내 로그인하지 않으면 "자동 재로그인 실패" 에러 발생.
이 경우 수동으로 `python login.py` 실행 필요.

---

## 11. Google Calendar 체크박스

### 기능
예약 모달에서 "Google Calendar에 등록" 체크박스로 캘린더 등록 여부 선택.

### 설정 저장
- 체크 상태가 변경되면 `config.yaml`의 `google_calendar.enabled` 값이 자동 업데이트
- 다음 예약 시에도 변경된 설정이 유지됨

### 체크박스 액션 핸들러
Slack이 체크박스 클릭 이벤트를 전송하므로 핸들러가 필요:

```python
# src/slack_handler.py
_app.action("calendar_checkbox")(handle_calendar_checkbox)

def handle_calendar_checkbox(ack, body, logger):
    """캘린더 체크박스 클릭 - ack만 하면 됨"""
    ack()
```

핸들러가 없으면 404 "unhandled request" 오류 발생.

---

## 12. 반복 예약 기능

### 기능
`/qw 매주 월요일 10시 팀미팅` 형식으로 반복 예약을 지원합니다.

### 지원 범위
- 요일: 월~금 (토, 일 미지원)
- 기본 종료일: 요청한 달의 마지막 날

### 모달에서 변경 가능
- **반복 요일**: 월~금 드롭다운 선택
- **반복 시작일**: datepicker (오늘 이후만 가능)
- **반복 종료일**: datepicker

### LLM 파싱 예시
```
입력: "매주 월요일 10시 팀미팅"
→ is_recurring: true
→ recurring_day: "MON"
→ date: 다음 월요일 날짜 (예: 2026-03-02)
→ recurring_until: 월말 (예: 2026-02-28)
```

### Google Calendar RRULE
반복 예약 시 Calendar에 등록하면 RRULE 형식으로 저장됩니다:
```
RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260228T235959Z
```

### 완료 메시지 형식
```
✅ 반복 회의실 예약 완료
시간: 매주 월요일 10:00-11:00
기간: 2026-02-24 ~ 2026-02-28
장소: 11층 회의실 11-3 (10인)
목적: 팀미팅
```

---

## 13. 점심 후 스터디 예약

### 기능
점심시간 기반으로 스터디/미팅을 자동 반복 예약합니다.

### 사용법
```
/qw 점심 11:45 스터디
/qw 점심 12:00 팀미팅
/qw 밥 12:30 스터디
```

### 시간 계산 규칙
- 점심시간: 1시간 (입력 시간 ~ +1시간)
- 스터디 시작: 점심 종료 후, 30분 단위로 올림
- 스터디 시간: 1시간

| 입력 | 점심 종료 | 올림 후 시작 | 예약 시간 |
|------|----------|-------------|----------|
| 점심 11:45 | 12:45 | 13:00 | 13:00~14:00 |
| 점심 12:00 | 13:00 | 13:00 | 13:00~14:00 |
| 점심 12:15 | 13:15 | 13:30 | 13:30~14:30 |
| 점심 12:30 | 13:30 | 13:30 | 13:30~14:30 |

### 자동 반복 예약
- 점심 키워드 사용 시 자동으로 반복 예약으로 처리
- 기본값: 오늘 요일, 이번 달 말일까지
- 모달에서 모든 설정 변경 가능

### 특정 월 지정
```
/qw 4월 점심 1시 스터디
/qw 3월 밥 12:30 팀미팅
```

- 월을 지정하면 해당 월의 첫 번째 해당 요일을 시작일로 설정
- 종료일은 해당 월 말일로 자동 설정

### 모달에서 변경 가능 항목
| 항목 | 설명 |
|------|------|
| 반복 요일 | 월~금 드롭다운 선택 |
| 반복 시작일 | 오늘 이후만 선택 가능 |
| 반복 종료일 | datepicker로 선택 |
| Google Calendar | 체크박스로 등록 여부 선택 |

### 인식 키워드
`점심`, `밥`, `밥시간`, `점심후`, `밥후`

---

## 자주 발생하는 문제

| 증상 | 원인 | 해결 |
|------|------|------|
| 로그인 페이지로 리다이렉트 | 쿠키 만료 | 자동 재로그인 동작 (DM 확인 후 브라우저에서 로그인) |
| 회의실을 찾을 수 없음 | row_key 불일치 | config.yaml의 row_key 확인 |
| Calendar 등록 안 됨 | 체크박스 해제 | 예약 모달에서 체크 |
| 봇 응답 없음 | Socket Mode 연결 끊김 | room-agent 재시작 |
| channel_not_found | 봇이 채널에 없음 | `/invite @room-agent` |
| start_time None 오류 | LLM 파싱 실패 | 입력 문구 다시 확인 |
| 404 unhandled request | 체크박스 핸들러 누락 | `handle_calendar_checkbox` 등록 확인 |
