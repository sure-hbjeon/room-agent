"""Slack 명령어 및 인터랙션 처리 모듈"""

import json
import logging
import asyncio
import re
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.webhook import WebhookClient

from .config import get_config, get_base_path, Room, update_config_value
from .llm_parser import parse_reservation, ReservationRequest
from .daou_automation import check_rooms_availability, make_reservation, make_recurring_reservation, RoomAvailability, ReservationResult
from .calendar_sync import add_calendar_event

logger = logging.getLogger(__name__)

# 전역 변수
_app: Optional[App] = None
_executor = ThreadPoolExecutor(max_workers=2)

# 진행 중인 예약 요청 저장 (user_id -> ReservationRequest)
_pending_requests: dict[str, ReservationRequest] = {}


def create_app() -> App:
    """Slack 앱 생성"""
    global _app

    config = get_config()

    _app = App(token=config.slack.bot_token)

    # 핸들러 등록
    _app.command("/res")(handle_room_command)
    _app.action(re.compile(r"select_room_.+"))(handle_room_selection)
    _app.action("calendar_checkbox")(handle_calendar_checkbox)
    _app.view("reservation_modal")(handle_reservation_modal)

    return _app


def get_app() -> App:
    """Slack 앱 인스턴스 반환"""
    global _app
    if _app is None:
        _app = create_app()
    return _app


def handle_room_command(ack, command, client: WebClient, respond):
    """
    /res 슬래시 명령어 처리

    예: /res 내일 10~12 팀미팅
    """
    ack()  # 3초 내 응답 필수

    user_id = command["user_id"]
    user_input = command["text"].strip()
    channel_id = command["channel_id"]
    response_url = command["response_url"]

    logger.info(f"[{user_id}] /res 명령: {user_input}")

    if not user_input:
        respond(
            text=(
                "사용법:\n"
                "• 예약: `/res 내일 10~12 팀미팅`\n"
                "• 조회: `/res 내일 11시 빈 회의실`"
            ),
            response_type="ephemeral"
        )
        return

    # 즉시 응답 (사용자에게 처리 중임을 알림)
    respond(
        text=f"🔍 회의실 확인 중...",
        response_type="ephemeral"
    )

    # 비동기 처리 (Slack 3초 제한 회피)
    _executor.submit(
        _process_room_command,
        client, user_id, channel_id, user_input, response_url
    )


def _process_room_command(
    client: WebClient,
    user_id: str,
    channel_id: str,
    user_input: str,
    response_url: str
):
    """회의실 예약 명령 처리 (백그라운드)"""
    webhook = WebhookClient(response_url)

    try:
        # 1. 자연어 파싱
        try:
            request = parse_reservation(user_input)
            logger.info(f"파싱 결과: {request.to_dict()}")
        except Exception as e:
            logger.error(f"파싱 실패: {e}")
            webhook.send(
                text=f"입력을 이해할 수 없습니다. 다시 시도해주세요.\n예: `/res 내일 10~12 팀미팅`",
                response_type="ephemeral"
            )
            return

        # 요청 저장 (버튼 클릭 시 사용)
        _pending_requests[user_id] = request

        # 2. 회의실 가용성 확인

        # 세션 만료 시 알림 콜백
        def notify_relogin():
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text="🔐 그룹웨어 세션이 만료되었습니다.\n브라우저 창이 열리면 로그인해주세요. (3분 내)"
                )
            except Exception as e:
                logger.warning(f"재로그인 알림 DM 실패: {e}")

        # 비동기 함수 실행
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            availabilities = loop.run_until_complete(
                check_rooms_availability(request, on_relogin_needed=notify_relogin)
            )
        finally:
            loop.close()

        # 3. 결과에 따라 메시지 구성
        available_rooms = [a for a in availabilities if a.available]
        unavailable_rooms = [a for a in availabilities if not a.available]

        if request.is_query:
            # 조회 모드: 빈 회의실 목록 표시
            blocks = _build_query_result_message(
                request, available_rooms, unavailable_rooms, channel_id
            )
            webhook.send(
                blocks=blocks,
                text=f"{request.date} {request.start_time}-{request.end_time} 조회 결과",
                response_type="ephemeral"
            )
        elif available_rooms:
            # 예약 모드: 가용 회의실 버튼 표시
            blocks = _build_available_rooms_message(
                request, available_rooms, unavailable_rooms, channel_id
            )
            webhook.send(
                blocks=blocks,
                text=f"{request.date} {request.start_time}-{request.end_time} 예약 가능",
                response_type="ephemeral"
            )
        else:
            # 모든 회의실 불가
            blocks = _build_no_availability_message(request, unavailable_rooms)
            webhook.send(
                blocks=blocks,
                text="예약 가능한 회의실이 없습니다.",
                response_type="ephemeral"
            )

    except Exception as e:
        logger.error(f"명령 처리 실패: {e}", exc_info=True)
        webhook.send(
            text=f"오류가 발생했습니다: {e}",
            response_type="ephemeral"
        )


