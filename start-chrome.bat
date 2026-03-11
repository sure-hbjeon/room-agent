@echo off
echo Starting Chrome with Remote Debugging...
echo.

start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\Google\Chrome\User Data" https://gw.suresofttech.com

echo Chrome started.
pause
