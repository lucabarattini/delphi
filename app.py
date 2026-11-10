"""Delphi's web server. Run it with `uv run app.py`, then open http://localhost:8000.

The agents live in scripts/oracle.py, the four data tools in scripts/tools.py, and the page in web/.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scripts.context_panel import AgentContexts, add_context_panel
from scripts.oracle import Trail, consult, reveal, sessions
from scripts.tools import NIGHTS, logos, order, resolve, slate

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

PING_SECONDS = 15
contexts: dict[str, AgentContexts] = {}

app = FastAPI()
add_context_panel(app, contexts)
app.mount("/assets", StaticFiles(directory=HERE / "web" / "assets"), name="assets")
app.mount("/data", StaticFiles(directory=HERE / "data"), name="data")


class Question(BaseModel):
    message: str
    session_id: str | None = None
    mode: str = "live"
    date: str = ""


class Order(BaseModel):
    ticker: str
    side: str = "yes"
    dollars: float


#-----Serve the single page----
@app.get("/")
def index():
    return FileResponse(HERE / "web" / "index.html", headers={"Cache-Control": "no-cache"})


#-----The slate for the right panel on page load, no model involved----
@app.get("/games")
def games(mode: str = "live", date: str = ""):
    try:
        mode, night = resolve(mode, date)
    except ValueError as error:
        return {"error": str(error), "games": []}

    found, error = slate(mode, night)
    return {"mode": mode, "night": night, "first_night": NIGHTS[0], "last_night": NIGHTS[-1], "error": error,
            "games": [{**game, "logos": logos(game)} for game in found]}


#-----Consult the oracle and stream every step as Server-Sent Events----
@app.get("/run")
async def run_stream(message: str, session_id: str = "", mode: str = "live", date: str = ""):
    session_id = session_id or str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    contexts[run_id] = AgentContexts()
    queue: asyncio.Queue = asyncio.Queue()
    hooks = Trail(contexts[run_id], queue.put_nowait)

    #-----Consult in the background and queue the answer, then the reveal in Replay----
    async def work():
        try:
            answer = await consult(message, session_id, mode, date, hooks)
            queue.put_nowait({"type": "done", **{key: answer[key] for key in ("markdown", "cards", "orders", "mode", "night")}})
            if answer["mode"] == "replay" and answer["cards"]:
                queue.put_nowait({"type": "reveal", "games": reveal(answer["night"], answer["cards"])})
        except Exception as error:
            queue.put_nowait({"type": "error", "message": f"{type(error).__name__}: {str(error)[:500]}"})
        queue.put_nowait(None)

    task = asyncio.create_task(work())

    #-----Stream queued events, with a ping every 15 s so proxies keep an idle stream open----
    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id, 'run_id': run_id})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), PING_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event, default=str)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


#-----The Project 1 shape: one answer, the session, and every tool call with its agent----
@app.post("/chat")
async def chat(request: Question):
    session_id = request.session_id or str(uuid.uuid4())
    try:
        hooks = Trail(AgentContexts(), lambda event: None)
        answer = await consult(request.message, session_id, request.mode, request.date, hooks)
    except Exception as error:
        return {"response": f"Delphi could not answer: {type(error).__name__}: {str(error)[:300]}",
                "session_id": session_id, "tool_calls": []}
    return {"response": answer["markdown"], "session_id": session_id, "tool_calls": answer["tool_calls"]}


#-----Forget one conversation, for the New chat button----
@app.post("/clear")
def clear(session_id: str = ""):
    sessions.pop(session_id, None)
    return {"status": "ok"}


#-----Walk the live order book for the amount typed on a card----
@app.post("/simulate")
def simulate(request: Order):
    return order(request.ticker.strip().upper(), request.side.strip().lower(), request.dollars)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
