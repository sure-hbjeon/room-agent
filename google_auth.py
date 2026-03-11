"""Google Calendar 인증 - 최초 1회만 실행

사용 전 설정:
1. Google Cloud Console (https://console.cloud.google.com) 접속
2. 프로젝트 선택 또는 생성
3. API 및 서비스 > 사용자 인증 정보 > OAuth 클라이언트 ID 생성 (데스크톱 앱)
4. credentials.json 다운로드하여 이 폴더에 저장
5. API 및 서비스 > OAuth 동의 화면 > 테스트 사용자 추가 (본인 이메일)
6. 이 스크립트 실행: python google_auth.py
"""

import sys
from pathlib import Path

# 프로젝트 경로 설정
sys.path.insert(0, str(Path(__file__).parent))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
BASE_PATH = Path(__file__).parent
CREDENTIALS_FILE = BASE_PATH / "credentials.json"
TOKEN_FILE = BASE_PATH / "token.json"


def main():
    print("=" * 50)
    print(" Google Calendar 인증 설정")
    print("=" * 50)
    print()

    # credentials.json 확인
    if not CREDENTIALS_FILE.exists():
        print(f"[X] credentials.json 파일이 없습니다!")
        print()
        print("설정 방법:")
        print("1. Google Cloud Console 접속")
        print("   https://console.cloud.google.com")
        print()
        print("2. API 및 서비스 > 사용자 인증 정보")
        print("3. OAuth 클라이언트 ID 생성 (데스크톱 앱)")
        print("4. credentials.json 다운로드")
        print(f"5. 파일을 여기에 저장: {CREDENTIALS_FILE}")
        print()
        print("6. OAuth 동의 화면 > 테스트 사용자에 본인 이메일 추가")
        return

    creds = None

    # 기존 토큰 확인
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            print(f"기존 토큰 발견: {TOKEN_FILE}")
        except Exception as e:
            print(f"토큰 로드 실패: {e}")

    # 토큰 갱신 또는 새로 획득
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("토큰 갱신 중...")
            try:
                creds.refresh(Request())
                print("[OK] 토큰 갱신 완료")
            except Exception as e:
                print(f"토큰 갱신 실패: {e}")
                creds = None

        if not creds:
            print()
            print("브라우저에서 Google 로그인을 진행합니다...")
            print()
            print("[!]  '이 앱은 확인되지 않았습니다' 경고가 나타나면:")
            print("   → '고급' 클릭 → 'room-agent(으)로 이동(안전하지 않음)' 클릭")
            print()

            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_FILE), SCOPES
                )
                creds = flow.run_local_server(port=0)
            except Exception as e:
                print(f"[X] 인증 실패: {e}")
                return

        # 토큰 저장
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
        print(f"[OK] 토큰 저장 완료: {TOKEN_FILE}")

    # 연결 테스트 (calendar.events 스코프로 가능한 테스트)
    print()
    print("Google Calendar 연결 테스트 중...")
    try:
        service = build("calendar", "v3", credentials=creds)
        # primary 캘린더에서 이벤트 목록 조회 (calendar.events 스코프로 가능)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        events_result = service.events().list(
            calendarId="primary",
            timeMin=now,
            maxResults=5,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])

        print(f"[OK] 연결 성공! 기본 캘린더 접근 확인됨")
        print()
        if events:
            print(f"향후 일정 {len(events)}개:")
            for event in events:
                start = event["start"].get("dateTime", event["start"].get("date"))
                print(f"  - {start[:16]} {event.get('summary', '(제목 없음)')}")
        else:
            print("향후 일정이 없습니다.")

    except Exception as e:
        print(f"[X] 연결 테스트 실패: {e}")
        return

    print()
    print("=" * 50)
    print(" 설정 완료!")
    print(" 이제 room-agent가 Google Calendar와 연동됩니다.")
    print("=" * 50)


if __name__ == "__main__":
    main()
