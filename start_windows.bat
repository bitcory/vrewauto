@echo off
chcp 65001 >nul
echo Vrew 자동화 에이전트 시작 중...
echo.

cd /d "%~dp0"

REM websockets 설치 (없으면 설치)
pip install websockets >nul 2>&1
if errorlevel 1 (
    pip3 install websockets >nul 2>&1
)

REM Python 버전에 따라 실행
python agent.py 2>nul
if errorlevel 1 (
    python3 agent.py
)

pause
