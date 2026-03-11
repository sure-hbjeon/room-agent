"""자연어 파싱 모듈 (Gemini API 사용)"""

import json
import logging
from calendar import monthrange
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

from google import genai

from .config import get_config

logger = logging.getLogger(__name__)


# 요일 코드 매핑
DAY_CODE_MAP = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4
}
DAY_LABELS = {
    "MON": "월", "TUE": "화", "WED": "수", "THU": "목", "FRI": "금"
}


def get_default_recurring_until() -> str:
    """현재 월의 마지막 날 반환"""
    today = datetime.now()
    _, last_day = monthrange(today.year, today.month)
    return f"{today.year}-{today.month:02d}-{last_day:02d}"


def get_next_weekday(day_code: str) -> str:
    """다음 특정 요일 날짜 반환 (MON, TUE, ...)

    Args:
        day_code: 요일 코드 (MON, TUE, WED, THU, FRI)

    Returns:
        다음 해당 요일의 날짜 (YYYY-MM-DD)
    """
    target_day = DAY_CODE_MAP.get(day_code.upper())
    if target_day is None:
        raise ValueError(f"유효하지 않은 요일 코드: {day_code}")

    today = datetime.now()
    days_ahead = target_day - today.weekday()
    if days_ahead <= 0:  # 오늘이거나 지났으면 다음 주
        days_ahead += 7
    next_date = today + timedelta(days=days_ahead)
    return next_date.strftime("%Y-%m-%d")


@dataclass
class ReservationRequest:
    """예약 요청 데이터"""
    action: str  # "reserve" 또는 "query"
    date: str  # YYYY-MM-DD
    start_time: str  # HH:MM
    end_time: str  # HH:MM
    duration_minutes: int
    preferred_room: Optional[str]  # 회의실 ID (예: "11-3") 또는 None
    purpose: Optional[str]  # 이용목적 (조회 시 None 가능)
    time_specified: bool = True  # 사용자가 시간을 명시했는지 여부
    # 반복 예약 필드
    is_recurring: bool = False  # 반복 예약 여부
    recurring_day: Optional[str] = None  # 요일: "MON", "TUE", "WED", "THU", "FRI"
    recurring_until: Optional[str] = None  # 반복 종료일: YYYY-MM-DD

    @property
    def is_query(self) -> bool:
        return self.action == "query"

    @property
    def is_schedule_view(self) -> bool:
        """시간 없이 조회만 하는 경우"""
        return self.action == "query" and not self.time_specified

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "date": self.date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_minutes": self.duration_minutes,
            "preferred_room": self.preferred_room,
            "purpose": self.purpose,
            "time_specified": self.time_specified,
            "is_recurring": self.is_recurring,
            "recurring_day": self.recurring_day,
            "recurring_until": self.recurring_until,
        }


