"""다우오피스 로그인 - 최초 1회만 실행"""

import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_FILE = Path(__file__).parent / "cookies.json"


async def main():
    print("=" * 50)
    print(" room-agent 로그인 설정")
    print("=" * 50)
    print()

    playwright = await async_playwright().start()

    try:
        print("브라우저 시작 중...")

        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        page.set_default_timeout(120000)

        print("다우오피스 페이지로 이동 중...")

        await page.goto(
            "https://gw.suresofttech.com/app/asset/27/list/reservation",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print()
        print("=" * 50)
        print(" 브라우저에서 로그인을 완료하세요!")
        print(" 로그인이 완료되면 자동으로 감지됩니다.")
        print("=" * 50)
        print()

        # 로그인 완료 자동 감지 (최대 5분 대기)
        for i in range(300):
            await asyncio.sleep(1)

            try:
                current_url = page.url
            except:
                print("브라우저가 닫혔습니다.")
                sys.exit(1)

            # 로그인 완료 확인
            if "login" not in current_url.lower() and ("reservation" in current_url or "asset" in current_url):
                print()
                print(f"현재 URL: {current_url}")
                print("로그인 성공!")

                # 쿠키 저장
                await asyncio.sleep(2)
                cookies = await context.cookies()

                with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, indent=2, ensure_ascii=False)

                print(f"쿠키 저장됨: {COOKIES_FILE}")
                print(f"쿠키 개수: {len(cookies)}")
                break

            if i % 30 == 0 and i > 0:
                print(f"로그인 대기 중... ({i}초)")

        else:
            print("시간 초과: 5분 내에 로그인하지 않았습니다.")
            await browser.close()
            await playwright.stop()
            sys.exit(1)

        print("브라우저를 닫는 중...")
        await browser.close()

    except Exception as e:
        print(f"오류 발생: {e}")
        sys.exit(1)
    finally:
        await playwright.stop()

    print()
    print("=" * 50)
    print(" 설정 완료!")
    print(" 이제 room-agent를 실행하세요:")
    print("   python -m src.main")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