def _get_room_sort_key(avail):
    """회의실 ID에서 정렬 키 추출 (예: '11-3' -> (11, 3))"""
    room_id = avail.room.id
    parts = room_id.split("-")
    try:
        floor = int(parts[0])
        num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return (floor, num)
    except:
        return (99, room_id)


def _build_query_result_message(
    request: ReservationRequest,
    available_rooms: list[RoomAvailability],
    unavailable_rooms: list[RoomAvailability],
    channel_id: str
) -> list[dict]:
    """조회 결과 메시지 블록 생성 - 빈 시간대 포함"""
    config = get_config()

    # 11층과 10층 회의실 분리
    all_rooms = available_rooms + unavailable_rooms
    floor_11_rooms = sorted(
        [a for a in all_rooms if a.room.id.startswith("11-")],
        key=_get_room_sort_key
    )
    floor_10_available = sorted(
        [a for a in available_rooms if a.room.id.startswith("10-")],
        key=_get_room_sort_key
    )
    floor_11_available = [a for a in available_rooms if a.room.id.startswith("11-")]

    date_str = request.date

    # 빈 시간대 정보가 있는 회의실들 표시 (11층만)
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📋 *{date_str}* 11층 회의실 현황"
            }
        }
    ]

    # 11층 각 회의실의 빈 시간대 표시
    for avail in floor_11_rooms:
        # 빈 시간대 문자열 생성
        if avail.free_slots:
            free_times = []
            for slot in avail.free_slots:
                duration_hr = slot['duration'] // 60
                duration_min = slot['duration'] % 60
                if duration_hr > 0 and duration_min > 0:
                    dur_str = f"{duration_hr}시간{duration_min}분"
                elif duration_hr > 0:
                    dur_str = f"{duration_hr}시간"
                else:
                    dur_str = f"{duration_min}분"
                free_times.append(f"{slot['start']}-{slot['end']}({dur_str})")

            free_str = ", ".join(free_times[:3])  # 최대 3개
            if len(avail.free_slots) > 3:
                free_str += f" 외 {len(avail.free_slots) - 3}개"
            status = f"빈 시간: {free_str}"
        else:
            status = "종일 예약됨"

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{avail.room.id}* ({avail.room.capacity}인)\n{status}"
            }
        })

    # 요청 시간대에 가용한 회의실 버튼
    # 시간이 명시된 경우에만 예약 버튼 표시
    if request.time_specified:
        blocks.append({
            "type": "divider"
        })

        # 11층에 빈 회의실이 없고 10층에 있는 경우
        if not floor_11_available and floor_10_available:
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_11층 회의실이 모두 예약되어 있어 10층을 탐색했습니다_"}
                ]
            })
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{request.start_time}-{request.end_time}* 10층 예약 가능:"
                }
            })

            buttons = []
            for avail in floor_10_available[:4]:
                room = avail.room
                buttons.append({
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"{room.id} ({room.capacity}인)"
                    },
                    "action_id": f"select_room_{room.id}",
                    "value": json.dumps({
                        "room_id": room.id,
                        "date": request.date,
                        "start_time": request.start_time,
                        "end_time": request.end_time,
                        "purpose": "회의",
                        "channel_id": channel_id,
                        "is_recurring": request.is_recurring,
                        "recurring_day": request.recurring_day,
                        "recurring_until": request.recurring_until,
                    })
                })

            if buttons:
                blocks.append({
                    "type": "actions",
                    "elements": buttons
                })

        elif floor_11_available:
            # 11층에 빈 회의실 있는 경우
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{request.start_time}-{request.end_time}* 예약 가능:"
                }
            })

            buttons = []
            for avail in sorted(floor_11_available, key=_get_room_sort_key)[:4]:
                room = avail.room
                buttons.append({
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"{room.id} 예약"
                    },
                    "action_id": f"select_room_{room.id}",
                    "value": json.dumps({
                        "room_id": room.id,
                        "date": request.date,
                        "start_time": request.start_time,
                        "end_time": request.end_time,
                        "purpose": "회의",
                        "channel_id": channel_id,
                        "is_recurring": request.is_recurring,
                        "recurring_day": request.recurring_day,
                        "recurring_until": request.recurring_until,
                    })
                })

            if buttons:
                blocks.append({
                    "type": "actions",
                    "elements": buttons
                })

        else:
            # 11층, 10층 모두 빈 회의실 없는 경우
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_{request.start_time}-{request.end_time}에는 빈 회의실이 없습니다_"}
                ]
            })

    return blocks


