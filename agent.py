"""
Vrew 자동화 로컬 에이전트

처음 실행: 초대 코드 입력 → 토큰 발급 → 자동 저장
이후 실행: 저장된 토큰으로 자동 연결
"""

import asyncio
import json
import sys
import os
import base64
import tempfile
import subprocess
from pathlib import Path

import websockets

# 클라우드 서버 URL
SERVER_URL = os.environ.get("VREW_SERVER", "wss://web-production-972a4f.up.railway.app")

# 토큰 저장 경로
if sys.platform == "win32":
    TOKEN_FILE = Path(os.environ.get("APPDATA", "~")) / "vrew_agent_token"
else:
    TOKEN_FILE = Path.home() / ".vrew_agent_token"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vrew_cdp import VrewController, launch_vrew


# ── 토큰 관리 ──────────────────────────────────────────────────────────────────

def load_token() -> str | None:
    try:
        return TOKEN_FILE.read_text().strip() or None
    except Exception:
        return None

def save_token(token: str):
    TOKEN_FILE.write_text(token)

async def activate(invite_code: str) -> str | None:
    """초대 코드 → 토큰 발급"""
    import urllib.request
    url = SERVER_URL.replace("wss://", "https://").replace("ws://", "http://")
    req = urllib.request.Request(
        f"{url}/api/auth/activate",
        data=json.dumps({"invite_code": invite_code}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read())
            return data.get("token")
    except Exception as e:
        print(f"  오류: {e}")
        return None


# ── 로그 캡처 ─────────────────────────────────────────────────────────────────

class LogCapture:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self._buf = ""
        self._orig = sys.stdout

    def write(self, s):
        self._orig.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.queue.put_nowait(line)

    def flush(self): self._orig.flush()
    def restore(self): sys.stdout = self._orig


# ── 작업 실행 ─────────────────────────────────────────────────────────────────

async def run_job(ws, params: dict):
    log_queue: asyncio.Queue = asyncio.Queue()
    capture = LogCapture(log_queue)
    sys.stdout = capture
    ok = True
    error_msg = ""
    tmp_dir = None

    async def flush_logs():
        while not log_queue.empty():
            await ws.send(json.dumps({"type": "log", "text": log_queue.get_nowait()}))

    try:
        # 이미지 저장
        images = params.get("images", [])
        tmp_dir = Path(tempfile.mkdtemp(prefix="vrew_"))
        image_paths = []
        for i, img in enumerate(images):
            b64 = img["data"].split(",")[-1]
            ext = Path(img["name"]).suffix or ".png"
            dest = tmp_dir / f"scene_{i:03d}{ext}"
            dest.write_bytes(base64.b64decode(b64))
            image_paths.append(str(dest))

        print(f"[에이전트] 이미지 {len(image_paths)}장 저장 완료")
        await flush_logs()

        # Vrew 연결
        import urllib.request as ur
        try:
            ur.urlopen("http://localhost:9222/json/list", timeout=2)
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

        # 자동화 실행
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
        await ctrl.wait_for_video_complete(timeout=300)
        await asyncio.sleep(1)
        await flush_logs()
        await ctrl.auto_clip_split(
            max_chars=int(params.get("max_chars", 20)),
            method=params.get("split_mode", "의미 기준"),
        )
        await ctrl.set_subtitle_position(vertical_value=int(params.get("subtitle_position", -15)))
        await flush_logs()
        await ctrl.close()
        print("[에이전트] 자동화 완료!")
        await flush_logs()

    except Exception as e:
        import traceback
        ok = False
        error_msg = str(e)
        print(f"[에이전트] 오류: {e}\n{traceback.format_exc()}")
        await flush_logs()
    finally:
        capture.restore()
        if tmp_dir:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    await ws.send(json.dumps({"type": "done", "ok": ok, "error": error_msg}))


# ── 메인 ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 50)
    print("  Vrew 자동화 에이전트")
    print("=" * 50)

    # 토큰 로드: 환경변수 → 저장 파일 → 초대 코드 입력
    token = os.environ.get("VREW_TOKEN", "").strip().upper() or load_token()

    if not token:
        print("\n처음 실행입니다. 초대 코드를 입력하세요.")
        while True:
            code = input("초대 코드: ").strip().upper()
            if not code:
                continue
            print("인증 중...")
            token = await activate(code)
            if token:
                save_token(token)
                print(f"✓ 인증 완료! 토큰이 저장되었습니다.\n")
                break
            else:
                print("✗ 유효하지 않은 초대 코드입니다. 다시 입력해주세요.\n")

    ws_url = SERVER_URL.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/ws/agent?token={token}"

    retry_delay = 3

    while True:
        try:
            async with websockets.connect(
                ws_url,
                max_size=100 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                raw = await ws.recv()
                data = json.loads(raw)

                server_base = SERVER_URL.replace("wss://", "https://").replace("ws://", "http://")
                url = f"{server_base}/?t={token}"
                print(f"\n✓ 연결됨!")
                print(f"브라우저에서 접속: {url}")
                print("-" * 50)
                print("작업을 기다리는 중... (Ctrl+C 로 종료)\n")

                # 브라우저 자동 오픈
                if sys.platform == "win32":
                    os.startfile(url)
                else:
                    subprocess.Popen(["open", url])

                retry_delay = 3

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
            print(f"{retry_delay}초 후 재연결...")
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
