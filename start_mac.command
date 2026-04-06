#!/bin/bash
# 이 파일이 있는 폴더로 이동 (어디에 저장하든 상관없음)
cd "$(dirname "$0")"

echo "Vrew 자동화 에이전트 시작 중..."

# websockets 설치 (없으면 설치)
pip3 install websockets 2>/dev/null || pip install websockets 2>/dev/null

# 실행
python3 agent.py || python agent.py