def _build_available_rooms_message(
    request: ReservationRequest,
    available_rooms: list[RoomAvailability],
    unavailable_rooms: list[RoomAvailability],
    channel_id: str
) -> list[dict]:
    """가용 회의실 메시지 블록 생성"""
    config = get_config()

    # ID 순서로 정렬 (11-1, 11-2, 11-3, 11-4)
    available_rooms = sorted(available_rooms, key=_get_room_sort_key)

    # 불가한 층과 가용한 층 분석
    unavailable_tiers = set()
    for avail in unavailable_rooms:
        tier = config.get_tier_for_room(avail.room.id)
        if tier:
            unavailable_tiers.add(tier.label)

    available_tiers = set()
    for avail in available_rooms:
        tier = config.get_tier_for_room(avail.room.id)
        if tier:
            available_tiers.add(tier.label)

    # 메시지 헤더
    time_str = f"{request.date} {request.start_time}-{request.end_time}"

    # 특정 회의실 지정했는데 불가한 경우
    if request.preferred_room:
        preferred_available = any(
            a.room.id == request.preferred_room for a in available_rooms
        )
        if not preferred_available:
            header_text = f"📅 {time_str}\n{request.preferred_room}이 예약되어 있습니다.\n\n같은 시간 다른 회의실:"
        else:
            header_text = f"🔍 {time_str} 예약 가능:"
    else:
        # 11층이 불가하고 다른 층에서 찾은 경우
        only_unavailable_tiers = unavailable_tiers - available_tiers
        if only_unavailable_tiers and available_tiers:
            unavailable_str = ", ".join(sorted(only_unavailable_tiers))
            header_text = f"📅 {time_str}\n{unavailable_str} 회의실이 모두 예약되어 있습니다.\n\n같은 시간 다른 층:"
        else:
            header_text = f"🔍 {time_str} 예약 가능:"

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text}
        }
    ]

    # 회의실 버튼들 (층 정보 포함)
    buttons = []
    for avail in available_rooms[:4]:  # 최대 4개
        room = avail.room
        tier = config.get_tier_for_room(room.id)
        tier_label = tier.label if tier else ""

        # 다른 층일 경우 층 정보 표시
        button_text = f"{room.id} ({room.capacity}인)"
        if tier_label and tier_label not in ["11층"]:
            button_text = f"{tier_label} {room.id} ({room.capacity}인)"

        buttons.append({
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": button_text[:24]  # Slack 버튼 텍스트 제한
            },
            "action_id": f"select_room_{room.id}",
            "value": json.dumps({
                "room_id": room.id,
                "date": request.date,
                "start_time": request.start_time,
                "end_time": request.end_time,
                "purpose": request.purpose,
                "channel_id": channel_id,  # 완료 메시지 전송용
                "is_recurring": request.is_recurring,
                "recurring_day": request.recurring_day,
                "recurring_until": request.recurring_until,
            })
        })

    if buttons:
        blocks.append({
            "type": "actions",
            "elements": buttons
        })

    # 이용목적 표시
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"이용목적: {request.purpose}"}
        ]
    })

    return blocks


