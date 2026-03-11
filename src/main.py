"""room-agent 메인 진입점"""

import os
import sys
import socket
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime

import pystray
from PIL import Image, ImageDraw

from .config import get_config, get_base_path
from .slack_handler import start_slack_app, stop_slack_app

# 로깅 설정
logger = logging.getLogger(__name__)

# 상태
_status = "대기 중"
_tray_icon: pystray.Icon = None
_running = False

# 중복 실행 방지용 포트
LOCK_PORT = 54321


def setup_logging():
    """로깅 설정"""
    base_path = get_base_path()
    logs_dir = base_path / "logs"
    logs_dir.mkdir(exist_ok=True)

    # 로그 파일명 (날짜별)
    log_file = logs_dir / f"room-agent-{datetime.now().strftime('%Y-%m-%d')}.log"

    # 포맷터
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 파일 핸들러
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # 콘솔 핸들러
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # 루트 로거 설정
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logger.info(f"로그 파일: {log_file}")


def check_single_instance() -> bool:
    """중복 실행 확인 (포트 바인딩 방식)"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", LOCK_PORT))
        # 소켓을 열어둠 (프로세스 종료 시 자동 해제)
        return True
    except socket.error:
        return False


def create_tray_icon_image(color: str = "green") -> Image.Image:
    """트레이 아이콘 이미지 생성"""
    # 이미지 파일 사용 (icon.png 또는 icon.ico)
    base_path = get_base_path()
    for icon_name in ["icon.png", "icon.ico"]:
        icon_path = base_path / icon_name
        if icon_path.exists():
            try:
                img = Image.open(icon_path)
                # 64x64로 리사이즈
                img = img.resize((64, 64), Image.Resampling.LANCZOS)
                return img.convert("RGBA")
            except Exception as e:
                logger.warning(f"아이콘 로드 실패: {e}")
                break

    # 이미지 파일 없으면 동적 생성
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 색상 맵
    colors = {
        "green": (76, 175, 80),   # 대기 중
        "blue": (33, 150, 243),   # 작업 중
        "red": (244, 67, 54),     # 오류
    }

    fill_color = colors.get(color, colors["green"])

    # 원형 아이콘
    padding = 4
    draw.ellipse(
        [padding, padding, size - padding, size - padding],
        fill=fill_color
    )

    # 'R' 문자
    draw.text(
        (size // 2 - 10, size // 2 - 15),
        "R",
        fill="white",
    )

    return image


def update_tray_status(status: str, color: str = "green"):
    """트레이 아이콘 상태 업데이트"""
    global _status, _tray_icon

    _status = status
    logger.info(f"상태 변경: {status}")

    if _tray_icon:
        _tray_icon.icon = create_tray_icon_image(color)
        _tray_icon.title = f"room-agent: {status}"


def on_status_click(icon, item):
    """상태 확인 메뉴 클릭"""
    logger.info(f"현재 상태: {_status}")


def on_open_logs(icon, item):
    """로그 폴더 열기"""
    logs_dir = get_base_path() / "logs"
    if sys.platform == "win32":
        os.startfile(str(logs_dir))
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(logs_dir)])


def on_quit(icon, item):
    """종료"""
    global _running
    logger.info("종료 요청")
    _running = False
    stop_slack_app()
    icon.stop()
    # 프로세스 강제 종료 (Slack이 메인 스레드를 블로킹하므로)
    os._exit(0)


def create_tray_menu() -> pystray.Menu:
    """트레이 메뉴 생성"""
    return pystray.Menu(
        pystray.MenuItem(
            lambda item: f"상태: {_status}",
            on_status_click,
            enabled=False
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("로그 열기", on_open_logs),
        pystray.MenuItem("종료", on_quit),
    )


def run_tray_icon():
    """트레이 아이콘 실행 (별도 스레드)"""
    global _tray_icon

    _tray_icon = pystray.Icon(
        name="room-agent",
        icon=create_tray_icon_image(),
        title="room-agent: 시작 중...",
        menu=create_tray_menu()
    )

    logger.info("트레이 아이콘 실행")
    _tray_icon.run()


def main():
    """메인 함수"""
    global _running

    # 중복 실행 확인
    if not check_single_instance():
        print("room-agent가 이미 실행 중입니다.")
        sys.exit(1)

    # 로깅 설정
    setup_logging()
    logger.info("room-agent 시작")

    # 설정 로드 테스트
    try:
        config = get_config()
        logger.info("설정 로드 완료")
    except Exception as e:
        logger.error(f"설정 로드 실패: {e}")
        sys.exit(1)

    _running = True

    # 트레이 아이콘 시작 (별도 스레드)
    tray_thread = threading.Thread(target=run_tray_icon, daemon=True)
    tray_thread.start()

    # 트레이 아이콘 초기화 대기
    import time
    time.sleep(0.5)

    # Slack 앱 시작 (메인 스레드 - signal 처리 필요)
    try:
        update_tray_status("연결 중...", "blue")
        logger.info("Slack 앱 시작")
        update_tray_status("대기 중", "green")
        start_slack_app()  # 블로킹 호출
    except KeyboardInterrupt:
        logger.info("Ctrl+C로 종료")
    except Exception as e:
        logger.error(f"Slack 앱 오류: {e}", exc_info=True)
        update_tray_status("오류", "red")

    logger.info("room-agent 종료")


if __name__ == "__main__":
    main()
