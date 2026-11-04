"""The office runtime: a never-ending loop that keeps every employee busy."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

from .agent import Agent
from .config import Config, load_config
from .connectors import EmailConnector, StripeConnector, TelegramConnector
from .db import DB
from .llm import make_llm
from .policy import Policy


class Office:
    def __init__(self, cfg: Config | None = None, llm=None, db_path: Path | None = None):
        self.cfg = cfg or load_config()
        self.db = DB(db_path or self.cfg.data_dir / "office.db")
        self.policy = Policy(self.cfg, self.db)
        self.llm = llm or make_llm()
        self.email = EmailConnector()
        self.telegram = TelegramConnector()
        self.stripe = StripeConnector()
        self.agents = {e.id: Agent(self, e) for e in self.cfg.employees}
        self.director = self.agents[self.cfg.director.id]
        self._busy: set[str] = set()
        self._running: set[asyncio.Task] = set()
        self.started_at = time.time()
        self.tick_count = 0
        # tasks interrupted by a restart go back to the queue
        self.db.execute("UPDATE tasks SET status='todo' WHERE status='in_progress'")

    @property
    def demo(self) -> bool:
        return type(self.llm).__name__ == "DemoLLM"

    # ================= main loop =================
    async def run_forever(self) -> None:
        self.db.log("system", "system", "Офис открыт. Сотрудники приступили к работе."
                    + (" (ДЕМО-режим без ИИ: добавьте ANTHROPIC_API_KEY)" if self.demo else ""))
        if not self.db.tasks(limit=1):
            self.bootstrap()
        while True:
            try:
                await self.tick()
            except Exception as e:  # the office never dies on one bad tick
                self.db.log("system", "error", f"Сбой цикла: {type(e).__name__}: {e}")
            await asyncio.sleep(float(self.cfg.auto("tick_seconds", 30)))

    def bootstrap(self) -> None:
        """First day at work: the director makes a plan."""
        self.db.create_task(
            "Стратегия запуска: что продаём первым",
            "Изучите миссию компании и рынок. Определите 3 первых продукта, которые можно быстро сделать "
            "и продать, с ценой и целевой аудиторией. Сохраните план в strategy/plan.md.",
            "researcher", "director", priority=1)

    async def tick(self) -> None:
        self.tick_count += 1
        self._reap()
        if self.policy.paused():
            self._set_idle_states()
            return
        if not self.policy.budget_ok():
            if self.db.get_setting("budget_warned") != datetime.now().strftime("%Y-%m-%d"):
                self.db.log("system", "system", "Дневной бюджет на ИИ исчерпан — офис отдыхает до завтра.")
                self.db.set_setting("budget_warned", datetime.now().strftime("%Y-%m-%d"))
            self._set_idle_states()
            return

        await asyncio.to_thread(self.poll_inbox)
        await asyncio.to_thread(self.dispatch_outbox)

        # Director: review + planning when there is something to do.
        if self.director.emp.id not in self._busy and self.director_agenda():
            self._spawn(self.director.emp.id, self.run_director)

        # Workers: everyone with a task gets to work (up to the parallel limit).
        limit = int(self.cfg.auto("max_parallel_workers", 3))
        for emp in self.cfg.workers:
            if len([b for b in self._busy if b != self.director.emp.id]) >= limit:
                break
            if emp.id in self._busy:
                continue
            task = self.db.next_task_for(emp.id)
            agent = self.agents[emp.id]
            if task:
                self._spawn(emp.id, lambda a=agent, t=task: a.work_on(t))
            elif agent.state != "break":
                agent.state, agent.bubble = "break", "Жду новую задачу ☕"
        self._set_idle_states()

    def _spawn(self, emp_id: str, fn) -> None:
        self._busy.add(emp_id)

        async def runner():
            try:
                await asyncio.to_thread(fn)
            except Exception as e:
                self.db.log(emp_id, "error", f"Сбой: {type(e).__name__}: {e}")
            finally:
                self._busy.discard(emp_id)

        t = asyncio.create_task(runner())
        self._running.add(t)
        t.add_done_callback(self._running.discard)

    def _reap(self) -> None:
        for emp_id, agent in self.agents.items():
            if emp_id not in self._busy and agent.state in ("working", "reviewing"):
                agent.state = "idle"

    def _set_idle_states(self) -> None:
        for emp_id, agent in self.agents.items():
            if emp_id not in self._busy and agent.state == "walking":
                agent.state = "idle"

    async def wait_idle(self) -> None:
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    # ================= director =================
    def director_agenda(self, mark_seen: bool = False) -> str:
        db = self.db
        review = db.tasks(status="review")
        blocked = db.tasks(status="blocked")
        outbound = db.query("SELECT * FROM outbox WHERE status='pending_review' ORDER BY id LIMIT 20")
        answers = db.query("SELECT * FROM escalations WHERE status='answered' AND answered_at > ?",
                           (float(db.get_setting("director_seen_answers", "0")),))
        drafts = db.query("SELECT * FROM products WHERE status='draft'")
        idle = [e for e in self.cfg.workers
                if e.id not in self._busy and not db.next_task_for(e.id)]
        backlog = len(db.tasks(status="todo")) + len(db.tasks(status="rework"))
        need_plan = idle and backlog < int(self.cfg.auto("min_backlog", 2)) + len(idle)
        open_esc = {e["task_id"] for e in db.query("SELECT task_id FROM escalations WHERE status='open'")}
        blocked = [t for t in blocked if t["id"] not in open_esc]   # waiting for the owner ≠ actionable

        if not (review or blocked or outbound or answers or drafts or need_plan):
            return ""
        parts = [f"Сегодня {datetime.now().strftime('%Y-%m-%d %H:%M')}. Повестка главного ассистента:"]
        if review:
            parts.append("ЗАДАЧИ НА ПРОВЕРКЕ (откройте файлы и проверьте каждую, затем review_task):\n" + "\n".join(
                f"[task #{t['id']}] исполнитель {t['assignee']}: {t['title']}\nОписание: {t['description']}\n"
                f"Отчёт: {t['result']}\nФайлы: {', '.join(t['files']) or 'нет'}" for t in review))
        if outbound:
            parts.append("ИСХОДЯЩИЕ СООБЩЕНИЯ НА ПРОВЕРКЕ (review_outbound для каждого):\n" + "\n".join(
                f"[msg #{m['id']}] {m['channel']} → {m['to_addr']} (автор {m['author']}, "
                f"{'первое письмо незнакомому' if m['is_cold'] else 'ответ/переписка'})\n"
                f"Тема: {m['subject']}\n{m['body']}" for m in outbound))
        if blocked:
            parts.append("ЗАБЛОКИРОВАННЫЕ ЗАДАЧИ (переформулируйте/отмените через review_task, поставьте новые):\n"
                         + "\n".join(f"[task #{t['id']}] {t['assignee']}: {t['title']} — {t['result']}" for t in blocked))
        if answers:
            parts.append("ОТ ВЛАДЕЛЬЦА (учтите и поставьте задачи, если нужно):\n" + "\n".join(
                (f"- УКАЗАНИЕ ВЛАДЕЛЬЦА: {a['answer']}" if a["agent"] == "owner" else
                 f"- Вопрос от {a['agent']}: {a['question']}\n  Ответ владельца: {a['answer']}") for a in answers))
            if mark_seen:
                db.set_setting("director_seen_answers", str(max(a["answered_at"] for a in answers)))
        if drafts:
            parts.append("ЧЕРНОВИКИ ПРОДУКТОВ (проверьте файлы; если готово — publish_product):\n" + "\n".join(
                f"#{p['id']} {p['name']} — {p['price']} {p['currency']}, папка {p['folder'] or '—'}: {p['description']}"
                for p in drafts))
        if need_plan:
            parts.append("СВОБОДНЫЕ СОТРУДНИКИ — поставьте каждому полезную задачу (create_task):\n"
                         + "\n".join(f"[idle {e.id}] {e.name}, {e.title}" for e in idle))
            parts.append(self.business_snapshot())
        return "\n\n".join(parts)

    def business_snapshot(self) -> str:
        db = self.db
        sales = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM sales")
        products = db.query("SELECT name,status,price,currency FROM products ORDER BY id DESC LIMIT 15")
        stages = db.query("SELECT stage, COUNT(*) AS n FROM contacts WHERE opted_out=0 GROUP BY stage")
        done = db.query("SELECT id,title,assignee FROM tasks WHERE status='done' ORDER BY updated_at DESC LIMIT 15")
        prod = ", ".join(f"{p['name']} ({p['status']}, {p['price']} {p['currency']})" for p in products) or "нет"
        funnel = ", ".join(f"{r['stage']}: {r['n']}" for r in stages) or "контактов нет"
        recent = "; ".join(f"#{t['id']} {t['title']} ({t['assignee']})" for t in done) or "ничего"
        return ("Состояние бизнеса:\n"
                f"- Продаж: {sales['n']} на сумму {sales['s']:.2f}\n"
                f"- Продукты: {prod}\n- Воронка: {funnel}\n- Недавно сделано: {recent}")

    def run_director(self) -> None:
        agenda = self.director_agenda(mark_seen=True)
        if not agenda:
            return
        d = self.director
        d.state = "reviewing"
        d.bubble = "Разбираю повестку"
        try:
            d.run(agenda)
        finally:
            d.state = "idle"

    # ================= communications =================
    def poll_inbox(self) -> None:
        items: list[dict] = []
        try:
            if self.email.can_receive:
                items += self.email.fetch_unseen()
        except Exception as e:
            self.db.log("system", "error", f"Почта (IMAP) недоступна: {e}")
        try:
            if self.telegram.configured:
                items += self.telegram.fetch_updates()
        except Exception as e:
            self.db.log("system", "error", f"Telegram недоступен: {e}")
        for item in items:
            self.receive(item)

    def receive(self, item: dict) -> int | None:
        """Store an inbound message and create a task for the sales team."""
        db = self.db
        if item.get("external_id") and db.one("SELECT id FROM inbox WHERE external_id=?", (item["external_id"],)):
            return None
        cur = db.execute(
            "INSERT INTO inbox(channel,from_addr,from_name,subject,body,external_id,received_at) VALUES(?,?,?,?,?,?,?)",
            (item["channel"], item["from_addr"], item.get("from_name", ""), item.get("subject", ""), item["body"],
             item.get("external_id"), time.time()))
        addr = item["from_addr"]
        contact = db.contact(addr)
        if self.policy.is_opt_out(item["body"]):
            db.upsert_contact(addr, channel=item["channel"], opted_out=1, stage="lost")
            db.log("system", "opt_out", f"{addr} отказался от сообщений — больше не пишем.")
            db.execute("UPDATE inbox SET processed=1 WHERE id=?", (cur.lastrowid,))
            return None
        if not contact:
            db.upsert_contact(addr, channel=item["channel"], name=item.get("from_name", ""), source="входящее")
        if not contact or contact["stage"] in ("lead", "contacted"):
            db.upsert_contact(addr, stage="replied")
        sales = next((e for e in self.cfg.workers if e.role == "sales"), self.cfg.workers[0])
        tid = db.create_task(
            f"Ответить {item.get('from_name') or addr} ({item['channel']})",
            f"Входящее сообщение от {item.get('from_name', '')} <{addr}> через {item['channel']}.\n"
            f"Тема: {item.get('subject', '')}\n\n{item['body']}\n\n"
            "Прочитайте историю (get_conversation), ответьте по существу через send_message "
            f"(channel={item['channel']}, to={addr}), обновите стадию контакта. "
            "Если клиент готов купить — дайте ссылку на оплату из list_products.",
            sales.id, "system", priority=1)
        db.execute("UPDATE inbox SET processed=1 WHERE id=?", (cur.lastrowid,))
        db.log("system", "inbound", f"Новое сообщение от {item.get('from_name') or addr} → задача #{tid}", task_id=tid)
        return tid

    def dispatch_outbox(self) -> None:
        db = self.db
        for msg in db.query("SELECT * FROM outbox WHERE status='approved' ORDER BY id LIMIT 20"):
            v = self.policy.final_check(msg)
            if not v.ok:
                db.execute("UPDATE outbox SET status='rejected', review_note=? WHERE id=?", (v.reason, msg["id"]))
                db.log("system", "blocked", f"Сообщение #{msg['id']} не отправлено: {v.reason}")
                continue
            try:
                status = self._send(msg, v.body)
                db.execute("UPDATE outbox SET status=?, body=?, sent_at=? WHERE id=?",
                           (status, v.body, time.time(), msg["id"]))
                label = "отправлено" if status == "sent" else "сохранено (канал не подключён)"
                db.log(msg["author"], "sent", f"Сообщение для {msg['to_addr']} {label}", msg_id=msg["id"])
            except Exception as e:
                db.execute("UPDATE outbox SET status='failed', review_note=? WHERE id=?", (str(e)[:500], msg["id"]))
                db.log("system", "error", f"Ошибка отправки #{msg['id']}: {e}")

    def _send(self, msg: dict, body: str) -> str:
        ch = msg["channel"]
        if ch == "email" and self.email.can_send:
            self.email.send(msg["to_addr"], msg["subject"], body)
            return "sent"
        if ch == "telegram" and self.telegram.configured:
            self.telegram.send(msg["to_addr"], body)
            return "sent"
        if ch == "telegram_channel" and self.telegram.configured and self.telegram.channel_id:
            self.telegram.post_to_channel(body)
            return "sent"
        return "dry_run"

    # ================= sales =================
    def record_sale(self, amount: float, currency: str, product_id: int | None, customer: str,
                    source: str, external_id: str | None = None) -> bool:
        if external_id and self.db.one("SELECT id FROM sales WHERE external_id=?", (external_id,)):
            return False
        self.db.execute("INSERT INTO sales(product_id,customer,amount,currency,source,external_id,ts) "
                        "VALUES(?,?,?,?,?,?,?)", (product_id, customer, amount, currency, source, external_id, time.time()))
        if customer:
            self.db.upsert_contact(customer, stage="customer")
        self.db.log("system", "sale", f"💰 Продажа: {amount:.2f} {currency.upper()} от {customer or 'клиента'}",
                    product_id=product_id)
        sales = next((e for e in self.cfg.workers if e.role == "sales"), None)
        if sales and customer:
            p = self.db.one("SELECT * FROM products WHERE id=?", (product_id,)) if product_id else None
            self.db.create_task(
                f"Отправить покупку клиенту {customer}",
                f"Клиент {customer} оплатил {amount} {currency}"
                + (f" за продукт «{p['name']}» (файлы в папке {p['folder']})." if p else ".")
                + " Поблагодарите, передайте продукт/доступ и предложите помощь. "
                  "Если файлы нельзя отправить письмом — объясните, как их получить, или эскалируйте владельцу.",
                sales.id, "system", priority=1)
        return True

    # ================= owner =================
    def answer_escalation(self, esc_id: int, answer: str) -> None:
        esc = self.db.one("SELECT * FROM escalations WHERE id=?", (esc_id,))
        if not esc:
            raise KeyError(esc_id)
        self.db.execute("UPDATE escalations SET answer=?, status='answered', answered_at=? WHERE id=?",
                        (answer, time.time(), esc_id))
        if esc["task_id"]:
            t = self.db.task(esc["task_id"])
            if t and t["status"] == "blocked":
                self.db.update_task(t["id"], status="todo",
                                    review_notes=f"Ответ владельца на вопрос «{esc['question']}»: {answer}")
        self.db.log("owner", "owner", f"Владелец ответил: {answer}")

    def give_directive(self, text: str) -> None:
        now = time.time()
        self.db.execute("INSERT INTO escalations(agent,question,answer,status,created_at,answered_at) "
                        "VALUES('owner','Указание владельца',?,'answered',?,?)", (text, now, now))
        self.db.log("owner", "owner", f"Указание владельца: {text}")

    def state(self) -> dict:
        db = self.db
        sales = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM sales")
        return {
            "company": self.cfg.company.get("name"),
            "demo": self.demo,
            "mode": self.cfg.auto("mode", "autonomous"),
            "paused": db.get_setting("paused") == "1",
            "stopped": db.get_setting("kill_switch") == "1",
            "spend_today": round(db.spend_today(), 4),
            "budget": self.cfg.limit("daily_llm_budget_usd"),
            "tick": self.tick_count,
            "connectors": {"email": self.email.can_send, "telegram": self.telegram.configured,
                           "stripe": self.stripe.configured},
            "employees": [{
                "id": a.emp.id, "name": a.emp.name, "title": a.emp.title, "role": a.emp.role,
                "color": a.emp.color, "state": a.state,
                "busy": a.emp.id in self._busy, "bubble": a.bubble, "task": a.current_task,
            } for a in self.agents.values()],
            "tasks": db.tasks(limit=300),
            "outbox": db.query("SELECT * FROM outbox ORDER BY id DESC LIMIT 60"),
            "inbox": db.query("SELECT * FROM inbox ORDER BY id DESC LIMIT 40"),
            "products": db.query("SELECT * FROM products ORDER BY id DESC"),
            "sales": {"count": sales["n"], "total": sales["s"],
                      "recent": db.query("SELECT * FROM sales ORDER BY id DESC LIMIT 20")},
            "contacts": db.query("SELECT * FROM contacts ORDER BY created_at DESC LIMIT 100"),
            "escalations": db.query("SELECT * FROM escalations ORDER BY status='open' DESC, id DESC LIMIT 30"),
            "events": db.recent_events(80),
        }


def main() -> None:
    asyncio.run(Office().run_forever())


if __name__ == "__main__":
    main()
