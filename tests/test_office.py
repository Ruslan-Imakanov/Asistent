import asyncio
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from office.agent import Agent
from office.llm import LLMResponse, ToolCall, _echo_content
from office.server import create_app
from office.tools import Ctx, TOOLS, run_tool


def tool(name):
    return next(t for t in TOOLS if t.name == name)


def ctx_for(office, emp_id, task=None):
    return Ctx(office=office, employee=office.cfg.employee(emp_id), task=task)


def run_ticks(office, n=3):
    async def go():
        for _ in range(n):
            await office.tick()
            await office.wait_idle()
    asyncio.run(go())


def test_full_cycle_task_work_review_done(office):
    tid = office.db.create_task("Сделать гайд", "Гайд для кофеен", "copywriter", "owner")
    run_ticks(office, 3)
    t = office.db.task(tid)
    assert t["status"] == "done", t
    assert (office.cfg.workspace_dir / "demo/result.md").exists()
    kinds = {e["kind"] for e in office.db.recent_events(500)}
    assert {"start", "submitted", "approved"} <= kinds


def test_director_keeps_everyone_busy(office):
    run_ticks(office, 1)
    assigned = {t["assignee"] for t in office.db.tasks()}
    assert {e.id for e in office.cfg.workers} <= assigned


def test_outbound_goes_through_director_and_gets_disclosure(office):
    c = ctx_for(office, "sales")
    out, err = run_tool(tool("send_message"), c, {"channel": "email", "to": "anna@example.com",
                                                   "subject": "Предложение", "body": "Здравствуйте, Анна!"})
    assert not err, out
    msg = office.db.one("SELECT * FROM outbox")
    assert msg["status"] == "pending_review" and msg["is_cold"] == 1
    run_ticks(office, 2)   # director approves, then dispatch (dry-run: no SMTP configured)
    msg = office.db.one("SELECT * FROM outbox")
    assert msg["status"] == "dry_run"
    assert "ИИ-ассистентом" in msg["body"]


def test_only_sales_and_marketing_can_write_to_people(office):
    out, err = run_tool(tool("send_message"), ctx_for(office, "developer"),
                        {"channel": "email", "to": "a@b.co", "body": "hi"})
    assert err


def test_opt_out_blocks_future_messages(office):
    office.receive({"channel": "email", "from_addr": "bob@example.com", "body": "Пожалуйста, больше не пишите мне"})
    assert office.db.contact("bob@example.com")["opted_out"] == 1
    out, err = run_tool(tool("send_message"), ctx_for(office, "sales"),
                        {"channel": "email", "to": "bob@example.com", "body": "Ещё одно предложение"})
    assert err and "отказался" in out


def test_inbound_creates_priority_task_for_sales(office):
    tid = office.receive({"channel": "email", "from_addr": "kate@shop.com", "from_name": "Катя",
                          "subject": "Цена?", "body": "Сколько стоит лендинг?", "external_id": "m1"})
    t = office.db.task(tid)
    assert t["assignee"] == "sales" and t["priority"] == 1
    assert office.receive({"channel": "email", "from_addr": "kate@shop.com", "body": "dup", "external_id": "m1"}) is None
    # a reply to someone who wrote first is not "cold"
    assert not office.policy.is_cold("email", "kate@shop.com")


def test_per_contact_and_cold_limits(office):
    office.cfg.limits["max_messages_per_contact_per_week"] = 1
    office.cfg.limits["max_cold_outreach_per_day"] = 2
    send = lambda to: run_tool(tool("send_message"), ctx_for(office, "sales"),
                               {"channel": "email", "to": to, "body": "Привет"})
    assert not send("a@x.com")[1]
    assert send("a@x.com")[1]          # 2nd message to same person in a week
    assert not send("b@x.com")[1]
    out, err = send("c@x.com")         # 3rd cold message today
    assert err and "первых писем" in out


def test_secrets_never_leave(office):
    out, err = run_tool(tool("send_message"), ctx_for(office, "sales"),
                        {"channel": "email", "to": "a@x.com", "body": "ключ sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"})
    assert err


def test_kill_switch_stops_everything(office):
    run_tool(tool("send_message"), ctx_for(office, "sales"), {"channel": "email", "to": "a@x.com", "body": "Привет"})
    office.db.execute("UPDATE outbox SET status='approved'")
    office.db.set_setting("kill_switch", "1")
    tid = office.db.create_task("x", "y", "developer", "owner")
    run_ticks(office, 1)
    office.dispatch_outbox()
    assert office.db.task(tid)["status"] == "todo"
    assert office.db.one("SELECT status FROM outbox")["status"] == "rejected"


def test_budget_exhausted_pauses_work(office):
    office.db.add_usage("x", "m", 1, 1, 999.0)
    tid = office.db.create_task("x", "y", "developer", "owner")
    run_ticks(office, 1)
    assert office.db.task(tid)["status"] == "todo"


