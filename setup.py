"""room-agent 초기 설정 - 기존 Chrome 쿠키 복사"""

import shutil
import os
from pathlib import Path


def main():
    print("=" * 50)
    print("room-agent 초기 설정")
    print("=" * 50)
    print()

    # 경로 설정
    chrome_user_data = Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"))
    profile_dir = Path(__file__).parent / "chrome-profile"

    # 복사할 파일들 (로그인 세션 유지에 필요)
    files_to_copy = [
        "Default/Cookies",
        "Default/Login Data",
        "Default/Web Data",
        "Local State",
    ]

    print("1. Chrome을 완전히 종료하세요 (필수)")
    print()
    input("Chrome 종료 후 Enter를 누르세요...")
    print()

    # 프로필 디렉토리 생성
    profile_dir.mkdir(exist_ok=True)
    (profile_dir / "Default").mkdir(exist_ok=True)

    print("2. Chrome 쿠키 복사 중...")

    copied = 0
    for file_path in files_to_copy:
        src = chrome_user_data / file_path
        dst = profile_dir / file_path

        if src.exists():
            dst.parent.mkdir(exist_ok=True)
            try:
                shutil.copy2(src, dst)
                print(f"   ✓ {file_path}")
                copied += 1
            except Exception as e:
                print(f"   ✗ {file_path}: {e}")
        else:
            print(f"   - {file_path} (없음)")

    print()
    if copied > 0:
        print(f"완료! {copied}개 파일 복사됨")
        print()
        print("이제 room-agent를 실행할 수 있습니다:")
        print("  python -m src.main")
        print()
        print("config.yaml에서 headless: true 확인하세요.")
    else:
        print("복사된 파일이 없습니다. Chrome 경로를 확인하세요.")


if __name__ == "__main__":
    main()
