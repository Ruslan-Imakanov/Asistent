"""Web server: the office 'game' UI, owner controls, webhooks. Runs the office loop in the background."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .office import Office

WEB = Path(__file__).resolve().parent.parent / "web"


def create_app(office: Office | None = None, run_loop: bool = True) -> FastAPI:
    holder: dict = {"office": office}
    sockets: set[WebSocket] = set()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        off = holder["office"] or Office()
        holder["office"] = off
        loop = asyncio.get_running_loop()

        def on_event(ev: dict) -> None:   # called from worker threads
            loop.call_soon_threadsafe(lambda: queue.full() or queue.put_nowait(ev))

        off.db.listeners.append(on_event)
        tasks = [asyncio.create_task(broadcaster())]
        if run_loop:
            tasks.append(asyncio.create_task(off.run_forever()))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()

    async def broadcaster():
        while True:
            ev = await queue.get()
            data = json.dumps({"type": "event", "event": ev}, ensure_ascii=False, default=str)
            for ws in list(sockets):
                try:
                    await ws.send_text(data)
                except Exception:
                    sockets.discard(ws)

    app = FastAPI(title="AI Office", lifespan=lifespan)

    def get_office() -> Office:
        return holder["office"]

    def check_token(token: str | None) -> None:
        expected = os.environ.get("OFFICE_ADMIN_TOKEN", "")
        if expected and not (token and hmac.compare_digest(token, expected)):
            raise HTTPException(401, "Нужен пароль (OFFICE_ADMIN_TOKEN)")

    def auth(x_admin_token: str | None = Header(default=None), token: str | None = Query(default=None)) -> None:
        check_token(x_admin_token or token)

    # ---------- UI ----------
    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    # ---------- state & control ----------
    @app.get("/api/state", dependencies=[Depends(auth)])
    def state():
        return JSONResponse(json.loads(json.dumps(get_office().state(), default=str)))

    @app.post("/api/pause", dependencies=[Depends(auth)])
    def pause():
        get_office().db.set_setting("paused", "1")
        get_office().db.log("owner", "owner", "Владелец поставил офис на паузу")
        return {"ok": True}

    @app.post("/api/resume", dependencies=[Depends(auth)])
    def resume():
        db = get_office().db
        db.set_setting("paused", "0")
        db.set_setting("kill_switch", "0")
        db.log("owner", "owner", "Владелец возобновил работу офиса")
        return {"ok": True}

    @app.post("/api/stop", dependencies=[Depends(auth)])
    def emergency_stop():
        db = get_office().db
        db.set_setting("kill_switch", "1")
        db.execute("UPDATE outbox SET status='rejected', review_note='аварийная остановка' "
                   "WHERE status IN ('pending_review','pending_owner','approved')")
        db.log("owner", "owner", "⛔ АВАРИЙНАЯ ОСТАНОВКА: вся работа и отправка сообщений остановлены")
        return {"ok": True}

    class OwnerTask(BaseModel):
        title: str
        description: str = ""
        assignee: str = ""
        priority: int = 2

    @app.post("/api/tasks", dependencies=[Depends(auth)])
    def add_task(t: OwnerTask):
        off = get_office()
        emp = off.cfg.employee(t.assignee) if t.assignee else None
        if emp and not emp.is_director:
            tid = off.db.create_task(t.title, t.description or t.title, emp.id, "owner", t.priority)
            off.db.log("owner", "owner", f"Владелец поставил задачу #{tid}: {t.title}", task_id=tid)
            return {"id": tid}
        # No assignee → an instruction for the director, who plans the work.
        off.give_directive(t.description or t.title)
        return {"ok": True}

    class Answer(BaseModel):
        answer: str

    @app.post("/api/escalations/{esc_id}/answer", dependencies=[Depends(auth)])
    def answer(esc_id: int, a: Answer):
        try:
            get_office().answer_escalation(esc_id, a.answer)
        except KeyError:
            raise HTTPException(404, "Вопрос не найден")
        return {"ok": True}

    class Decision(BaseModel):
        approve: bool

    @app.post("/api/outbox/{msg_id}/decision", dependencies=[Depends(auth)])
    def outbox_decision(msg_id: int, d: Decision):
        """Supervised mode: the owner approves messages that the director already approved."""
        db = get_office().db
        msg = db.one("SELECT * FROM outbox WHERE id=?", (msg_id,))
        if not msg or msg["status"] not in ("pending_owner", "pending_review"):
            raise HTTPException(404, "Сообщение не ожидает решения")
        db.execute("UPDATE outbox SET status=? WHERE id=?", ("approved" if d.approve else "rejected", msg_id))
        db.log("owner", "owner", f"Владелец {'одобрил' if d.approve else 'отклонил'} сообщение #{msg_id}")
        return {"ok": True}

    class Inbound(BaseModel):
        channel: str = "email"
        from_addr: str
        from_name: str = ""
        subject: str = ""
        body: str

    @app.post("/api/inbox", dependencies=[Depends(auth)])
    def inbound(m: Inbound):
        """Manually add an inbound message (e.g. a lead from a website form or a call)."""
        tid = get_office().receive(m.model_dump())
        return {"task_id": tid}

    # ---------- payments ----------
    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request):
        off = get_office()
        payload = await request.body()
        if not off.stripe.verify_webhook(payload, request.headers.get("stripe-signature", "")):
            raise HTTPException(400, "bad signature")
        event = json.loads(payload)
        if event.get("type") == "checkout.session.completed":
            s = event["data"]["object"]
            pid = (s.get("metadata") or {}).get("office_product_id")
            off.record_sale(
                amount=(s.get("amount_total") or 0) / 100,
                currency=s.get("currency") or "usd",
                product_id=int(pid) if pid and str(pid).isdigit() else None,
                customer=(s.get("customer_details") or {}).get("email") or "",
                source="stripe", external_id=s.get("id"))
        return {"received": True}

    # ---------- live feed ----------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token: str | None = None):
        try:
            check_token(token)
        except HTTPException:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        sockets.add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            sockets.discard(websocket)

    return app


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run("office.server:app", host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")), log_level="info")


if __name__ == "__main__":
    main()