def test_workspace_sandbox(office):
    out, err = run_tool(tool("write_file"), ctx_for(office, "developer"), {"path": "../../etc/evil", "content": "x"})
    assert err and "вне рабочей папки" in out
    out, err = run_tool(tool("write_file"), ctx_for(office, "developer"), {"path": "/abs/ok.txt", "content": "x"})
    assert not err and (office.cfg.workspace_dir / "abs/ok.txt").exists()


def test_price_limit_requires_owner(office):
    out, err = run_tool(tool("create_product"), ctx_for(office, "developer"),
                        {"name": "Big", "description": "d", "price": 100000})
    assert err and "владельца" in out
    out, err = run_tool(tool("create_product"), ctx_for(office, "developer"),
                        {"name": "Guide", "description": "d", "price": 19})
    assert not err
    out, err = run_tool(tool("publish_product"), ctx_for(office, "director"), {"product_id": 1})
    assert not err and office.db.one("SELECT status FROM products")["status"] == "on_sale"


def test_rework_limit_cancels(office):
    office.cfg.autonomy["max_rework"] = 1
    tid = office.db.create_task("t", "d", "developer", "owner")
    d = ctx_for(office, "director")
    for expected in ("rework", "cancelled"):
        office.db.update_task(tid, status="review")
        run_tool(tool("review_task"), d, {"task_id": tid, "verdict": "rework", "notes": "плохо"})
        assert office.db.task(tid)["status"] == expected


def test_escalation_blocks_then_owner_answer_unblocks(office):
    tid = office.db.create_task("Возврат", "Клиент просит возврат", "sales", "system")
    task = office.db.task(tid)
    office.db.update_task(tid, status="in_progress")
    out, err = run_tool(tool("escalate_to_owner"), ctx_for(office, "sales", task), {"question": "Вернуть $20?"})
    assert not err and office.db.task(tid)["status"] == "blocked"
    esc = office.db.one("SELECT * FROM escalations")
    office.answer_escalation(esc["id"], "Да, верните")
    t = office.db.task(tid)
    assert t["status"] == "todo" and "Да, верните" in t["review_notes"]


def test_agent_auto_submits_when_model_stops_without_tools(office):
    class Quiet:
        def create(self, **kw):
            return LLMResponse([{"type": "text", "text": "Сделал."}], [], "Сделал.", "end_turn", 1, 1, "m")
    office.llm = Quiet()
    tid = office.db.create_task("t", "d", "developer", "owner")
    Agent(office, office.cfg.employee("developer")).work_on(office.db.task(tid))
    assert office.db.task(tid)["status"] == "review"


def test_unknown_tool_is_reported_not_crashing(office):
    class Bad:
        n = 0
        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                c = ToolCall("t1", "rm_rf", {})
                return LLMResponse([{"type": "tool_use", "id": "t1", "name": "rm_rf", "input": {}}], [c], "", "tool_use")
            return LLMResponse([], [], "ok", "end_turn")
    office.llm = Bad()
    Agent(office, office.cfg.employee("developer")).run("hi")
    assert any("недоступен" in e["text"] for e in office.db.recent_events(50))


def test_echo_content_strips_blocks_before_fallback():
    class B:
        def __init__(self, t): self.type = t
    content = [B("thinking"), B("text"), B("tool_use"), B("fallback"), B("text"), B("tool_use")]
    assert [b.type for b in _echo_content(content)] == ["text", "text", "tool_use"]


def test_api_auth_and_stripe_webhook(office, monkeypatch):
    monkeypatch.setenv("OFFICE_ADMIN_TOKEN", "secret")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    office.stripe.webhook_secret = "whsec_test"
    app = create_app(office, run_loop=False)
    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/state", headers={"X-Admin-Token": "secret"}).status_code == 200
        payload = json.dumps({"type": "checkout.session.completed", "data": {"object": {
            "id": "cs_1", "amount_total": 1900, "currency": "usd",
            "customer_details": {"email": "buyer@x.com"}, "metadata": {}}}}).encode()
        ts = str(int(time.time()))
        sig = hmac.new(b"whsec_test", f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        assert client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
        r = client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": f"t={ts},v1={sig}"})
        assert r.status_code == 200
        client.post("/webhooks/stripe", content=payload, headers={"stripe-signature": f"t={ts},v1={sig}"})  # replay
    sales = office.db.query("SELECT * FROM sales")
    assert len(sales) == 1 and sales[0]["amount"] == 19.0
    assert office.db.contact("buyer@x.com")["stage"] == "customer"
    assert any(t["title"].startswith("Отправить покупку") for t in office.db.tasks())


def test_owner_directive_reaches_director(office):
    office.give_directive("Сделайте упор на кофейни")
    assert "Сделайте упор на кофейни" in office.director_agenda()
