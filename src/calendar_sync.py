"""Google Calendar 연동 모듈"""

import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import get_config, get_base_path, Room

logger = logging.getLogger(__name__)

# Google Calendar API 스코프
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def get_credentials() -> Credentials:
    """Google OAuth 인증 정보 획득"""
    config = get_config()
    base_path = get_base_path()

    credentials_path = base_path / config.google_calendar.credentials_file
    token_path = base_path / config.google_calendar.token_file

    creds = None

    # 기존 토큰 로드
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            logger.info("기존 토큰 로드 성공")
        except Exception as e:
            logger.warning(f"토큰 로드 실패: {e}")

    # 토큰 갱신 또는 새로 획득
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("토큰 갱신 중...")
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"토큰 갱신 실패: {e}")
                creds = None

        if not creds:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Google 인증 파일을 찾을 수 없습니다: {credentials_path}\n"
                    "Google Cloud Console에서 OAuth 클라이언트 ID를 생성하고 "
                    "credentials.json을 다운로드하세요."
                )

            logger.info("새 OAuth 인증 시작 (브라우저 열림)")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # 토큰 저장
        with open(token_path, 'w') as token_file:
            token_file.write(creds.to_json())
            logger.info(f"토큰 저장: {token_path}")

    return creds


def get_calendar_service():
    """Google Calendar API 서비스 객체 반환"""
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)
    return service


def add_calendar_event(
    room: Room,
    date: str,
    start_time: str,
    end_time: str,
    purpose: str,
    attendees: Optional[list[str]] = None,
    recurrence: Optional[dict] = None
) -> Optional[str]:
    """Google Calendar에 예약 일정 추가

    Args:
        room: 회의실 정보
        date: 날짜 (YYYY-MM-DD)
        start_time: 시작 시간 (HH:MM)
        end_time: 종료 시간 (HH:MM)
        purpose: 이용목적 (일정 제목)
        attendees: 참석자 이메일 목록 (선택)
        recurrence: 반복 일정 정보 (선택)
            - day: 요일 코드 ("MON", "TUE", "WED", "THU", "FRI")
            - until: 종료일 (YYYY-MM-DD)

    Returns:
        생성된 이벤트 ID 또는 None (실패 시)
    """
    config = get_config()

    try:
        service = get_calendar_service()

        # 시간대 설정 (한국)
        timezone = "Asia/Seoul"

        # datetime 문자열 생성
        start_datetime = f"{date}T{start_time}:00"
        end_datetime = f"{date}T{end_time}:00"

        # 이벤트 데이터
        event = {
            "summary": purpose,
            "location": f"{room.name}",
            "description": f"회의실: {room.name} ({room.capacity}인)",
            "start": {
                "dateTime": start_datetime,
                "timeZone": timezone,
            },
            "end": {
                "dateTime": end_datetime,
                "timeZone": timezone,
            },
        }

        # 반복 일정 추가 (있는 경우)
        if recurrence and recurrence.get("day") and recurrence.get("until"):
            # RRULE 형식: FREQ=WEEKLY;BYDAY=MO;UNTIL=20260228T235959Z
            day_code = recurrence["day"][:2].upper()  # "MON" -> "MO"
            until_date = recurrence["until"].replace("-", "")  # "2026-02-28" -> "20260228"
            rrule = f"RRULE:FREQ=WEEKLY;BYDAY={day_code};UNTIL={until_date}T235959Z"
            event["recurrence"] = [rrule]
            logger.info(f"반복 일정 RRULE: {rrule}")

        # 참석자 추가 (있는 경우)
        if attendees:
            event["attendees"] = [{"email": email} for email in attendees]

        # 이벤트 생성
        created_event = service.events().insert(
            calendarId=config.google_calendar.calendar_id,
            body=event
        ).execute()

        event_id = created_event.get("id")
        event_link = created_event.get("htmlLink")

        logger.info(f"캘린더 이벤트 생성 완료: {event_id}")
        logger.debug(f"이벤트 링크: {event_link}")

        return event_id

    except HttpError as e:
        logger.error(f"Google Calendar API 오류: {e}")
        return None
    except Exception as e:
        logger.error(f"캘린더 이벤트 생성 실패: {e}")
        return None


def delete_calendar_event(event_id: str) -> bool:
    """Google Calendar 이벤트 삭제

    Args:
        event_id: 삭제할 이벤트 ID

    Returns:
        성공 여부
    """
    config = get_config()

    try:
        service = get_calendar_service()

        service.events().delete(
            calendarId=config.google_calendar.calendar_id,
            eventId=event_id
        ).execute()

        logger.info(f"캘린더 이벤트 삭제 완료: {event_id}")
        return True

    except HttpError as e:
        logger.error(f"이벤트 삭제 실패: {e}")
        return False


def check_calendar_conflict(
    date: str,
    start_time: str,
    end_time: str
) -> list[dict]:
    """지정 시간에 기존 일정이 있는지 확인

    Args:
        date: 날짜 (YYYY-MM-DD)
        start_time: 시작 시간 (HH:MM)
        end_time: 종료 시간 (HH:MM)

    Returns:
        충돌하는 이벤트 목록
    """
    config = get_config()

    try:
        service = get_calendar_service()

        # 시간 범위 설정
        time_min = f"{date}T{start_time}:00+09:00"
        time_max = f"{date}T{end_time}:00+09:00"

        events_result = service.events().list(
            calendarId=config.google_calendar.calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        events = events_result.get("items", [])
        return events

    except HttpError as e:
        logger.error(f"캘린더 조회 실패: {e}")
        return []
