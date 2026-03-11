"""다우오피스 Playwright 자동화 모듈"""

import os
import json
import logging
import asyncio
import re
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeout

from .config import get_config, get_base_path, Room, RoomTier
from .llm_parser import ReservationRequest

logger = logging.getLogger(__name__)


@dataclass
class RoomAvailability:
    """회의실 가용성 정보"""
    room: Room
    available: bool
    reservations: list[dict] = None  # 기존 예약 목록
    free_slots: list[dict] = None  # 빈 시간대 목록

    def __post_init__(self):
        if self.reservations is None:
            self.reservations = []
        if self.free_slots is None:
            self.free_slots = []


@dataclass
class ReservationResult:
    """예약 결과"""
    success: bool
    room: Optional[Room] = None
    date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    error_message: Optional[str] = None


def calculate_free_slots(
    reservations: list[dict],
    day_start: str = "09:00",
    day_end: str = "18:00",
    min_duration: int = 30
) -> list[dict]:
    """예약 목록에서 빈 시간대 계산

    Args:
        reservations: 기존 예약 목록 [{"start": "HH:MM", "end": "HH:MM"}, ...]
        day_start: 업무 시작 시간
        day_end: 업무 종료 시간
        min_duration: 최소 표시할 빈 시간 (분)

    Returns:
        빈 시간대 목록 [{"start": "HH:MM", "end": "HH:MM"}, ...]
    """
    # 예약을 시작 시간 순으로 정렬
    sorted_reservations = sorted(reservations, key=lambda x: x["start"])

    free_slots = []
    current_time = day_start

    for res in sorted_reservations:
        res_start = res["start"]
        res_end = res["end"]

        # 현재 시간보다 예약 시작이 늦으면 빈 시간대
        if res_start > current_time:
            # 빈 시간대 길이 계산
            start_dt = datetime.strptime(current_time, "%H:%M")
            end_dt = datetime.strptime(res_start, "%H:%M")
            duration = (end_dt - start_dt).seconds // 60

            if duration >= min_duration:
                free_slots.append({
                    "start": current_time,
                    "end": res_start,
                    "duration": duration
                })

        # 현재 시간을 예약 종료 이후로 이동
        if res_end > current_time:
            current_time = res_end

    # 마지막 예약 이후 ~ 업무 종료까지 빈 시간
    if current_time < day_end:
        start_dt = datetime.strptime(current_time, "%H:%M")
        end_dt = datetime.strptime(day_end, "%H:%M")
        duration = (end_dt - start_dt).seconds // 60

        if duration >= min_duration:
            free_slots.append({
                "start": current_time,
                "end": day_end,
                "duration": duration
            })

    return free_slots


