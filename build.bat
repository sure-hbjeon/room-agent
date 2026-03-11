@echo off
echo room-agent 빌드 시작...

REM 가상환경 활성화 (있는 경우)
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

REM Playwright 브라우저 설치
echo Playwright 브라우저 설치 중...
playwright install chromium

REM PyInstaller 빌드
echo PyInstaller 빌드 중...
pyinstaller room-agent.spec --noconfirm

REM 필요한 파일 복사
echo 배포 파일 복사 중...
if not exist dist\room-agent mkdir dist\room-agent
copy config.yaml dist\room-agent\
if exist credentials.json copy credentials.json dist\room-agent\

echo.
echo 빌드 완료!
echo 배포 폴더: dist\room-agent\
echo.
pause