SYSTEM_PROMPT = """당신은 회의실 예약/조회 요청을 파싱하는 도우미입니다.
사용자의 자연어 입력을 분석하여 다음 형식의 JSON으로 변환해야 합니다.

출력 형식:
{
  "action": "reserve" 또는 "query",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "end_time": "HH:MM",
  "duration_minutes": 숫자,
  "preferred_room": "회의실ID" 또는 null,
  "purpose": "이용목적" 또는 null,
  "time_specified": true 또는 false,
  "is_recurring": true 또는 false,
  "recurring_day": "MON" 또는 "TUE" 또는 "WED" 또는 "THU" 또는 "FRI" 또는 null,
  "recurring_until": "YYYY-MM-DD" 또는 null (특정 월 지정 시 해당 월 말일)
}

time_specified 구분:
- true: 사용자가 시간을 명시한 경우 ("11시", "2시~3시", "오후 2시" 등)
- false: 시간 없이 날짜만 언급한 경우 ("내일 회의실", "오늘 빈 방", "내일 조회" 등)

action 구분:
- "query": 조회/확인 요청 (빈 회의실 조회, 예약 현황 확인 등)
  - 키워드: "조회", "확인", "빈 회의실", "비어있", "가능한", "어디 비어" 등
  - 예: "내일 11시 빈 회의실", "오늘 오후 회의실 조회", "10시에 어디 비어있어?"
  - 시간 없는 조회: "내일 회의실", "오늘 빈 방" → time_specified: false
- "reserve": 예약 요청 (이용목적이 있거나 예약 의도가 명확한 경우)
  - 예: "내일 10~12 팀미팅", "오늘 14시 11-3 면접"

시간 파싱 규칙:
- "11시", "11시에" → start: "11:00", end: "12:00", time_specified: true
- "오후 2시" → start: "14:00", end: "15:00", time_specified: true
- "10~12", "10-12", "10:00-12:00" → start: "10:00", end: "12:00", time_specified: true
- 시간 없음 ("내일 회의실") → start: "09:00", end: "18:00", time_specified: false
- "14:00-15:30" → start: "14:00", end: "15:30"

점심 시간 기반 예약 (점심 직후 스터디/미팅 - 자동 반복 예약):
- 규칙: 점심시간 + 1시간 후, 30분 단위로 올림하여 시작 시간 결정
- 예약은 30분 단위(00분, 30분)로만 가능하므로 올림 처리 필수
- "점심 11:45" → 12:45 → 30분 올림 → start: "13:00", end: "14:00"
- "점심 12:00" → 13:00 → 이미 정각 → start: "13:00", end: "14:00"
- "점심 12:15" → 13:15 → 30분 올림 → start: "13:30", end: "14:30"
- "점심 12:30" → 13:30 → 이미 30분 → start: "13:30", end: "14:30"
- "점심", "밥", "밥시간", "점심후", "밥후" 키워드 인식
- 중요: 점심 키워드가 있으면 자동으로 반복 예약으로 처리 (is_recurring: true)
- recurring_day는 오늘 요일로 설정 (월=MON, 화=TUE, 수=WED, 목=THU, 금=FRI)

특정 월 지정 시:
- "4월 점심 1시 스터디" → date: 4월 첫 번째 오늘요일 날짜, recurring_until: 4월 말일
- "3월 밥 12:30 팀미팅" → date: 3월 첫 번째 오늘요일 날짜, recurring_until: 3월 말일
- 월이 지정되면 해당 월의 첫 번째 해당 요일을 date로, 해당 월 말일을 recurring_until로 설정
- 예: 오늘이 목요일이고 "4월 점심 1시 스터디" → date: "2026-04-02" (4월 첫 목요일), recurring_until: "2026-04-30", recurring_day: "THU"

월 미지정 시 (기본):
- date는 오늘 날짜로 설정
- recurring_until은 이번 달 말일로 설정
- 예: 오늘이 목요일이고 "점심 11:45 스터디" → is_recurring: true, recurring_day: "THU", date: 오늘, start: "13:00", end: "14:00", purpose: "스터디"

중요 - 모호한 시간 처리 (오전/오후 명시 없는 경우):
- 회의실 예약은 업무 시간(09:00~18:00)에 이루어집니다.
- 절대로 01:00~06:00 시간대로 해석하지 마세요. 새벽 시간에는 회의실 예약을 하지 않습니다.
- "1시", "한시", "1시에" → start: "13:00" (반드시 오후 1시 = 13시로 해석)
- "2시", "두시" → start: "14:00" (반드시 오후 2시 = 14시로 해석)
- "3시", "세시" → start: "15:00" (반드시 오후 3시 = 15시로 해석)
- "4시", "네시" → start: "16:00" (반드시 오후 4시 = 16시로 해석)
- "5시", "다섯시" → start: "17:00" (반드시 오후 5시 = 17시로 해석)
- "6시", "여섯시" → start: "18:00" (오후 6시 = 18시로 해석)
- "7시", "일곱시" → start: "07:00" (오전 7시)
- "8시", "여덟시" → start: "08:00" (오전 8시)
- "9시", "아홉시" → start: "09:00" (오전 9시, 업무 시작)
- "10시", "열시" → start: "10:00" (오전 10시)
- "11시", "열한시" → start: "11:00" (오전 11시)
- "12시", "열두시" → start: "12:00" (정오 12시)
- "오전 1시" 처럼 명시적으로 "오전"이 붙은 경우만 01:00~06:00으로 해석

날짜 파싱 규칙:
- "내일" → 오늘 날짜 + 1일
- "오늘" → 오늘 날짜
- "모레" → 오늘 날짜 + 2일
- "다음주 월요일" → 다음 주 월요일 날짜
- "1/30", "01/30" → 해당 연도의 1월 30일

회의실 ID 형식:
- 11층: "11-1", "11-2", "11-3", "11-4"
- 10층: "10-3", "10-4", "10-6"
- 9층: "9-3", "9-4"
- 2층: "2-소교육장"

반복 예약 파싱 규칙:
- "매주 월요일" → is_recurring: true, recurring_day: "MON", action: "reserve"
- "매주 화요일" → is_recurring: true, recurring_day: "TUE"
- "매주 수요일" → is_recurring: true, recurring_day: "WED"
- "매주 목요일" → is_recurring: true, recurring_day: "THU"
- "매주 금요일" → is_recurring: true, recurring_day: "FRI"
- 반복이 아닌 경우 → is_recurring: false, recurring_day: null
- 토요일, 일요일 반복 예약은 지원하지 않습니다.

반복 예약 날짜(date) 설정:
- "매주 월요일"인 경우 → date는 다음 월요일 날짜로 설정
- "매주 화요일"인 경우 → date는 다음 화요일 날짜로 설정
- 예: 오늘이 2026-02-24(월)이고 "매주 수요일 10시"라면 → date: "2026-02-26"

반복 예약 예시:
- "매주 월요일 10시 팀미팅" → is_recurring: true, recurring_day: "MON", date: 다음 월요일, start_time: "10:00", end_time: "11:00", purpose: "팀미팅"
- "매주 금요일 2시~3시 스탠드업" → is_recurring: true, recurring_day: "FRI", date: 다음 금요일, start_time: "14:00", end_time: "15:00", purpose: "스탠드업"

주의사항:
- 반드시 유효한 JSON만 출력하세요.
- 다른 설명 없이 JSON만 출력하세요.
- 시간은 24시간 형식으로 출력하세요.
- duration_minutes는 시작과 종료 시간의 차이(분)입니다.
- 조회(query) 시 purpose는 null로 설정하세요.
"""