def _build_no_availability_message(
    request: ReservationRequest,
    unavailable_rooms: list[RoomAvailability]
) -> list[dict]:
    """예약 불가 메시지 블록 생성"""
    # 기존 예약 현황 표시
    reservation_info = []
    for avail in unavailable_rooms[:4]:
        room = avail.room
        if avail.reservations:
            times = [f"{r['start']}-{r['end']}" for r in avail.reservations[:3]]
            reservation_info.append(f"• {room.id}: {', '.join(times)}")

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📅 {request.date} {request.start_time}-{request.end_time}\n해당 시간에 예약 가능한 회의실이 없습니다."
            }
        }
    ]

    if reservation_info:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"현재 예약 현황:\n{chr(10).join(reservation_info)}"
            }
        })

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"이용목적: {request.purpose}"}
        ]
    })

    return blocks


def handle_room_selection(ack, action, body, client: WebClient):
    """회의실 선택 버튼 클릭 - 모달 열기"""
    ack()

    user_id = body["user"]["id"]
    trigger_id = body["trigger_id"]

    try:
        data = json.loads(action["value"])
        room_id = data["room_id"]
        date = data["date"]
        start_time = data["start_time"]
        end_time = data["end_time"]
        purpose = data.get("purpose") or ""
        # 버튼 값에서 채널 ID 가져오기 (완료 메시지 전송용)
        channel_id = data.get("channel_id") or body.get("channel", {}).get("id") or user_id
        # 반복 예약 정보
        is_recurring = data.get("is_recurring", False)
        recurring_day = data.get("recurring_day")
        recurring_until = data.get("recurring_until")

        logger.info(f"[{user_id}] 회의실 선택: {room_id} (채널: {channel_id}, 반복: {is_recurring})")

        config = get_config()

        # 입력 요소 구성 (기존 목적이 있으면 pre-fill)
        input_element = {
            "type": "plain_text_input",
            "action_id": "purpose_input",
            "placeholder": {"type": "plain_text", "text": "예: 팀 미팅, 고객 미팅"}
        }
        if purpose and purpose != "회의":
            input_element["initial_value"] = purpose

        # Google Calendar 체크박스 구성
        calendar_checkbox = {
            "type": "checkboxes",
            "action_id": "calendar_checkbox",
            "options": [
                {
                    "text": {"type": "plain_text", "text": "Google Calendar에 등록"},
                    "value": "add_to_calendar"
                }
            ]
        }
        # config.yaml의 enabled 값에 따라 기본 체크 상태 설정
        if config.google_calendar.enabled:
            calendar_checkbox["initial_options"] = [
                {
                    "text": {"type": "plain_text", "text": "Google Calendar에 등록"},
                    "value": "add_to_calendar"
                }
            ]

        # 모달 헤더 텍스트 (반복 예약 여부에 따라)
        from .llm_parser import DAY_LABELS
        if is_recurring and recurring_day:
            day_label = DAY_LABELS.get(recurring_day, recurring_day)
            header_text = f"*{room_id}*\n매주 {day_label}요일 {start_time}-{end_time}"
        else:
            header_text = f"*{room_id}*\n{date} {start_time}-{end_time}"

        # 모달 블록 구성
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": header_text
                }
            },
            {
                "type": "input",
                "block_id": "purpose_block",
                "element": input_element,
                "label": {"type": "plain_text", "text": "회의 이름"}
            },
        ]

        # 반복 예약인 경우 요일/시작일/종료일 선택 추가
        if is_recurring and recurring_day and recurring_until:
            from datetime import datetime
            today_str = datetime.now().strftime("%Y-%m-%d")

            # 시작일이 과거면 오늘로 보정
            if date < today_str:
                date = today_str

            # 요일 선택 드롭다운
            day_options = [
                {"text": {"type": "plain_text", "text": "월요일"}, "value": "MON"},
                {"text": {"type": "plain_text", "text": "화요일"}, "value": "TUE"},
                {"text": {"type": "plain_text", "text": "수요일"}, "value": "WED"},
                {"text": {"type": "plain_text", "text": "목요일"}, "value": "THU"},
                {"text": {"type": "plain_text", "text": "금요일"}, "value": "FRI"},
            ]
            # 현재 선택된 요일 찾기
            initial_day_option = next((opt for opt in day_options if opt["value"] == recurring_day), day_options[0])

            blocks.append({
                "type": "input",
                "block_id": "recurring_day_block",
                "element": {
                    "type": "static_select",
                    "action_id": "recurring_day_input",
                    "initial_option": initial_day_option,
                    "options": day_options
                },
                "label": {"type": "plain_text", "text": "반복 요일"}
            })

            # 시작일 datepicker
            blocks.append({
                "type": "input",
                "block_id": "recurring_start_block",
                "element": {
                    "type": "datepicker",
                    "action_id": "recurring_start_input",
                    "initial_date": date,
                    "placeholder": {"type": "plain_text", "text": "시작일 선택"}
                },
                "label": {"type": "plain_text", "text": "반복 시작일"}
            })
            # 종료일 datepicker
            blocks.append({
                "type": "input",
                "block_id": "recurring_until_block",
                "element": {
                    "type": "datepicker",
                    "action_id": "recurring_until_input",
                    "initial_date": recurring_until,
                    "placeholder": {"type": "plain_text", "text": "종료일 선택"}
                },
                "label": {"type": "plain_text", "text": "반복 종료일"}
            })

        # Calendar 체크박스 추가
        blocks.append({
            "type": "section",
            "block_id": "calendar_block",
            "text": {"type": "mrkdwn", "text": " "},
            "accessory": calendar_checkbox
        })

        # 모달 열기 - 회의 이름 입력
        client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "reservation_modal",
                "private_metadata": json.dumps({
                    "room_id": room_id,
                    "date": date,
                    "start_time": start_time,
                    "end_time": end_time,
                    "channel_id": channel_id,
                    "is_recurring": is_recurring,
                    "recurring_day": recurring_day,
                    "recurring_until": recurring_until,
                }),
                "title": {"type": "plain_text", "text": "회의실 예약"},
                "submit": {"type": "plain_text", "text": "예약"},
                "close": {"type": "plain_text", "text": "취소"},
                "blocks": blocks
            }
        )

    except Exception as e:
        logger.error(f"모달 열기 실패: {e}", exc_info=True)