class DaouAutomation:
    """다우오피스 자동화 클래스"""

    def __init__(self, on_relogin_needed: callable = None):
        """
        Args:
            on_relogin_needed: 재로그인 필요 시 호출되는 콜백 (선택)
                               예: lambda: send_slack_dm("세션 만료, 브라우저에서 로그인하세요")
        """
        self.config = get_config()
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None
        self._on_relogin_needed = on_relogin_needed

    def _get_cookies_file(self) -> Path:
        """쿠키 파일 경로"""
        return get_base_path() / "cookies.json"

    async def _load_cookies(self, context: BrowserContext) -> bool:
        """저장된 쿠키 로드"""
        cookies_file = self._get_cookies_file()

        if not cookies_file.exists():
            logger.warning(f"쿠키 파일 없음: {cookies_file}")
            return False

        try:
            with open(cookies_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)

            await context.add_cookies(cookies)
            logger.info(f"쿠키 {len(cookies)}개 로드됨")
            return True

        except Exception as e:
            logger.error(f"쿠키 로드 실패: {e}")
            return False

    async def _handle_relogin(self) -> bool:
        """세션 만료 시 재로그인 처리 (브라우저 열어서 사용자 로그인 대기)"""
        logger.info("세션 만료 - 재로그인 필요")

        # 콜백이 있으면 호출 (Slack DM 알림 등)
        if self._on_relogin_needed:
            try:
                self._on_relogin_needed()
            except Exception as e:
                logger.warning(f"재로그인 콜백 실패: {e}")

        # 새 playwright 인스턴스로 로그인 브라우저 실행
        login_playwright = await async_playwright().start()

        try:
            # headless=False로 브라우저 열기 (사용자가 로그인할 수 있도록)
            browser = await login_playwright.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(120000)

            logger.info("로그인 브라우저 열림 - 사용자 로그인 대기 중...")

            await page.goto(
                "https://gw.suresofttech.com/app/asset/27/list/reservation",
                wait_until="domcontentloaded",
                timeout=60000,
            )

            # 로그인 완료 대기 (최대 3분)
            for i in range(180):
                await asyncio.sleep(1)

                try:
                    current_url = page.url
                except:
                    logger.warning("로그인 브라우저가 닫혔습니다")
                    return False

                # 로그인 완료 확인
                if "login" not in current_url.lower() and ("reservation" in current_url or "asset" in current_url):
                    logger.info("로그인 성공 감지!")

                    # 쿠키 저장
                    await asyncio.sleep(2)
                    cookies = await context.cookies()

                    cookies_file = self._get_cookies_file()
                    with open(cookies_file, "w", encoding="utf-8") as f:
                        json.dump(cookies, f, indent=2, ensure_ascii=False)

                    logger.info(f"새 쿠키 저장됨: {len(cookies)}개")

                    await browser.close()
                    return True

                if i % 30 == 0 and i > 0:
                    logger.info(f"로그인 대기 중... ({i}초)")

            logger.warning("로그인 시간 초과 (3분)")
            await browser.close()
            return False

        except Exception as e:
            logger.error(f"재로그인 처리 실패: {e}")
            return False
        finally:
            await login_playwright.stop()

    @asynccontextmanager
    async def get_browser_context(self):
        """브라우저 컨텍스트 생성"""
        playwright = await async_playwright().start()
        self._playwright = playwright

        timeout = self.config.defaults.timeout * 1000
        max_retries = self.config.defaults.max_retries

        browser = None
        for attempt in range(max_retries):
            try:
                logger.info(f"브라우저 시작 시도 ({attempt + 1}/{max_retries})")

                browser = await playwright.chromium.launch(
                    headless=self.config.defaults.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )

                self.context = await browser.new_context()

                if not await self._load_cookies(self.context):
                    raise RuntimeError(
                        "쿠키 파일이 없습니다. 먼저 login.py를 실행하세요: python login.py"
                    )

                self.page = await self.context.new_page()
                self.page.set_default_timeout(timeout)

                logger.info("브라우저 시작 성공")
                break

            except Exception as e:
                logger.warning(f"브라우저 시작 실패 (시도 {attempt + 1}): {e}")
                if browser:
                    await browser.close()
                if attempt == max_retries - 1:
                    raise RuntimeError(f"브라우저 시작 실패: {e}")
                await asyncio.sleep(2)

        try:
            yield self.page
        finally:
            if browser:
                await browser.close()
            await playwright.stop()

    async def _reload_browser_with_new_cookies(self) -> bool:
        """새 쿠키로 브라우저 컨텍스트 재시작"""
        try:
            # 기존 브라우저 닫기
            if self.context:
                browser = self.context.browser
                await self.context.close()
                if browser:
                    await browser.close()

            # 새 브라우저 시작
            timeout = self.config.defaults.timeout * 1000
            browser = await self._playwright.chromium.launch(
                headless=self.config.defaults.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )

            self.context = await browser.new_context()

            if not await self._load_cookies(self.context):
                return False

            self.page = await self.context.new_page()
            self.page.set_default_timeout(timeout)

            logger.info("새 쿠키로 브라우저 재시작 완료")
            return True

        except Exception as e:
            logger.error(f"브라우저 재시작 실패: {e}")
            return False

    async def navigate_to_floor(self, floor_id: int, date: str, _retry: bool = False) -> bool:
        """특정 층 예약 페이지로 이동"""
        url = f"https://gw.suresofttech.com/app/asset/{floor_id}/list/reservation"
        logger.info(f"페이지 이동: {url}")

        await self.page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)  # 페이지 로드 대기

        # 로그인 확인
        current_url = self.page.url
        if "login" in current_url.lower():
            if _retry:
                # 이미 재시도했는데도 로그인 필요하면 에러
                raise RuntimeError("재로그인 후에도 세션이 유효하지 않습니다.")

            logger.warning("세션 만료 감지 - 자동 재로그인 시도")

            # 재로그인 처리
            if await self._handle_relogin():
                # 새 쿠키로 브라우저 재시작
                if await self._reload_browser_with_new_cookies():
                    # 재시도
                    return await self.navigate_to_floor(floor_id, date, _retry=True)

            raise RuntimeError("자동 재로그인 실패. 수동으로 python login.py를 실행하세요.")

        # 날짜 이동 (버튼 클릭 방식)
        await self._navigate_to_date(date)

        # 디버그: 스크린샷 저장
        debug_dir = get_base_path() / "debug"
        debug_dir.mkdir(exist_ok=True)
        await self.page.screenshot(path=str(debug_dir / f"floor_{floor_id}_date_{date}.png"))
        logger.info(f"스크린샷 저장: floor_{floor_id}_date_{date}.png")

        return True

    async def _navigate_to_date(self, date: str) -> None:
        """특정 날짜로 이동 - 다우오피스 캘린더 네비게이션"""
        target = datetime.strptime(date, "%Y-%m-%d")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        days_diff = (target - today).days
        logger.info(f"날짜 이동: 오늘={today.strftime('%Y-%m-%d')}, 목표={date}, 차이={days_diff}일")

        if days_diff == 0:
            logger.info("목표 날짜가 오늘이므로 이동 불필요")
            return

        target_year = target.year
        target_month = target.month
        target_day = target.day

        # 방법 1: datepicker 사용 시도 (날짜 표시 영역 클릭)
        date_display = self.page.locator(".date_select, .current_date, .date_area, [data-date]").first
        try:
            if await date_display.is_visible(timeout=2000):
                await date_display.click()
                await asyncio.sleep(0.5)

                # datepicker 열렸는지 확인
                datepicker = self.page.locator("#ui-datepicker-div")
                if await datepicker.is_visible(timeout=1000):
                    logger.info("datepicker 열림 - 콤보박스로 날짜 선택")

                    # 년도 선택
                    year_select = self.page.locator("#ui-datepicker-div .ui-datepicker-year")
                    if await year_select.is_visible(timeout=500):
                        await year_select.select_option(value=str(target_year))
                        await asyncio.sleep(0.3)

                    # 월 선택 (0-based)
                    month_select = self.page.locator("#ui-datepicker-div .ui-datepicker-month")
                    if await month_select.is_visible(timeout=500):
                        await month_select.select_option(value=str(target_month - 1))
                        await asyncio.sleep(0.3)

                    # 날짜 클릭
                    date_cell = self.page.locator(f"#ui-datepicker-div td:not(.ui-datepicker-unselectable) a:text-is('{target_day}')").first
                    if await date_cell.is_visible(timeout=500):
                        await date_cell.click()
                        logger.info(f"datepicker로 날짜 선택 완료: {date}")
                        await asyncio.sleep(1)
                        return
        except Exception as e:
            logger.debug(f"datepicker 방식 실패: {e}")

        # 방법 2: 버튼 클릭 (fallback, 7일 이내만)
        if abs(days_diff) <= 7:
            logger.info(f"버튼 클릭 방식으로 {abs(days_diff)}일 이동")
            for i in range(abs(days_diff)):
                if days_diff > 0:
                    btn = self.page.locator(".btn_ic_next2").first
                    direction = "다음"
                else:
                    btn = self.page.locator(".btn_ic_prev2").first
                    direction = "이전"

                try:
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await asyncio.sleep(0.5)
                    else:
                        break
                except Exception as e:
                    logger.warning(f"버튼 클릭 실패: {e}")
                    break
        else:
            logger.warning(f"날짜 차이가 {abs(days_diff)}일로 너무 큼 - datepicker 필요")

        await asyncio.sleep(1)
        logger.info(f"날짜 이동 완료: {date}")

    async def get_room_reservations(self, row_key: int) -> list[dict]:
        """특정 회의실의 예약 목록 조회 (현재 날짜만)"""
        reservations = []
        seen_times = set()  # 중복 제거용

        try:
            # 해당 회의실의 예약 항목들 찾기
            row_selector = f"[data-matrix-row][data-row-key='{row_key}']"
            items = self.page.locator(f"{row_selector} [data-matrix-item]")

            count = await items.count()
            logger.debug(f"row_key={row_key}: {count}개 예약 항목 발견")

            for i in range(count):
                item = items.nth(i)

                # CSS left 값 확인 - 100% 이상은 다음 날 데이터이므로 제외
                style = await item.get_attribute("style") or ""
                left_match = re.search(r"left:\s*([\d.]+)%", style)
                if left_match:
                    left_pct = float(left_match.group(1))
                    if left_pct >= 100:
                        # 다음 날 데이터 - 건너뜀
                        continue

                title = await item.get_attribute("title") or ""
                content_el = item.locator("[data-matrix-item-content]")
                content = await content_el.get_attribute("title") if await content_el.count() > 0 else title

                # title에서 시간 추출: "이름 09:00 ~ 10:00 제목, 참석자"
                time_match = re.search(r"(\d{2}:\d{2})\s*[~-]\s*(\d{2}:\d{2})", content)
                if time_match:
                    start_t = time_match.group(1)
                    end_t = time_match.group(2)

                    # 중복 제거
                    time_key = f"{start_t}-{end_t}"
                    if time_key in seen_times:
                        logger.debug(f"  중복 건너뜀: {start_t}-{end_t}")
                        continue
                    seen_times.add(time_key)

                    reservations.append({
                        "start": start_t,
                        "end": end_t,
                        "title": content,
                    })
                    logger.info(f"  예약: {start_t}-{end_t}")
                else:
                    logger.debug(f"  시간 파싱 실패: {content[:50]}")

        except Exception as e:
            logger.error(f"예약 조회 실패 (row_key={row_key}): {e}")

        return reservations

    def _is_time_slot_available(
        self,
        reservations: list[dict],
        start_time: str,
        end_time: str
    ) -> bool:
        """시간대 충돌 확인"""
        start = datetime.strptime(start_time, "%H:%M")
        end = datetime.strptime(end_time, "%H:%M")

        logger.debug(f"충돌 확인: 요청 {start_time}-{end_time}")

        for res in reservations:
            res_start = datetime.strptime(res["start"], "%H:%M")
            res_end = datetime.strptime(res["end"], "%H:%M")

            # 시간대 겹침 확인
            if not (end <= res_start or start >= res_end):
                logger.debug(f"  충돌: {res['start']}-{res['end']} vs 요청 {start_time}-{end_time}")
                return False
            else:
                logger.debug(f"  OK: {res['start']}-{res['end']}")

        return True

    async def check_floor_availability(
        self,
        tier: RoomTier,
        date: str,
        start_time: str,
        end_time: str
    ) -> list[RoomAvailability]:
        """특정 층의 모든 회의실 가용성 확인"""
        results = []

        # 해당 층 페이지로 이동
        await self.navigate_to_floor(tier.floor_id, date)

        # 각 회의실 확인
        for room in tier.rooms:
            if room.row_key == 0:
                continue

            reservations = await self.get_room_reservations(room.row_key)

            # 상세 로깅: 각 회의실의 예약 시간대
            logger.info(f"=== {room.id} (row_key={room.row_key}) 예약 현황 ===")
            for r in reservations:
                logger.info(f"  - {r['start']}-{r['end']}")

            available = self._is_time_slot_available(reservations, start_time, end_time)

            # 빈 시간대 계산
            free_slots = calculate_free_slots(reservations)

            results.append(RoomAvailability(
                room=room,
                available=available,
                reservations=reservations,
                free_slots=free_slots,
            ))

            logger.info(f"{room.id}: {'가용' if available else '불가'} (요청: {start_time}-{end_time})")
            logger.info(f"  빈 시간대: {[f'{s['start']}-{s['end']}' for s in free_slots]}")

        return results

    async def reserve_room(
        self,
        room: Room,
        tier: RoomTier,
        date: str,
        start_time: str,
        end_time: str,
        purpose: str
    ) -> ReservationResult:
        """회의실 예약 수행"""
        debug_dir = get_base_path() / "debug"
        debug_dir.mkdir(exist_ok=True)

        try:
            # 1. 해당 층/날짜 페이지로 이동
            await self.navigate_to_floor(tier.floor_id, date)

            # 2. 기존 예약 확인하여 빈 시간대인지 검증
            reservations = await self.get_room_reservations(room.row_key)
            if not self._is_time_slot_available(reservations, start_time, end_time):
                return ReservationResult(
                    success=False,
                    error_message=f"해당 시간에 이미 예약이 있습니다: {start_time}-{end_time}"
                )

            # 3. 해당 회의실 행 찾기
            row_selector = f"[data-matrix-row][data-row-key='{room.row_key}']"
            row = self.page.locator(row_selector)

            if not await row.is_visible():
                return ReservationResult(
                    success=False,
                    error_message=f"회의실 행을 찾을 수 없습니다: {room.id}"
                )

            # 4. 시간 기준 픽셀 위치 계산
            start_hour, start_min = map(int, start_time.split(":"))

            # 캘린더는 00:00부터 시작, 1시간당 약 30px
            minutes_from_0 = start_hour * 60 + start_min
            click_x = int(minutes_from_0 * (30 / 60)) + 10  # 30px per hour

            logger.info(f"행 클릭 시도: room={room.id}, time={start_time}, x={click_x}")

            # 스크린샷 저장 (클릭 전)
            await self.page.screenshot(path=str(debug_dir / "reserve_before_click.png"))

            # 더블클릭으로 예약 폼 열기
            await row.dblclick(position={"x": click_x, "y": 20})
            await asyncio.sleep(2)

            # 5. 페이지 변경 확인
            current_url = self.page.url
            logger.info(f"클릭 후 URL: {current_url}")

            # 스크린샷 저장 (클릭 후)
            await self.page.screenshot(path=str(debug_dir / "reserve_after_click.png"))

            # 읽기 전용 페이지인지 확인 (기존 예약 조회)
            return_btn = self.page.locator("[data-btntype='returnList']")
            readonly_inputs = await self.page.locator("input[readonly='readonly']").count()

            if await return_btn.is_visible() and readonly_inputs > 3:
                logger.warning("기존 예약 조회 페이지가 열림")
                await return_btn.click()
                await asyncio.sleep(1)
                return ReservationResult(
                    success=False,
                    error_message="해당 시간에 이미 예약이 있습니다."
                )

            # 6. 모달 팝업 확인 및 종료 시간 수정
            # 종료 시간 필드 수정 (id="endTime") - 드롭다운 형식
            end_time_input = self.page.locator("#endTime")
            if await end_time_input.is_visible():
                current_end = await end_time_input.input_value()
                logger.info(f"현재 종료 시간: {current_end}, 변경할 종료 시간: {end_time}")

                if current_end != end_time:
                    # 입력 필드 클릭하여 드롭다운 열기
                    await end_time_input.click()
                    await asyncio.sleep(0.3)

                    # 시간 입력
                    await end_time_input.fill(end_time)
                    await asyncio.sleep(0.5)

                    # 드롭다운에서 해당 시간 옵션 클릭
                    # 옵션 형식: "16:00(1시간)" 등
                    dropdown_option = self.page.locator(f"li:has-text('{end_time}')").first
                    if await dropdown_option.is_visible():
                        await dropdown_option.click()
                        logger.info(f"드롭다운에서 {end_time} 선택")
                    else:
                        # 드롭다운이 안 보이면 Enter로 확정
                        await end_time_input.press("Enter")
                        logger.info(f"Enter로 {end_time} 확정")

                    await asyncio.sleep(0.3)

                    # 변경 확인
                    new_end = await end_time_input.input_value()
                    logger.info(f"종료 시간 변경 완료: {current_end} -> {new_end}")
            else:
                logger.warning("종료 시간 필드(#endTime)를 찾지 못함")

            # 모달 내의 입력 필드들 찾기
            purpose_inputs = self.page.locator("input.txt:not([readonly]):not([disabled])")
            purpose_input = None

            # 여러 입력 필드 중 빈 것 찾기
            count = await purpose_inputs.count()
            logger.info(f"입력 필드 {count}개 발견")

            for i in range(count):
                inp = purpose_inputs.nth(i)
                if await inp.is_visible():
                    val = await inp.input_value()
                    # 빈 필드이면서 시간 형식이 아닌 것 (이용목적)
                    if not val:
                        purpose_input = inp
                        break

            if purpose_input:
                logger.info("이용목적 입력 필드 발견")
                await purpose_input.fill(purpose)
                logger.info(f"이용목적 입력: {purpose}")
            else:
                html_content = await self.page.content()
                (debug_dir / "reserve_no_form.html").write_text(html_content, encoding="utf-8")
                return ReservationResult(
                    success=False,
                    error_message="예약 폼이 열리지 않았습니다."
                )

            # 7. 참석인원 입력 (다음 빈 필드)
            for i in range(count):
                inp = purpose_inputs.nth(i)
                if await inp.is_visible():
                    val = await inp.input_value()
                    if not val:  # 아직 빈 필드 (참석인원)
                        await inp.fill(self.config.defaults.attendees)
                        logger.info(f"참석인원 입력: {self.config.defaults.attendees}")
                        break

            await asyncio.sleep(0.5)

            # 8. 확인 버튼 클릭
            save_btn = self.page.locator(
                "button:has-text('확인'), "
                "a:has-text('확인'), "
                ".btn_major:has-text('확인'), "
                "button.btn_major"
            ).first

            # 스크린샷 저장 (저장 전)
            await self.page.screenshot(path=str(debug_dir / "reserve_before_save.png"))

            if await save_btn.is_visible():
                await save_btn.click()
                logger.info("저장 버튼 클릭")
            else:
                html_content = await self.page.content()
                (debug_dir / "reserve_no_save_btn.html").write_text(html_content, encoding="utf-8")
                return ReservationResult(
                    success=False,
                    error_message="저장 버튼을 찾을 수 없습니다."
                )

            await asyncio.sleep(2)

            # 9. 성공 확인
            new_url = self.page.url
            await self.page.screenshot(path=str(debug_dir / "reserve_after_save.png"))

            # 에러 메시지 확인
            error_msg = self.page.locator(".error, .alert-danger, .notice_error")
            if await error_msg.is_visible():
                error_text = await error_msg.text_content()
                return ReservationResult(
                    success=False,
                    error_message=f"예약 실패: {error_text}"
                )

            # 페이지가 변경되었거나 목록으로 돌아갔으면 성공
            if "list" in new_url or new_url != current_url:
                logger.info(f"예약 성공: {room.id} {date} {start_time}-{end_time}")
                return ReservationResult(
                    success=True,
                    room=room,
                    date=date,
                    start_time=start_time,
                    end_time=end_time,
                )

            return ReservationResult(
                success=False,
                error_message="예약 결과를 확인할 수 없습니다."
            )

        except Exception as e:
            logger.error(f"예약 실패: {e}", exc_info=True)
            await self.page.screenshot(path=str(debug_dir / "reserve_error.png"))
            return ReservationResult(
                success=False,
                error_message=str(e)
            )

    async def reserve_room_recurring(
        self,
        room: Room,
        tier: RoomTier,
        date: str,
        start_time: str,
        end_time: str,
        purpose: str,
        recurring_day: str,
        recurring_until: str,
    ) -> ReservationResult:
        """반복 예약 수행

        Args:
            room: 회의실 정보
            tier: 층 정보
            date: 시작 날짜 (YYYY-MM-DD)
            start_time: 시작 시간 (HH:MM)
            end_time: 종료 시간 (HH:MM)
            purpose: 이용목적
            recurring_day: 반복 요일 (MON, TUE, WED, THU, FRI)
            recurring_until: 반복 종료일 (YYYY-MM-DD)
        """
        debug_dir = get_base_path() / "debug"
        debug_dir.mkdir(exist_ok=True)

        # 요일 라벨 매핑
        day_labels = {"MON": "월", "TUE": "화", "WED": "수", "THU": "목", "FRI": "금"}
        day_label = day_labels.get(recurring_day, recurring_day)

        try:
            # 1. 해당 층/날짜로 이동
            await self.navigate_to_floor(tier.floor_id, date)

            # 2. 회의실 행 찾기 (기존 reserve_room과 동일한 selector 사용)
            row_selector = f"[data-matrix-row][data-row-key='{room.row_key}']"
            row = self.page.locator(row_selector)

            if not await row.is_visible():
                return ReservationResult(
                    success=False,
                    error_message=f"회의실 행을 찾을 수 없습니다: {room.id}"
                )

            # 3. 시간 기준 클릭 위치 계산 (기존 reserve_room과 동일)
            start_hour, start_min = map(int, start_time.split(":"))
            minutes_from_0 = start_hour * 60 + start_min
            click_x = int(minutes_from_0 * (30 / 60)) + 10  # 30px per hour

            logger.info(f"반복 예약 - 행 클릭: room={room.id}, time={start_time}, x={click_x}")

            # 4. 더블클릭으로 예약 폼 열기
            await row.dblclick(position={"x": click_x, "y": 20})
            await asyncio.sleep(2)

            await self.page.screenshot(path=str(debug_dir / "recurring_after_dblclick.png"))

            # 4-1. 종료 시간 설정 (#endTime)
            end_time_input = self.page.locator("#endTime")
            if await end_time_input.is_visible(timeout=2000):
                current_end = await end_time_input.input_value()
                logger.info(f"현재 종료 시간: {current_end}, 변경할 종료 시간: {end_time}")

                if current_end != end_time:
                    # 입력 필드 클릭하여 드롭다운 열기
                    await end_time_input.click()
                    await asyncio.sleep(0.3)

                    # 시간 입력
                    await end_time_input.fill(end_time)
                    await asyncio.sleep(0.5)

                    # 드롭다운에서 해당 시간 옵션 클릭
                    dropdown_option = self.page.locator(f"#endTimeList li[data-value='{end_time}']")
                    if await dropdown_option.is_visible(timeout=1000):
                        await dropdown_option.click()
                        logger.info(f"드롭다운에서 {end_time} 선택")
                    else:
                        # Enter로 확정
                        await end_time_input.press("Enter")
                        logger.info(f"Enter로 {end_time} 확정")

                    await asyncio.sleep(0.3)

                    # 변경 확인
                    new_end = await end_time_input.input_value()
                    logger.info(f"종료 시간 변경 완료: {current_end} -> {new_end}")
            else:
                logger.warning("종료 시간 필드(#endTime)를 찾지 못함")

            # 5. "예약상세 등록" 버튼 찾기 (여러 selector 시도)
            detail_btn_found = False
            detail_selectors = [
                "button:has-text('예약상세 등록')",
                "a:has-text('예약상세 등록')",
                "button:has-text('예약상세')",
                "a:has-text('예약상세')",
                "button:has-text('예약 상세 등록')",
                "a:has-text('예약 상세 등록')",
                ".btn:has-text('상세')",
                "[data-btntype='detail']",
                ".btn_detail",
            ]

            for selector in detail_selectors:
                detail_btn = self.page.locator(selector).first
                if await detail_btn.is_visible(timeout=1000):
                    logger.info(f"예약 상세 등록 버튼 발견: {selector}")
                    await detail_btn.click()
                    await asyncio.sleep(1)
                    detail_btn_found = True
                    break

            if not detail_btn_found:
                logger.warning("예약상세 등록 버튼을 찾을 수 없음")
                html_content = await self.page.content()
                (debug_dir / "recurring_no_detail_btn.html").write_text(html_content, encoding="utf-8")
                return ReservationResult(
                    success=False,
                    error_message="예약상세 등록 버튼을 찾을 수 없습니다. debug/recurring_no_detail_btn.html 확인"
                )

            await self.page.screenshot(path=str(debug_dir / "recurring_after_detail.png"))

            # 6. "반복" 체크박스 선택 (라벨 클릭)
            repeat_label = self.page.locator("label[for='repeat']")
            repeat_checkbox = self.page.locator("#repeat")

            if await repeat_label.is_visible(timeout=3000):
                # 이미 체크되어 있는지 확인
                is_checked = await repeat_checkbox.is_checked()
                if not is_checked:
                    await repeat_label.click()
                    logger.info("반복 라벨 클릭: label[for='repeat']")
                    await asyncio.sleep(1)  # JavaScript 실행 대기
                else:
                    logger.info("반복 체크박스 이미 선택됨")
            else:
                return ReservationResult(
                    success=False,
                    error_message="반복 라벨을 찾을 수 없습니다."
                )

            await self.page.screenshot(path=str(debug_dir / "recurring_after_repeat_check.png"))

            # 7. 반복 옵션 탭 영역 대기
            await asyncio.sleep(1)

            # HTML 저장 (체크박스 선택 후 상태)
            html_content = await self.page.content()
            (debug_dir / "recurring_after_checkbox.html").write_text(html_content, encoding="utf-8")
            logger.info("HTML 저장: recurring_after_checkbox.html")

            # 8. "매주" 탭 선택 (#recurrence-tab-weekly)
            weekly_tab = self.page.locator("#recurrence-tab-weekly")
            if await weekly_tab.is_visible(timeout=3000):
                await weekly_tab.click()
                logger.info("매주 탭 클릭: #recurrence-tab-weekly")
                await asyncio.sleep(0.5)
            else:
                logger.error("매주 탭을 찾을 수 없음")
                html_content = await self.page.content()
                (debug_dir / "recurring_no_weekly.html").write_text(html_content, encoding="utf-8")
                return ReservationResult(
                    success=False,
                    error_message="매주 탭(#recurrence-tab-weekly)을 찾을 수 없습니다."
                )

            await self.page.screenshot(path=str(debug_dir / "recurring_after_weekly.png"))
            # 요일은 시작 날짜 기준으로 다우에서 자동 선택됨 - 별도 선택 불필요

            # 10. 반복 종료일 설정 (input[name='repeat_date'] 클릭 → 날짜 선택)
            # 반복종료 input 필드 클릭 (캘린더 열기)
            repeat_date_input = self.page.locator("input[name='repeat_date']")
            if await repeat_date_input.is_visible(timeout=2000):
                await repeat_date_input.click()
                logger.info("반복종료 날짜 입력 필드 클릭")
                await asyncio.sleep(0.5)
            else:
                # 캘린더 아이콘 클릭 시도
                calendar_icon = self.page.locator("label[for='repeat_end'] ~ .wrap_date .ic_calendar")
                if await calendar_icon.is_visible(timeout=1000):
                    await calendar_icon.click()
                    logger.info("반복종료 캘린더 아이콘 클릭")
                    await asyncio.sleep(0.5)

            await self.page.screenshot(path=str(debug_dir / "recurring_calendar_open.png"))

            # 날짜 선택 (datepicker 콤보박스로 년/월 선택)
            try:
                target_year = int(recurring_until.split("-")[0])
                target_month = int(recurring_until.split("-")[1])
                target_day = int(recurring_until.split("-")[2])
                logger.info(f"목표 날짜: {target_year}-{target_month:02d}-{target_day:02d}")

                datepicker = self.page.locator("#ui-datepicker-div")
                if await datepicker.is_visible(timeout=2000):
                    logger.info("datepicker 열림 확인")

                    # 년도 콤보박스 선택
                    year_select = self.page.locator("#ui-datepicker-div .ui-datepicker-year")
                    if await year_select.is_visible(timeout=1000):
                        await year_select.select_option(value=str(target_year))
                        logger.info(f"년도 선택: {target_year}")
                        await asyncio.sleep(0.3)
                    else:
                        logger.warning("년도 콤보박스를 찾을 수 없음")

                    # 월 콤보박스 선택 (0-based: 1월=0, 2월=1, ...)
                    month_select = self.page.locator("#ui-datepicker-div .ui-datepicker-month")
                    if await month_select.is_visible(timeout=1000):
                        await month_select.select_option(value=str(target_month - 1))
                        logger.info(f"월 선택: {target_month}월 (value={target_month - 1})")
                        await asyncio.sleep(0.3)
                    else:
                        logger.warning("월 콤보박스를 찾을 수 없음")

                    await self.page.screenshot(path=str(debug_dir / "recurring_month_selected.png"))

                    # 해당 날짜 클릭
                    date_cell = self.page.locator(f"#ui-datepicker-div td:not(.ui-datepicker-unselectable) a:text-is('{target_day}')").first
                    if await date_cell.is_visible(timeout=1000):
                        await date_cell.click()
                        logger.info(f"반복종료 날짜 선택: {target_day}일")
                        await asyncio.sleep(0.5)
                    else:
                        logger.warning(f"datepicker에서 {target_day}일을 찾을 수 없음 - 마지막 날 선택 시도")
                        # 마지막 활성 날짜 선택
                        last_date = self.page.locator("#ui-datepicker-div td:not(.ui-datepicker-unselectable) a").last
                        if await last_date.is_visible(timeout=500):
                            await last_date.click()
                            logger.info("마지막 활성 날짜 선택")
                        else:
                            await self.page.keyboard.press("Escape")
                else:
                    logger.warning("datepicker(#ui-datepicker-div)가 표시되지 않음")
                    html = await self.page.content()
                    (debug_dir / "recurring_no_datepicker.html").write_text(html, encoding="utf-8")

            except Exception as e:
                logger.warning(f"날짜 선택 실패: {e}")
                import traceback
                logger.warning(traceback.format_exc())
                await self.page.keyboard.press("Escape")

            await self.page.screenshot(path=str(debug_dir / "recurring_after_until.png"))

            # 11. 반복 설정 모달 확인 버튼 클릭 (footer .btn_major_s)
            modal_confirm = self.page.locator("#gpopupLayer footer .btn_major_s")
            if await modal_confirm.is_visible(timeout=2000):
                await modal_confirm.click()
                logger.info("반복 설정 모달 확인 버튼 클릭")
                await asyncio.sleep(1)
            else:
                # 다른 selector 시도
                alt_confirm = self.page.locator("footer a.btn_major_s:has-text('확인')")
                if await alt_confirm.is_visible(timeout=1000):
                    await alt_confirm.click()
                    logger.info("반복 설정 확인 버튼 클릭 (alt)")
                    await asyncio.sleep(1)
                else:
                    logger.warning("반복 설정 확인 버튼을 찾을 수 없음")

            await self.page.screenshot(path=str(debug_dir / "recurring_after_modal_confirm.png"))

            # 12. 예약 불가 경고 다이얼로그 확인 (있는 경우)
            await asyncio.sleep(0.5)
            warning_confirm_selectors = [
                ".go_popup button:has-text('확인')",
                ".go_popup a:has-text('확인')",
                ".layer_alert button:has-text('확인')",
                ".layer_alert a:has-text('확인')",
                "#alertLayer button:has-text('확인')",
                "#alertLayer a:has-text('확인')",
                ".btn_major:has-text('확인')",
            ]

            for selector in warning_confirm_selectors:
                try:
                    warning_btn = self.page.locator(selector).first
                    if await warning_btn.is_visible(timeout=500):
                        await warning_btn.click()
                        logger.info(f"예약 불가 경고 확인 버튼 클릭: {selector}")
                        await asyncio.sleep(0.5)
                        break
                except:
                    continue

            await self.page.screenshot(path=str(debug_dir / "recurring_after_warning.png"))

            # 13. 이용목적 입력 (기존 로직과 동일)
            purpose_inputs = self.page.locator("input.txt:not([readonly]):not([disabled])")
            count = await purpose_inputs.count()
            logger.info(f"입력 필드 개수: {count}")

            purpose_input = None
            for i in range(count):
                inp = purpose_inputs.nth(i)
                if await inp.is_visible():
                    val = await inp.input_value()
                    if not val:
                        purpose_input = inp
                        break

            if purpose_input:
                await purpose_input.fill(purpose)
                logger.info(f"이용목적 입력: {purpose}")

            # 12. 참석인원 입력
            for i in range(count):
                inp = purpose_inputs.nth(i)
                if await inp.is_visible():
                    val = await inp.input_value()
                    if not val:
                        await inp.fill(self.config.defaults.attendees)
                        logger.info(f"참석인원 입력: {self.config.defaults.attendees}")
                        break

            await self.page.screenshot(path=str(debug_dir / "recurring_before_save.png"))

            # 13. 최종 확인 버튼 클릭
            save_btn = self.page.locator(
                "button:has-text('확인'), "
                "a:has-text('확인'), "
                ".btn_major:has-text('확인'), "
                "button.btn_major"
            ).first

            if await save_btn.is_visible(timeout=5000):
                await save_btn.click()
                logger.info("최종 확인 버튼 클릭")
            else:
                return ReservationResult(
                    success=False,
                    error_message="확인 버튼을 찾을 수 없습니다"
                )

            await asyncio.sleep(2)
            await self.page.screenshot(path=str(debug_dir / "recurring_after_save.png"))

            # 성공 여부 확인 (에러 메시지 체크)
            error_msg = self.page.locator(".error, .alert-error, [class*='error']")
            if await error_msg.is_visible(timeout=1000):
                error_text = await error_msg.text_content()
                return ReservationResult(
                    success=False,
                    error_message=f"반복 예약 실패: {error_text}"
                )

            logger.info(f"반복 예약 성공: {room.id} 매주 {day_label}요일 {start_time}-{end_time} (~{recurring_until})")

            return ReservationResult(
                success=True,
                room=room,
                date=date,
                start_time=start_time,
                end_time=end_time,
            )

        except Exception as e:
            logger.error(f"반복 예약 실패: {e}", exc_info=True)
            await self.page.screenshot(path=str(debug_dir / "recurring_error.png"))
            return ReservationResult(
                success=False,
                error_message=str(e)
            )