def parse_reservation(user_input: str) -> ReservationRequest:
    """자연어 입력을 예약 요청으로 파싱"""
    config = get_config()

    # Gemini API 클라이언트 생성
    client = genai.Client(api_key=config.gemini.api_key)

    today = datetime.now()
    user_prompt = f"""오늘 날짜: {today.strftime('%Y-%m-%d')} ({today.strftime('%A')})

사용자 입력: {user_input}

위 입력을 JSON으로 변환하세요."""

    logger.info(f"LLM 파싱 요청: {user_input}")

    response = client.models.generate_content(
        model=config.gemini.model,
        contents=user_prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
        ),
    )
    response_text = response.text.strip()

    logger.debug(f"LLM 응답: {response_text}")

    # JSON 파싱
    try:
        # 코드 블록으로 감싸진 경우 처리
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            json_lines = []
            in_json = False
            for line in lines:
                if line.startswith("```") and not in_json:
                    in_json = True
                    continue
                elif line.startswith("```") and in_json:
                    break
                elif in_json:
                    json_lines.append(line)
            response_text = "\n".join(json_lines)

        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON 파싱 실패: {e}")
        raise ValueError(f"LLM 응답을 파싱할 수 없습니다: {response_text}")

    # 필수 필드 검증 (키 존재 + 값이 None이 아닌지)
    required_fields = ["date", "start_time", "end_time"]
    for field in required_fields:
        if field not in data or data[field] is None:
            raise ValueError(f"필수 필드 누락 또는 null: {field}")
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError(f"필수 필드가 비어있음: {field}")

    # action 기본값
    if "action" not in data:
        data["action"] = "reserve" if data.get("purpose") else "query"

    # duration_minutes 계산 (없는 경우)
    if "duration_minutes" not in data:
        start = datetime.strptime(data["start_time"], "%H:%M")
        end = datetime.strptime(data["end_time"], "%H:%M")
        duration = (end - start).seconds // 60
        data["duration_minutes"] = duration

    # time_specified 기본값 (없으면 True로 가정)
    time_specified = data.get("time_specified", True)

    # 반복 예약 필드
    is_recurring = data.get("is_recurring", False)
    recurring_day = data.get("recurring_day")

    # 반복 예약인 경우 recurring_until 설정
    recurring_until = None
    if is_recurring and recurring_day:
        # LLM이 반환한 recurring_until 사용 (특정 월 지정 시)
        recurring_until = data.get("recurring_until")
        if not recurring_until:
            # 없으면 기본값 (이번 달 말일)
            recurring_until = get_default_recurring_until()
        # 반복 예약은 항상 reserve 액션
        data["action"] = "reserve"

    return ReservationRequest(
        action=data["action"],
        date=data["date"],
        start_time=data["start_time"],
        end_time=data["end_time"],
        duration_minutes=data["duration_minutes"],
        preferred_room=data.get("preferred_room"),
        purpose=data.get("purpose"),
        time_specified=time_specified,
        is_recurring=is_recurring,
        recurring_day=recurring_day,
        recurring_until=recurring_until,
    )


def format_time_range(start_time: str, end_time: str) -> str:
    """시간 범위 포맷팅"""
    return f"{start_time}-{end_time}"


def format_datetime(date: str, start_time: str, end_time: str) -> str:
    """날짜/시간 포맷팅"""
    return f"{date} {start_time}-{end_time}"
