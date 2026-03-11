"""설정 관리 모듈"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class SlackConfig:
    app_token: str
    bot_token: str


@dataclass
class GeminiConfig:
    api_key: str
    model: str = "gemini-2.0-flash"


@dataclass
class DaouConfig:
    url: str


@dataclass
class GoogleCalendarConfig:
    credentials_file: str
    token_file: str
    calendar_id: str
    enabled: bool = False


@dataclass
class DefaultsConfig:
    attendees: str = "-"
    timeout: int = 30
    max_retries: int = 3
    headless: bool = False


@dataclass
class Room:
    id: str
    name: str
    capacity: int
    row_key: int = 0  # 다우오피스 캘린더 row key


@dataclass
class RoomTier:
    tier: int
    label: str
    floor_id: int = 0  # 다우오피스 층 ID (URL용)
    rooms: list[Room] = field(default_factory=list)


@dataclass
class Config:
    slack: SlackConfig
    gemini: GeminiConfig
    daou: DaouConfig
    google_calendar: GoogleCalendarConfig
    defaults: DefaultsConfig
    room_priority: list[RoomTier] = field(default_factory=list)

    def get_all_rooms(self) -> list[Room]:
        """모든 회의실을 우선순위 순으로 반환"""
        rooms = []
        for tier in self.room_priority:
            rooms.extend(tier.rooms)
        return rooms

    def get_room_by_id(self, room_id: str) -> Optional[Room]:
        """ID로 회의실 찾기"""
        for tier in self.room_priority:
            for room in tier.rooms:
                if room.id == room_id:
                    return room
        return None

    def get_tier_for_room(self, room_id: str) -> Optional[RoomTier]:
        """회의실이 속한 Tier 찾기"""
        for tier in self.room_priority:
            for room in tier.rooms:
                if room.id == room_id:
                    return tier
        return None


def get_base_path() -> Path:
    """실행 파일 또는 스크립트 기준 경로 반환"""
    if getattr(sys, 'frozen', False):
        # PyInstaller로 패키징된 경우
        return Path(sys.executable).parent
    else:
        # 일반 Python 스크립트 실행
        return Path(__file__).parent.parent


def load_config(config_path: Optional[str] = None) -> Config:
    """설정 파일 로드"""
    if config_path is None:
        config_path = get_base_path() / "config.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    # Slack 설정
    slack = SlackConfig(
        app_token=data['slack']['app_token'],
        bot_token=data['slack']['bot_token'],
    )

    # Gemini 설정
    gemini = GeminiConfig(
        api_key=data['gemini']['api_key'],
        model=data['gemini'].get('model', 'gemini-2.0-flash'),
    )

    # 다우오피스 설정
    daou = DaouConfig(
        url=data['daou']['url'],
    )

    # Google Calendar 설정
    gc_data = data['google_calendar']
    google_calendar = GoogleCalendarConfig(
        credentials_file=gc_data['credentials_file'],
        token_file=gc_data.get('token_file', 'token.json'),
        calendar_id=gc_data['calendar_id'],
        enabled=gc_data.get('enabled', False),
    )

    # 기본값 설정
    defaults_data = data.get('defaults', {})
    defaults = DefaultsConfig(
        attendees=defaults_data.get('attendees', '-'),
        timeout=defaults_data.get('timeout', 30),
        max_retries=defaults_data.get('max_retries', 3),
        headless=defaults_data.get('headless', False),
    )

    # 회의실 우선순위
    room_priority = []
    for tier_data in data.get('room_priority', []):
        rooms = [
            Room(
                id=r['id'],
                name=r['name'],
                capacity=r['capacity'],
                row_key=r.get('row_key', 0),
            )
            for r in tier_data.get('rooms', [])
        ]
        room_priority.append(RoomTier(
            tier=tier_data['tier'],
            label=tier_data['label'],
            floor_id=tier_data.get('floor_id', 0),
            rooms=rooms,
        ))

    return Config(
        slack=slack,
        gemini=gemini,
        daou=daou,
        google_calendar=google_calendar,
        defaults=defaults,
        room_priority=room_priority,
    )


# 전역 설정 객체 (싱글톤)
_config: Optional[Config] = None


def get_config() -> Config:
    """전역 설정 객체 반환"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config() -> Config:
    """설정 다시 로드"""
    global _config
    _config = load_config()
    return _config


def update_config_value(key_path: str, value) -> bool:
    """설정 파일의 특정 값 업데이트

    Args:
        key_path: 점으로 구분된 키 경로 (예: "google_calendar.enabled")
        value: 새로운 값

    Returns:
        성공 여부
    """
    config_path = get_base_path() / "config.yaml"

    try:
        # 기존 파일 읽기 (원본 형식 유지)
        with open(config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        # 중첩된 키 찾아서 업데이트
        keys = key_path.split('.')
        current = data
        for key in keys[:-1]:
            current = current[key]
        current[keys[-1]] = value

        # 파일 저장
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # 메모리 설정도 업데이트
        reload_config()
        return True

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"설정 업데이트 실패: {e}")
        return False
