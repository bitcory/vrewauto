"""
Vrew 자동화 클라우드 서버 (Railway 배포용)

환경변수:
  INVITE_CODES  = 콤마로 구분된 초대 코드 목록  (예: ALPHA001,BETA002)
  SECRET_KEY    = 토큰 서명용 비밀키
  ADMIN_KEY     = 관리자 페이지 접근 비밀번호
  PORT          = 서버 포트 (기본 8080)
"""

import hmac
import hashlib
import uuid
import json
import asyncio
import os
import sys
import subprocess
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE        = os.path.dirname(__file__)
SECRET_KEY  = os.environ.get("SECRET_KEY", "vrew-secret-change-me")
ADMIN_KEY   = os.environ.get("ADMIN_KEY", "admin1234")

# 초대 코드 목록 (환경변수 + 런타임 추가분)
_env_codes  = [c.strip() for c in os.environ.get("INVITE_CODES", "").split(",") if c.strip()]
invite_codes: set = set(_env_codes)

# token → { ws, log_queue, status, invite_code, connected_at }
sessions: dict = {}

# token → { logs, ok, error, started_at, finished_at }
results: dict = {}


# ── 토큰 유틸 ─────────────────────────────────────────────────────────────────

def make_token(invite_code: str) -> str:
    """초대 코드 → 영구 토큰 (HMAC-SHA256, 앞 16자)"""
    return hmac.new(
        SECRET_KEY.encode(), invite_code.encode(), hashlib.sha256
    ).hexdigest()[:16].upper()

def token_to_invite(token: str) -> str | None:
    """토큰 → 초대 코드 역추적"""
    for code in invite_codes:
        if make_token(code) == token:
            return code
    return None


# ── 웹 UI & 파일 다운로드 ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return open(os.path.join(BASE, "index.html")).read()

@app.get("/download/vrew-agent")
async def dl_mac():
    path = os.path.join(BASE, "dist", "vrew-agent")
    if not os.path.exists(path):
        raise HTTPException(404, "아직 빌드되지 않았습니다. 잠시 후 다시 시도하세요.")
    return FileResponse(path, media_type="application/octet-stream", filename="vrew-agent")

@app.get("/download/vrew-agent.exe")
async def dl_win():
    path = os.path.join(BASE, "dist", "vrew-agent.exe")
    if not os.path.exists(path):
        raise HTTPException(404, "아직 빌드되지 않았습니다. 잠시 후 다시 시도하세요.")
    return FileResponse(path, media_type="application/octet-stream", filename="vrew-agent.exe")


# ── 초대 코드 인증 ─────────────────────────────────────────────────────────────

@app.post("/api/auth/activate")
async def activate(req: Request):
    """초대 코드 → 영구 토큰 발급"""
    body = await req.json()
    code = body.get("invite_code", "").strip().upper()

    if code not in invite_codes:
        raise HTTPException(401, "유효하지 않은 초대 코드입니다")

    token = make_token(code)
    return {"token": token}


# ── 에이전트 WebSocket ───────────────────────────────────────────────────────

