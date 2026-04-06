"""
Vrew 자동화 로컬 에이전트

사용법:
  python3 agent.py

실행하면 연결 코드가 출력됩니다.
웹 UI에서 그 코드를 입력하면 이 PC의 Vrew 가 자동으로 동작합니다.
"""

import asyncio
import json
import sys
import os
import base64
import tempfile
from pathlib import Path

import websockets

# 클라우드 서버 URL
# 환경변수 VREW_SERVER 로 재정의 가능
# 예) VREW_SERVER=wss://vrewauto.up.railway.app/ws/agent python3 agent.py
SERVER_URL = os.environ.get("VREW_SERVER", "wss://web-production-972a4f.up.railway.app/ws/agent")

# vrew_cdp.py 가 같은 디렉토리에 있으므로 그대로 임포트
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vrew_cdp import VrewController, launch_vrew


class LogCapture:
    """stdout 을 가로채서 큐에 넣기"""
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self._buf = ""
        self._orig = sys.stdout

    def write(self, s):
        self._orig.write(s)  # 터미널에도 출력
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.queue.put_nowait(line)

    def flush(self):
        self._orig.flush()

    def restore(self):
        sys.stdout = self._orig


async def run_job(ws, params: dict):
    """작업 실행: 이미지 저장 → Vrew 자동화 → 로그 전송"""
    log_queue: asyncio.Queue = asyncio.Queue()

    # stdout 캡처 시작
    capture = LogCapture(log_queue)
    sys.stdout = capture

    ok = True
    error_msg = ""

    async def flush_logs():
        """큐에 쌓인 로그를 WebSocket으로 전송"""
        while not log_queue.empty():
            line = log_queue.get_nowait()
            await ws.send(json.dumps({"type": "log", "text": line}))

    try:
        # 1) base64 이미지 → 임시 파일로 저장
        images = params.get("images", [])
        tmp_dir = Path(tempfile.mkdtemp(prefix="vrew_"))
        image_paths = []
        for i, img in enumerate(images):
            b64 = img["data"].split(",")[-1]
            raw = base64.b64decode(b64)
            ext = Path(img["name"]).suffix or ".png"
            dest = tmp_dir / f"scene_{i:03d}{ext}"
            dest.write_bytes(raw)
            image_paths.append(str(dest))

        print(f"[에이전트] 이미지 {len(image_paths)}장 저장 완료")
        await flush_logs()

        # 2) Vrew 연결
        import urllib.request
        try:
            urllib.request.urlopen("http://localhost:9222/json/list", timeout=2)
        except Exception:
            print("[에이전트] Vrew 실행 중...")
            await flush_logs()
            launch_vrew()
            await asyncio.sleep(10)

        ctrl = VrewController()
        await ctrl.connect()
        await ctrl.resize_window()
        await asyncio.sleep(1)
        await ctrl.dismiss_recovery_popup()
        await ctrl.go_home()
        await flush_logs()

        # 3) 자동화 실행
        anim = str(params.get("auto_animation", "true")).lower() in ("true", "1", "on")
        voice = params.get("voice_name", "").strip() or None

        await ctrl.new_image_video(
            image_paths=image_paths,
            subtitles=params.get("subtitles", ""),
            aspect_ratio=params.get("aspect_ratio", "쇼츠 (9:16)"),
            fill_option=params.get("fill_option", "비율 유지하며 채우기"),
            voice_name=voice,
            auto_animation=anim,
        )
        await flush_logs()

        await ctrl.click_create_video()
        await flush_logs()

        await ctrl.wait_for_video_complete(timeout=300)
        await asyncio.sleep(1)
        await flush_logs()

        await ctrl.auto_clip_split(
            max_chars=int(params.get("max_chars", 20)),
            method=params.get("split_mode", "의미 기준"),
        )
        await flush_logs()

        await ctrl.set_subtitle_position(
            vertical_value=int(params.get("subtitle_position", -15))
        )
        await flush_logs()

        await ctrl.close()
        print("[에이전트] 자동화 완료!")
        await flush_logs()

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        ok = False
        error_msg = str(e)
        print(f"[에이전트] 오류: {e}")
        print(tb)
        await flush_logs()
    finally:
        capture.restore()
        # 임시 파일 정리
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    # 완료 신호
    await ws.send(json.dumps({"type": "done", "ok": ok, "error": error_msg}))


async def main():
    print("=" * 50)
    print("  Vrew 자동화 에이전트")
    print("=" * 50)
    print(f"서버 연결 중: {SERVER_URL}")

    retry_delay = 3

    while True:
        try:
            async with websockets.connect(
                SERVER_URL,
                max_size=100 * 1024 * 1024,  # 100MB (이미지 전송용)
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                # 연결 확인 + agent_id 수신
                raw = await ws.recv()
                data = json.loads(raw)
                agent_id = data["agent_id"]

                server_base = SERVER_URL.replace("ws://", "http://").replace("wss://", "https://").replace("/ws/agent", "")
                print(f"\n✓ 연결됨! 에이전트 코드: [{agent_id}]")
                print(f"\n브라우저에서 접속: {server_base}/?agent={agent_id}")
                print("-" * 50)
                print("작업을 기다리는 중... (Ctrl+C 로 종료)\n")

                retry_delay = 3  # 성공하면 재시도 딜레이 초기화

                # 브라우저 자동 오픈
                import subprocess
                server_base = SERVER_URL.replace("ws://", "http://").replace("wss://", "https://").replace("/ws/agent", "")
                url = f"{server_base}/?agent={agent_id}"
                subprocess.Popen(["open", url])

                # 메시지 수신 루프
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg["type"] == "job":
                        print(f"\n[에이전트] 작업 수신 → 실행 시작")
                        await run_job(ws, msg["params"])
                        print("[에이전트] 다음 작업 대기 중...\n")
                    elif msg["type"] == "pong":
                        pass

        except (websockets.exceptions.ConnectionClosed, OSError, ConnectionRefusedError) as e:
            print(f"\n[연결 끊김] {e}")
            print(f"{retry_delay}초 후 재연결 시도...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)
        except KeyboardInterrupt:
            print("\n에이전트 종료")
            break
        except Exception as e:
            print(f"\n[오류] {e}")
            await asyncio.sleep(retry_delay)


if __name__ == "__main__":
    asyncio.run(main())