def handle_calendar_checkbox(ack, body, logger):
    """캘린더 체크박스 클릭 - ack만 하면 됨 (값은 모달 제출 시 처리)"""
    ack()


def handle_reservation_modal(ack, body, client: WebClient, view):
    """모달 제출 처리 - 예약 실행"""
    ack()

    user_id = body["user"]["id"]

    try:
        config = get_config()

        # 모달에서 데이터 추출
        metadata = json.loads(view["private_metadata"])
        room_id = metadata["room_id"]
        date = metadata["date"]
        start_time = metadata["start_time"]
        end_time = metadata["end_time"]
        channel_id = metadata.get("channel_id") or user_id  # 원래 채널

        # 반복 예약 정보
        is_recurring = metadata.get("is_recurring", False)
        recurring_day = metadata.get("recurring_day")
        recurring_until = metadata.get("recurring_until")

        # 회의 이름 추출
        purpose = view["state"]["values"]["purpose_block"]["purpose_input"]["value"]

        # 반복 요일 추출 (드롭다운에서 - 사용자가 변경했을 수 있음)
        if is_recurring and "recurring_day_block" in view["state"]["values"]:
            recurring_day = view["state"]["values"]["recurring_day_block"]["recurring_day_input"]["selected_option"]["value"]

        # 반복 시작일 추출 (datepicker에서 - 사용자가 변경했을 수 있음)
        if is_recurring and "recurring_start_block" in view["state"]["values"]:
            date = view["state"]["values"]["recurring_start_block"]["recurring_start_input"]["selected_date"]

            # 시작일이 과거면 오류
            from datetime import datetime
            today_str = datetime.now().strftime("%Y-%m-%d")
            if date < today_str:
                client.chat_postMessage(
                    channel=user_id,
                    text=f"시작일은 오늘({today_str}) 이후여야 합니다."
                )
                return

        # 반복 종료일 추출 (datepicker에서 - 사용자가 변경했을 수 있음)
        if is_recurring and "recurring_until_block" in view["state"]["values"]:
            recurring_until = view["state"]["values"]["recurring_until_block"]["recurring_until_input"]["selected_date"]

        # Google Calendar 체크박스 상태 추출
        calendar_selected = view["state"]["values"]["calendar_block"]["calendar_checkbox"]["selected_options"]
        add_to_calendar = len(calendar_selected) > 0

        # 체크 상태가 config와 다르면 저장
        if add_to_calendar != config.google_calendar.enabled:
            update_config_value("google_calendar.enabled", add_to_calendar)
            logger.info(f"Google Calendar 설정 변경: {add_to_calendar}")

        # 로그
        if is_recurring:
            logger.info(f"[{user_id}] 반복 예약 요청: {room_id} - {purpose} (매주 {recurring_day}, ~{recurring_until})")
        else:
            logger.info(f"[{user_id}] 예약 요청: {room_id} - {purpose} (채널: {channel_id}, 캘린더: {add_to_calendar})")

        # 사용자에게 DM으로 진행 상황 알림
        from .llm_parser import DAY_LABELS
        try:
            if is_recurring:
                day_label = DAY_LABELS.get(recurring_day, recurring_day)
                client.chat_postMessage(
                    channel=user_id,
                    text=f"⏳ {room_id} 반복 예약 중... (매주 {day_label}요일, ~{recurring_until})"
                )
            else:
                client.chat_postMessage(
                    channel=user_id,
                    text=f"⏳ {room_id} 예약 중... ({purpose})"
                )
        except Exception as e:
            logger.warning(f"DM 전송 실패: {e}")

        # 비동기 예약 처리 (channel_id는 원래 채널, user_id는 DM용)
        _executor.submit(
            _process_reservation,
            client, user_id, channel_id,
            room_id, date, start_time, end_time, purpose, add_to_calendar,
            is_recurring, recurring_day, recurring_until
        )

    except Exception as e:
        logger.error(f"모달 처리 실패: {e}", exc_info=True)
        try:
            client.chat_postMessage(
                channel=user_id,
                text=f"❌ 오류가 발생했습니다: {e}"
            )
        except:
            pass