@app.websocket("/ws/agent")
async def agent_websocket(ws: WebSocket):
    token = ws.query_params.get("token", "").upper()
    invite = token_to_invite(token)

    if not invite:
        await ws.close(code=4001)
        return

    await ws.accept()
    log_queue: asyncio.Queue = asyncio.Queue()

    sessions[token] = {
        "ws": ws,
        "log_queue": log_queue,
        "status": "idle",
        "invite_code": invite,
        "connected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        await ws.send_text(json.dumps({"type": "connected", "token": token}))
        print(f"[+] 연결: {invite} ({token[:8]}...)")

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg["type"] == "log":
                await log_queue.put({"type": "log", "text": msg["text"]})
                if token in results:
                    results[token]["logs"].append(msg["text"])

            elif msg["type"] == "done":
                sessions[token]["status"] = "idle"
                ok = msg.get("ok", True)
                error = msg.get("error", "")
                await log_queue.put({"type": "done", "ok": ok, "error": error})
                if token in results:
                    results[token].update({
                        "ok": ok, "error": error,
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })

            elif msg["type"] == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        print(f"[-] 해제: {invite}")
    finally:
        sessions.pop(token, None)


# ── 내 세션 확인 ──────────────────────────────────────────────────────────────

@app.get("/api/me")
async def me(token: str):
    token = token.upper()
    invite = token_to_invite(token)
    if not invite:
        raise HTTPException(401, "유효하지 않은 토큰")

    connected = token in sessions
    status = sessions[token]["status"] if connected else "offline"
    return {"valid": True, "connected": connected, "status": status}


# ── 작업 실행 ─────────────────────────────────────────────────────────────────

@app.post("/api/run")
async def run_job(req: Request):
    body = await req.json()
    token = body.get("token", "").upper()

    if not token_to_invite(token):
        raise HTTPException(401, "유효하지 않은 토큰")
    if token not in sessions:
        raise HTTPException(404, "에이전트가 연결되어 있지 않습니다. vrew-agent 를 실행해주세요.")

    session = sessions[token]
    if session["status"] == "running":
        raise HTTPException(400, "이미 실행 중입니다")

    session["status"] = "running"
    while not session["log_queue"].empty():
        session["log_queue"].get_nowait()

    results[token] = {
        "logs": [], "ok": None, "error": "",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
    }

    await session["ws"].send_text(json.dumps({"type": "job", "params": body}))
    return {"status": "sent"}


# ── 로그 SSE ──────────────────────────────────────────────────────────────────

@app.get("/api/logs/{token}")
async def stream_logs(token: str):
    token = token.upper()
    if token not in sessions:
        raise HTTPException(404, "에이전트가 연결되어 있지 않습니다")

    session = sessions[token]

    async def generate():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(session["log_queue"].get(), timeout=25)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if msg["type"] == "log":
                    yield f"data: {msg['text'].replace(chr(10), ' ')}\n\n"
                elif msg["type"] == "done":
                    status = "ok" if msg["ok"] else f"error:{msg['error']}"
                    yield f"event: done\ndata: {status}\n\n"
                    break
        except Exception as e:
            yield f"event: done\ndata: error:{e}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 마지막 결과 ───────────────────────────────────────────────────────────────

@app.get("/api/result/{token}")
async def get_result(token: str):
    token = token.upper()
    if token not in results:
        return {"found": False}
    r = results[token]
    running = token in sessions and sessions[token]["status"] == "running"
    return {"found": True, "running": running, **r}


# ── 관리자 페이지 ─────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(key: str = ""):
    if key != ADMIN_KEY:
        return HTMLResponse("<h2>403 Forbidden</h2>", status_code=403)

    rows = ""
    for code in sorted(invite_codes):
        token = make_token(code)
        connected = token in sessions
        status = sessions[token]["status"] if connected else "오프라인"
        color = "#4caf50" if connected else "#aaa"
        rows += f"<tr><td>{code}</td><td>{token[:8]}...</td><td style='color:{color}'>{status}</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>관리자</title>
    <style>body{{font-family:sans-serif;padding:30px}}table{{border-collapse:collapse;width:100%}}
    td,th{{border:1px solid #ddd;padding:10px;text-align:left}}th{{background:#f5f5f5}}
    input{{padding:8px;font-size:14px;width:200px}}button{{padding:8px 16px;background:#00b8d4;color:#fff;border:none;cursor:pointer;border-radius:4px}}</style>
    </head><body>
    <h2>Vrew 자동화 관리자</h2>
    <h3>초대 코드 목록</h3>
    <table><tr><th>초대 코드</th><th>토큰</th><th>상태</th></tr>{rows}</table>
    <h3 style="margin-top:24px">새 초대 코드 추가</h3>
    <form method="post" action="/admin/add-code?key={key}">
      <input name="code" placeholder="예: USER001" required>
      <button type="submit">추가</button>
    </form>
    </body></html>"""

@app.post("/admin/add-code", response_class=HTMLResponse)
async def add_code(req: Request, key: str = ""):
    if key != ADMIN_KEY:
        raise HTTPException(403)
    form = await req.form()
    code = form.get("code", "").strip().upper()
    if code:
        invite_codes.add(code)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/admin?key={key}", status_code=303)


# ── 에이전트 실행 (로컬 전용) ─────────────────────────────────────────────────

_agent_proc = None

@app.post("/api/start-agent")
async def start_agent(req: Request):
    global _agent_proc
    body = await req.json()
    token = body.get("token", "").upper()

    if not token_to_invite(token):
        raise HTTPException(401, "유효하지 않은 토큰")

    # 이미 연결된 경우
    if token in sessions:
        return {"status": "already_connected"}

    # 클라우드 환경 감지 (Railway 등)
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"):
        raise HTTPException(400, "클라우드 환경에서는 직접 에이전트를 실행할 수 없습니다. 로컬에서 vrew-agent를 실행하세요.")

    # 이미 실행 중인 프로세스 확인
    if _agent_proc and _agent_proc.poll() is None:
        return {"status": "already_running"}

    agent_path = os.path.join(BASE, "agent.py")
    env = os.environ.copy()
    env["VREW_TOKEN"] = token  # 토큰 직접 전달 (초대코드 입력 스킵)

    _agent_proc = subprocess.Popen(
        [sys.executable, agent_path],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"status": "started", "pid": _agent_proc.pid}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    print(f"서버 시작: http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