async def check_rooms_availability(
    request: ReservationRequest,
    on_relogin_needed: callable = None
) -> list[RoomAvailability]:
    """회의실 가용성 조회

    Args:
        request: 예약 요청 정보
        on_relogin_needed: 세션 만료 시 호출되는 콜백 (선택)
    """
    automation = DaouAutomation(on_relogin_needed=on_relogin_needed)
    results = []

    async with automation.get_browser_context():
        config = automation.config

        for tier in config.room_priority:
            if tier.floor_id == 0:
                continue

            availabilities = await automation.check_floor_availability(
                tier=tier,
                date=request.date,
                start_time=request.start_time,
                end_time=request.end_time,
            )
            results.extend(availabilities)

            # 순수 조회 모드 (시간 미지정): 11층만 표시
            if request.is_query and not request.time_specified:
                break

            # 시간 지정된 경우: 가용한 회의실 있으면 중단, 없으면 다음 층 탐색
            if any(a.available for a in availabilities):
                break

    return results


async def make_reservation(
    room_id: str,
    request: ReservationRequest,
    on_relogin_needed: callable = None
) -> ReservationResult:
    """예약 수행

    Args:
        room_id: 회의실 ID
        request: 예약 요청 정보
        on_relogin_needed: 세션 만료 시 호출되는 콜백 (선택)
    """
    automation = DaouAutomation(on_relogin_needed=on_relogin_needed)
    config = automation.config

    # 회의실 및 Tier 찾기
    room = config.get_room_by_id(room_id)
    if not room:
        return ReservationResult(
            success=False,
            error_message=f"회의실을 찾을 수 없습니다: {room_id}"
        )

    tier = config.get_tier_for_room(room_id)
    if not tier:
        return ReservationResult(
            success=False,
            error_message=f"층 정보를 찾을 수 없습니다: {room_id}"
        )

    async with automation.get_browser_context():
        result = await automation.reserve_room(
            room=room,
            tier=tier,
            date=request.date,
            start_time=request.start_time,
            end_time=request.end_time,
            purpose=request.purpose or "회의",
        )

    return result