def _process_reservation(
    client: WebClient,
    user_id: str,
    channel_id: str,
    room_id: str,
    date: str,
    start_time: str,
    end_time: str,
    purpose: str,
    add_to_calendar: bool = True,
    is_recurring: bool = False,
    recurring_day: str = None,
    recurring_until: str = None
):
    """예약 처리 (백그라운드)

    Args:
        add_to_calendar: Google Calendar에 등록할지 여부
        is_recurring: 반복 예약 여부
        recurring_day: 반복 요일 (MON, TUE, ...)
        recurring_until: 반복 종료일 (YYYY-MM-DD)
    """
    config = get_config()
    from .llm_parser import DAY_LABELS

    try:
        # 요청 객체 생성
        request = ReservationRequest(
            action="reserve",
            date=date,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=0,
            preferred_room=room_id,
            purpose=purpose,
            is_recurring=is_recurring,
            recurring_day=recurring_day,
            recurring_until=recurring_until,
        )

        # 세션 만료 시 알림 콜백
        def notify_relogin():
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text="🔐 그룹웨어 세션이 만료되었습니다.\n브라우저 창이 열리면 로그인해주세요. (3분 내)"
                )
            except Exception as e:
                logger.warning(f"재로그인 알림 DM 실패: {e}")

        # 비동기 예약 수행
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            if is_recurring and recurring_day and recurring_until:
                # 반복 예약
                result = loop.run_until_complete(
                    make_recurring_reservation(
                        room_id, request, recurring_day, recurring_until,
                        on_relogin_needed=notify_relogin
                    )
                )
            else:
                # 단일 예약
                result = loop.run_until_complete(
                    make_reservation(room_id, request, on_relogin_needed=notify_relogin)
                )
        finally:
            loop.close()

        if result.success:
            # Google Calendar 등록 (체크박스 선택 + token.json이 있을 때만)
            calendar_added = False
            if add_to_calendar:
                token_file = get_base_path() / config.google_calendar.token_file
                if token_file.exists():
                    try:
                        # 반복 예약인 경우 recurrence 전달
                        recurrence = None
                        if is_recurring and recurring_day and recurring_until:
                            recurrence = {"day": recurring_day, "until": recurring_until}

                        event_id = add_calendar_event(
                            room=result.room,
                            date=result.date,
                            start_time=result.start_time,
                            end_time=result.end_time,
                            purpose=purpose,
                            recurrence=recurrence,
                        )

                        if event_id:
                            logger.info(f"캘린더 등록 완료: {event_id}")
                            calendar_added = True
                        else:
                            logger.warning("캘린더 등록 실패 (예약은 성공)")
                    except Exception as e:
                        logger.warning(f"캘린더 등록 중 오류 (예약은 성공): {e}")
                else:
                    logger.warning("Google Calendar 토큰 없음 - python google_auth.py 실행 필요")

            tier = config.get_tier_for_room(room_id)
            tier_label = tier.label if tier else ""

            # 채널에 완료 메시지 공유 (팀원들이 볼 수 있게)
            if is_recurring and recurring_day:
                day_label = DAY_LABELS.get(recurring_day, recurring_day)
                channel_message = (
                    f"✅ 반복 회의실 예약 완료\n"
                    f"시간: 매주 {day_label}요일 {result.start_time}-{result.end_time}\n"
                    f"기간: {result.date} ~ {recurring_until}\n"
                    f"장소: {tier_label} {result.room.name}\n"
                    f"목적: {purpose}"
                )
            else:
                channel_message = (
                    f"✅ 회의실 예약 완료\n"
                    f"시간: {result.date} {result.start_time}-{result.end_time}\n"
                    f"장소: {tier_label} {result.room.name}\n"
                    f"목적: {purpose}"
                )

            channel_post_success = False
            # 채널에 메시지 전송 시도
            if channel_id and channel_id != user_id:
                try:
                    logger.info(f"채널에 완료 메시지 전송: {channel_id}")
                    client.chat_postMessage(
                        channel=channel_id,
                        text=channel_message
                    )
                    channel_post_success = True
                except SlackApiError as e:
                    logger.warning(f"채널 메시지 전송 실패 ({channel_id}): {e.response['error']}")

            # 사용자에게 DM으로 상세 확인
            dm_message = f"✅ 예약 완료: {tier_label} {result.room.name}\n{result.date} {result.start_time}-{result.end_time}"
            if calendar_added:
                dm_message += "\n📅 Google Calendar에 등록됨"
            if not channel_post_success:
                # 채널 전송 실패 시 DM에 전체 내용 포함
                dm_message = channel_message
                if calendar_added:
                    dm_message += "\n📅 Google Calendar에 등록됨"

            try:
                client.chat_postMessage(
                    channel=user_id,
                    text=dm_message
                )
            except Exception as e:
                logger.warning(f"완료 DM 전송 실패: {e}")

        else:
            # 예약 실패 - DM으로만 알림 (채널에 에러 스팸 방지)
            error_msg = result.error_message or "알 수 없는 오류"
            _send_error_dm(client, user_id, f"예약 실패: {error_msg}")

        # 대기 중 요청 삭제
        _pending_requests.pop(user_id, None)

    except Exception as e:
        logger.error(f"예약 처리 실패: {e}", exc_info=True)
        _send_error_dm(client, user_id, str(e))


def _send_error_dm(client: WebClient, user_id: str, error_message: str):
    """사용자에게 에러 DM 전송"""
    try:
        client.chat_postMessage(
            channel=user_id,  # DM 전송
            text=f"⚠️ room-agent 오류\n```{error_message}```"
        )
    except SlackApiError as e:
        logger.error(f"DM 전송 실패: {e}")


def start_slack_app():
    """Slack 앱 시작 (Socket Mode)"""
    config = get_config()
    app = get_app()

    handler = SocketModeHandler(app, config.slack.app_token)

    logger.info("Slack 앱 시작 (Socket Mode)")
    handler.start()


def stop_slack_app():
    """Slack 앱 중지"""
    global _app
    _executor.shutdown(wait=False)
    logger.info("Slack 앱 중지")
