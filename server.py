"""
Vrew 자동화 클라우드 서버 (Railway/Render 배포용)

흐름:
  1. 사용자 PC의 agent.py 가 이 서버에 WebSocket 연결 → agent_id 발급
  2. 브라우저가 /?agent=XXXXXX 로 접속
  3. 브라우저가 이미지(base64) + 설정을 POST /api/run 으로 전송
  4. 서버가 해당 agent 에게 WebSocket으로 작업 전달
  5. agent 가 로컬 Vrew 를 제어하며 로그를 WebSocket으로 전송
  6. 서버가 로그를 SSE 로 브라우저에 실시간 스트리밍
"""

import uuid
import json
import asyncio
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# agent_id → { ws, log_queue, status }
agents: dict = {}


# ── 웹 UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(os.path.dirname(__file__), "index.html")
    return open(path).read()


# ── 에이전트 WebSocket ───────────────────────────────────────────────────────

@app.websocket("/ws/agent")
async def agent_websocket(ws: WebSocket):
    await ws.accept()
    agent_id = str(uuid.uuid4())[:6].upper()
    log_queue: asyncio.Queue = asyncio.Queue()
    agents[agent_id] = {"ws": ws, "log_queue": log_queue, "status": "idle"}

    try:
        await ws.send_text(json.dumps({"type": "connected", "agent_id": agent_id}))
        print(f"[+] 에이전트 연결: {agent_id}")

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg["type"] == "log":
                await log_queue.put({"type": "log", "text": msg["text"]})
            elif msg["type"] == "done":
                agents[agent_id]["status"] = "idle"
                await log_queue.put({"type": "done", "ok": msg.get("ok", True), "error": msg.get("error", "")})
            elif msg["type"] == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        print(f"[-] 에이전트 해제: {agent_id}")
    finally:
        agents.pop(agent_id, None)


# ── 에이전트 목록 ─────────────────────────────────────────────────────────────

@app.get("/api/agents")
async def list_agents():
    return {
        "agents": [
            {"id": k, "status": v["status"]}
            for k, v in agents.items()
        ]
    }


# ── 작업 실행 ─────────────────────────────────────────────────────────────────

@app.post("/api/run")
async def run_job(req: Request):
    body = await req.json()
    agent_id = body.get("agent_id", "").upper()

    if agent_id not in agents:
        raise HTTPException(404, f"에이전트 [{agent_id}] 가 연결되어 있지 않습니다. agent.py 를 실행했는지 확인하세요.")

    agent = agents[agent_id]
    if agent["status"] == "running":
        raise HTTPException(400, "이미 실행 중입니다. 완료 후 다시 시도하세요.")

    agent["status"] = "running"

    # 기존 큐 비우기
    while not agent["log_queue"].empty():
        agent["log_queue"].get_nowait()

    # 에이전트에 작업 전달 (이미지 base64 포함)
    await agent["ws"].send_text(json.dumps({"type": "job", "params": body}))
    return {"status": "sent"}


# ── 로그 SSE 스트림 ───────────────────────────────────────────────────────────

@app.get("/api/logs/{agent_id}")
async def stream_logs(agent_id: str):
    agent_id = agent_id.upper()
    if agent_id not in agents:
        raise HTTPException(404, "에이전트가 연결되어 있지 않습니다")

    agent = agents[agent_id]

    async def generate():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(agent["log_queue"].get(), timeout=25)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                if msg["type"] == "log":
                    text = msg["text"].replace("\n", " ")
                    yield f"data: {text}\n\n"
                elif msg["type"] == "done":
                    if msg["ok"]:
                        yield "event: done\ndata: ok\n\n"
                    else:
                        err = msg["error"].replace("\n", " ")
                        yield f"event: done\ndata: error:{err}\n\n"
                    break
        except Exception as e:
            yield f"event: done\ndata: error:{e}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    print(f"서버 시작: http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