async def make_recurring_reservation(
    room_id: str,
    request: ReservationRequest,
    recurring_day: str,
    recurring_until: str,
    on_relogin_needed: callable = None
) -> ReservationResult:
    """반복 예약 수행

    Args:
        room_id: 회의실 ID
        request: 예약 요청 정보
        recurring_day: 반복 요일 (MON, TUE, WED, THU, FRI)
        recurring_until: 반복 종료일 (YYYY-MM-DD)
        on_relogin_needed: 세션 만료 시 호출되는 콜백 (선택)
    """
    automation = DaouAutomation(on_relogin_needed=on_relogin_needed)
    config = automation.config

    # 회의실 및 Tier 찾기
    room = config.get_room_by_id(room_id)
    if not room:
        return ReservationResult(
            success=False,
            error_message=f"회의실을 찾을 수 없습니다: {room_id}"
        )

    tier = config.get_tier_for_room(room_id)
    if not tier:
        return ReservationResult(
            success=False,
            error_message=f"층 정보를 찾을 수 없습니다: {room_id}"
        )

    async with automation.get_browser_context():
        result = await automation.reserve_room_recurring(
            room=room,
            tier=tier,
            date=request.date,
            start_time=request.start_time,
            end_time=request.end_time,
            purpose=request.purpose or "회의",
            recurring_day=recurring_day,
            recurring_until=recurring_until,
        )

    return result
