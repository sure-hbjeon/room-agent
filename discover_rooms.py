"""회의실 정보 자동 탐색 스크립트"""

import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_FILE = Path(__file__).parent / "cookies.json"

# 탐색할 층 정보
FLOORS = [
    {"floor_id": 27, "label": "11층"},
    {"floor_id": 26, "label": "10층"},
    {"floor_id": 25, "label": "9층"},
]


async def discover_rooms():
    """각 층의 회의실 정보 탐색"""

    if not COOKIES_FILE.exists():
        print("[ERROR] cookies.json이 없습니다. 먼저 python login.py를 실행하세요.")
        return

    print("=" * 50)
    print(" 회의실 정보 탐색")
    print("=" * 50)
    print()

    playwright = await async_playwright().start()

    try:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()

        # 쿠키 로드
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        await context.add_cookies(cookies)

        page = await context.new_page()

        all_results = []

        for floor in FLOORS:
            floor_id = floor["floor_id"]
            label = floor["label"]

            print(f"\n[{label}] (floor_id: {floor_id}) 탐색 중...")

            url = f"https://gw.suresofttech.com/app/asset/{floor_id}/list/reservation"
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # 로그인 확인
            if "login" in page.url.lower():
                print(f"  [WARN] 로그인 필요 - 건너뜀")
                continue

            # 회의실 행 찾기
            rows = page.locator("[data-matrix-row][data-row-key]")
            count = await rows.count()

            print(f"  발견된 회의실: {count}개")

            rooms = []
            for i in range(count):
                row = rows.nth(i)
                row_key = await row.get_attribute("data-row-key")

                # 회의실 이름 찾기
                name_el = row.locator("[data-matrix-row-header] .txt, .room_name, .asset_name").first
                if await name_el.count() > 0:
                    name = await name_el.text_content()
                    name = name.strip() if name else f"회의실 {row_key}"
                else:
                    # 대체 방법
                    header = row.locator("[data-matrix-row-header]").first
                    if await header.count() > 0:
                        name = await header.text_content()
                        name = name.strip() if name else f"회의실 {row_key}"
                    else:
                        name = f"회의실 {row_key}"

                # 이름에서 회의실 ID 추출 시도
                room_id_match = re.search(r"(\d+-\d+|\d+층.*|\w+교육장)", name)
                if room_id_match:
                    room_id = room_id_match.group(1).replace("회의실 ", "").strip()
                else:
                    room_id = name.replace("회의실 ", "").strip()

                rooms.append({
                    "row_key": int(row_key),
                    "name": name,
                    "room_id": room_id,
                })

                print(f"    - {name} (row_key: {row_key})")

            all_results.append({
                "floor_id": floor_id,
                "label": label,
                "rooms": rooms,
            })

        await browser.close()

        # 결과 출력 (config.yaml 형식)
        print("\n" + "=" * 50)
        print(" config.yaml에 추가할 내용:")
        print("=" * 50)
        print()
        print("room_priority:")

        for idx, floor in enumerate(all_results, 1):
            print(f"  - tier: {idx}")
            print(f"    label: \"{floor['label']}\"")
            print(f"    floor_id: {floor['floor_id']}")
            print("    rooms:")
            for room in floor["rooms"]:
                print(f"      - id: \"{room['room_id']}\"")
                print(f"        name: \"{room['name']}\"")
                print(f"        capacity: 6  # 수동 확인 필요")
                print(f"        row_key: {room['row_key']}")

        # JSON으로도 저장
        output_file = Path(__file__).parent / "debug" / "discovered_rooms.json"
        output_file.parent.mkdir(exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        print(f"\n[SAVE] 결과 저장: {output_file}")

    except Exception as e:
        print(f"[ERROR] 오류: {e}")
    finally:
        await playwright.stop()


if __name__ == "__main__":
    asyncio.run(discover_rooms())
