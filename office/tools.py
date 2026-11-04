"""Tools that employees can use. Each tool = JSON schema + handler; access is granted per role."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .office import Office
    from .config import Employee

MAX_FILE_WRITE = 300_000
MAX_FILE_READ = 60_000


class ToolError(Exception):
    pass


@dataclass
class Ctx:
    office: "Office"
    employee: "Employee"
    task: dict | None = None
    finished: bool = False   # set when the agent submitted / blocked the task


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[Ctx, dict], str]
    roles: set[str]          # {"*"} = everyone, "worker" = all non-directors

    def allowed(self, role: str) -> bool:
        return "*" in self.roles or role in self.roles or (role != "director" and "worker" in self.roles)

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}


# ---------------- workspace (files) ----------------
def _safe_path(ctx: Ctx, rel: str) -> Path:
    root = ctx.office.cfg.workspace_dir.resolve()
    p = (root / rel.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise ToolError("Путь вне рабочей папки офиса запрещён.")
    return p


def t_write_file(ctx: Ctx, a: dict) -> str:
    content = a["content"]
    if len(content.encode()) > MAX_FILE_WRITE:
        raise ToolError(f"Файл больше {MAX_FILE_WRITE} байт — разбейте на части.")
    p = _safe_path(ctx, a["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    ctx.office.db.log(ctx.employee.id, "file", f"Сохранил файл {a['path']}", path=a["path"])
    return f"Файл {a['path']} сохранён ({len(content)} символов)."


def t_read_file(ctx: Ctx, a: dict) -> str:
    p = _safe_path(ctx, a["path"])
    if not p.is_file():
        raise ToolError(f"Файл {a['path']} не найден.")
    data = p.read_text(encoding="utf-8", errors="replace")
    offset = int(a.get("offset") or 0)
    chunk = data[offset:offset + MAX_FILE_READ]
    if offset + MAX_FILE_READ < len(data):
        chunk += (f"\n\n[ПОКАЗАНО {offset}–{offset + MAX_FILE_READ} из {len(data)} символов. "
                  f"Вызовите read_file с offset={offset + MAX_FILE_READ}, чтобы читать дальше.]")
    return chunk


def t_list_files(ctx: Ctx, a: dict) -> str:
    p = _safe_path(ctx, a.get("path") or "")
    if not p.exists():
        return "Папка пуста или не существует."
    root = ctx.office.cfg.workspace_dir.resolve()
    files = sorted(str(f.relative_to(root)) for f in p.rglob("*") if f.is_file())
    return "\n".join(files[:500]) or "Файлов нет."


# ---------------- tasks ----------------
def _fmt_task(t: dict) -> str:
    return f"#{t['id']} [{t['status']}] P{t['priority']} → {t['assignee']}: {t['title']}"


def t_list_tasks(ctx: Ctx, a: dict) -> str:
    rows = ctx.office.db.tasks(status=a.get("status") or None, assignee=a.get("assignee") or None, limit=60)
    return "\n".join(_fmt_task(t) for t in rows) or "Задач нет."


def t_get_task(ctx: Ctx, a: dict) -> str:
    t = ctx.office.db.task(int(a["task_id"]))
    if not t:
        raise ToolError("Задача не найдена.")
    return json.dumps({k: t[k] for k in ("id", "title", "description", "assignee", "status", "priority",
                                          "result", "files", "review_notes", "attempts", "created_by")},
                      ensure_ascii=False, indent=1)


def t_create_task(ctx: Ctx, a: dict) -> str:
    office = ctx.office
    assignee = a["assignee"]
    emp = office.cfg.employee(assignee)
    if not emp or emp.is_director:
        raise ToolError(f"Нет такого сотрудника: {assignee}. Доступны: "
                        + ", ".join(e.id for e in office.cfg.workers))
    open_dupe = office.db.one("SELECT id FROM tasks WHERE lower(title)=lower(?) AND status NOT IN ('done','cancelled')",
                              (a["title"],))
    if open_dupe:
        raise ToolError(f"Такая задача уже есть: #{open_dupe['id']}.")
    tid = office.db.create_task(a["title"], a.get("description", ""), assignee, ctx.employee.id,
                                int(a.get("priority") or 3))
    office.db.log(ctx.employee.id, "task_created", f"Поставил задачу #{tid} для {emp.name}: {a['title']}",
                  task_id=tid, assignee=assignee)
    return f"Задача #{tid} создана и назначена {emp.name}."


def t_submit_for_review(ctx: Ctx, a: dict) -> str:
    tid = int(a["task_id"])
    t = ctx.office.db.task(tid)
    if not t or t["assignee"] != ctx.employee.id:
        raise ToolError("Это не ваша задача.")
    files = [f for f in a.get("files", []) if f]
    missing = [f for f in files if not _safe_path(ctx, f).is_file()]
    if missing:
        raise ToolError(f"Этих файлов нет в рабочей папке: {missing}. Сначала сохраните их.")
    ctx.office.db.update_task(tid, status="review", result=a["summary"], files=files)
    ctx.office.db.log(ctx.employee.id, "submitted", f"Сдал задачу #{tid} на проверку", task_id=tid)
    ctx.finished = True
    return "Задача отправлена главному ассистенту на проверку. Работа по задаче завершена."


def t_report_blocker(ctx: Ctx, a: dict) -> str:
    tid = int(a["task_id"])
    ctx.office.db.update_task(tid, status="blocked", result=f"БЛОКЕР: {a['reason']}")
    ctx.office.db.log(ctx.employee.id, "blocked", f"Задача #{tid} заблокирована: {a['reason']}", task_id=tid)
    ctx.finished = True
    return "Блокер зафиксирован, главный ассистент разберётся."


# ---------------- director-only ----------------
def t_review_task(ctx: Ctx, a: dict) -> str:
    office = ctx.office
    tid = int(a["task_id"])
    t = office.db.task(tid)
    if not t or t["status"] not in ("review", "blocked"):
        raise ToolError("Задача не ожидает проверки.")
    verdict = a["verdict"]
    notes = a.get("notes", "")
    max_rework = int(office.cfg.auto("max_rework", 3))
    if verdict == "approve":
        office.db.update_task(tid, status="done", review_notes=notes)
        office.db.log(ctx.employee.id, "approved", f"Принял задачу #{tid}: {notes}", task_id=tid)
        return f"Задача #{tid} принята."
    if verdict == "rework":
        attempts = t["attempts"] + 1
        if attempts > max_rework:
            office.db.update_task(tid, status="cancelled", review_notes=notes, attempts=attempts)
            office.db.log(ctx.employee.id, "cancelled",
                          f"Задача #{tid} снята после {max_rework} доработок", task_id=tid)
            return f"Лимит доработок исчерпан, задача #{tid} снята. Поставьте новую, более чёткую задачу."
        office.db.update_task(tid, status="rework", review_notes=notes, attempts=attempts)
        office.db.log(ctx.employee.id, "rework", f"Вернул #{tid} на доработку: {notes}", task_id=tid)
        return f"Задача #{tid} возвращена на доработку."
    if verdict == "cancel":
        office.db.update_task(tid, status="cancelled", review_notes=notes)
        office.db.log(ctx.employee.id, "cancelled", f"Отменил задачу #{tid}: {notes}", task_id=tid)
        return f"Задача #{tid} отменена."
    raise ToolError("verdict: approve | rework | cancel")


def t_review_outbound(ctx: Ctx, a: dict) -> str:
    office = ctx.office
    mid = int(a["message_id"])
    msg = office.db.one("SELECT * FROM outbox WHERE id=?", (mid,))
    if not msg or msg["status"] != "pending_review":
        raise ToolError("Сообщение не ожидает проверки.")
    note = a.get("note", "")
    if a["verdict"] == "approve":
        status = "pending_owner" if office.policy.needs_owner_approval_for_outbound() else "approved"
        office.db.execute("UPDATE outbox SET status=?, review_note=? WHERE id=?", (status, note, mid))
        office.db.log(ctx.employee.id, "msg_approved", f"Одобрил сообщение #{mid} для {msg['to_addr']}", msg_id=mid)
        return f"Сообщение #{mid} одобрено" + (" и ждёт владельца." if status == "pending_owner" else " и будет отправлено.")
    office.db.execute("UPDATE outbox SET status='rejected', review_note=? WHERE id=?", (note, mid))
    office.db.log(ctx.employee.id, "msg_rejected", f"Отклонил сообщение #{mid}: {note}", msg_id=mid)
    if msg["task_id"] and a.get("send_back", True):
        t = office.db.task(msg["task_id"])
        if t and t["status"] in ("review", "done", "in_progress"):
            office.db.update_task(t["id"], status="rework",
                                  review_notes=f"Сообщение #{mid} отклонено: {note}", attempts=t["attempts"] + 1)
    return f"Сообщение #{mid} отклонено."


def t_escalate(ctx: Ctx, a: dict) -> str:
    office = ctx.office
    task_id = ctx.task["id"] if ctx.task else None
    cur = office.db.execute("INSERT INTO escalations(agent,question,context,task_id,created_at) VALUES(?,?,?,?,?)",
                            (ctx.employee.id, a["question"], a.get("context", ""), task_id, time.time()))
    office.db.log(ctx.employee.id, "escalation", f"Вопрос владельцу: {a['question']}", escalation_id=cur.lastrowid)
    if task_id and ctx.employee.role != "director":
        office.db.update_task(task_id, status="blocked", result=f"Ждёт ответа владельца: {a['question']}")
        ctx.finished = True
    return "Вопрос передан владельцу. Ответ придёт новой задачей. Продолжайте с тем, что не зависит от ответа."


# ---------------- notes (long-term memory) ----------------
def t_save_note(ctx: Ctx, a: dict) -> str:
    ctx.office.db.execute(
        "INSERT INTO notes(agent,key,value,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(agent,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (ctx.employee.id, a["key"], a["value"], time.time()))
    return "Заметка сохранена."


def t_read_notes(ctx: Ctx, a: dict) -> str:
    who = a.get("agent") or ctx.employee.id
    rows = ctx.office.db.query("SELECT key,value FROM notes WHERE agent=? ORDER BY updated_at DESC LIMIT 50", (who,))
    return "\n".join(f"- {r['key']}: {r['value']}" for r in rows) or "Заметок нет."


# ---------------- contacts / CRM ----------------
def t_add_contact(ctx: Ctx, a: dict) -> str:
    db = ctx.office.db
    addr = a["addr"].strip()
    existing = db.contact(addr)
    if existing and existing["opted_out"]:
        raise ToolError("Этот человек отказался от контактов.")
    db.upsert_contact(addr, channel=a.get("channel", "email"), name=a.get("name", ""),
                      company=a.get("company", ""), source=a.get("source", ""), notes=a.get("notes", ""))
    db.log(ctx.employee.id, "contact", f"Добавил контакт {a.get('name') or addr}", addr=addr)
    return f"Контакт {addr} сохранён."


def t_update_contact(ctx: Ctx, a: dict) -> str:
    db = ctx.office.db
    if not db.contact(a["addr"]):
        raise ToolError("Контакт не найден.")
    fields = {k: a[k] for k in ("stage", "notes", "name", "company") if a.get(k)}
    db.upsert_contact(a["addr"], **fields)
    return "Контакт обновлён."


def t_list_contacts(ctx: Ctx, a: dict) -> str:
    sql, params = "SELECT * FROM contacts WHERE opted_out=0", []
    if a.get("stage"):
        sql += " AND stage=?"
        params.append(a["stage"])
    rows = ctx.office.db.query(sql + " ORDER BY created_at DESC LIMIT 100", params)
    return "\n".join(f"{r['addr']} ({r['channel']}) {r['name']} {r['company']} — {r['stage']}; {r['notes'][:120]}"
                     for r in rows) or "Контактов нет."


def t_conversation(ctx: Ctx, a: dict) -> str:
    db = ctx.office.db
    addr = a["addr"]
    ins = db.query("SELECT received_at AS ts, 'ВХОДЯЩЕЕ' AS dir, subject, body FROM inbox WHERE lower(from_addr)=lower(?)", (addr,))
    outs = db.query("SELECT created_at AS ts, 'ИСХОДЯЩЕЕ ('||status||')' AS dir, subject, body FROM outbox WHERE lower(to_addr)=lower(?)", (addr,))
    rows = sorted(ins + outs, key=lambda r: r["ts"])[-30:]
    return "\n\n".join(f"[{r['dir']}] {r['subject']}\n{r['body']}" for r in rows) or "Переписки нет."


# ---------------- communications ----------------
def t_send_message(ctx: Ctx, a: dict) -> str:
    office = ctx.office
    channel = a["channel"]
    to_addr = (a.get("to") or "").strip()
    if channel == "telegram_channel":
        to_addr = to_addr or "channel"
        if ctx.employee.role not in ("marketer", "sales"):
            raise ToolError("Публиковать в канал может только маркетолог.")
    elif ctx.employee.role not in ("sales", "marketer"):
        raise ToolError("Писать клиентам может только отдел продаж.")
    v = office.policy.precheck_outbound(channel, to_addr, a["body"])
    if not v.ok:
        raise ToolError(v.reason)
    is_cold = office.policy.is_cold(channel, to_addr)
    task_id = ctx.task["id"] if ctx.task else None
    cur = office.db.execute(
        "INSERT INTO outbox(channel,to_addr,subject,body,author,task_id,is_cold,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (channel, to_addr, a.get("subject", ""), a["body"], ctx.employee.id, task_id, int(is_cold), time.time()))
    if channel != "telegram_channel":
        c = office.db.contact(to_addr)
        if not c:
            office.db.upsert_contact(to_addr, channel=channel, source=f"outreach by {ctx.employee.id}")
        if not c or c["stage"] == "lead":
            office.db.upsert_contact(to_addr, stage="contacted")
    office.db.log(ctx.employee.id, "drafted", f"Подготовил сообщение для {to_addr} → на проверку",
                  msg_id=cur.lastrowid)
    return (f"Сообщение #{cur.lastrowid} поставлено в очередь. Главный ассистент проверит его и отправит. "
            "Подпись про ИИ-ассистента и возможность отписаться добавятся автоматически.")


# ---------------- products & sales ----------------
def t_create_product(ctx: Ctx, a: dict) -> str:
    office = ctx.office
    v = office.policy.price_ok(float(a["price"]))
    if not v.ok:
        raise ToolError(v.reason)
    folder = a.get("folder", "")
    if folder and not _safe_path(ctx, folder).exists():
        raise ToolError(f"Папки {folder} нет в рабочей папке.")
    cur = office.db.execute(
        "INSERT INTO products(name,description,price,currency,folder,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
        (a["name"], a["description"], float(a["price"]), a.get("currency", "usd").lower(), folder,
         ctx.employee.id, time.time()))
    office.db.log(ctx.employee.id, "product", f"Создал продукт «{a['name']}» за {a['price']} {a.get('currency', 'usd')}",
                  product_id=cur.lastrowid)
    return f"Продукт #{cur.lastrowid} создан как черновик. Главный ассистент выставит его на продажу (publish_product)."


def t_publish_product(ctx: Ctx, a: dict) -> str:
    office = ctx.office
    pid = int(a["product_id"])
    p = office.db.one("SELECT * FROM products WHERE id=?", (pid,))
    if not p:
        raise ToolError("Продукт не найден.")
    v = office.policy.price_ok(p["price"])
    if not v.ok:
        raise ToolError(v.reason)
    if office.policy.stopped():
        raise ToolError("Аварийная остановка.")
    stripe = office.stripe
    if stripe.configured:
        url = stripe.create_payment_link(p["name"], p["description"], p["price"], p["currency"], pid)
    else:
        url = f"(dry-run) оплата не подключена — продукт #{pid}"
    office.db.execute("UPDATE products SET status='on_sale', payment_url=? WHERE id=?", (url, pid))
    office.db.log(ctx.employee.id, "on_sale", f"Продукт «{p['name']}» выставлен на продажу", product_id=pid, url=url)
    return f"Продукт на продаже. Ссылка на оплату: {url}"


def t_list_products(ctx: Ctx, a: dict) -> str:
    rows = ctx.office.db.query("SELECT * FROM products ORDER BY id DESC LIMIT 50")
    sold = {r["product_id"]: r for r in ctx.office.db.query(
        "SELECT product_id, COUNT(*) AS n, SUM(amount) AS s FROM sales GROUP BY product_id")}
    return "\n".join(
        f"#{p['id']} [{p['status']}] {p['name']} — {p['price']} {p['currency']}; продаж: "
        f"{sold.get(p['id'], {}).get('n', 0)}; ссылка: {p['payment_url'] or '—'}; папка: {p['folder'] or '—'}"
        for p in rows) or "Продуктов пока нет."


# ---------------- registry ----------------
TOOLS: list[Tool] = [
    Tool("list_tasks", "Список задач офиса. Фильтры необязательны.",
         obj({"status": S, "assignee": S}), t_list_tasks, {"*"}),
    Tool("get_task", "Полная информация о задаче.", obj({"task_id": I}, ["task_id"]), t_get_task, {"*"}),
    Tool("create_task", "Поставить задачу сотруднику (assignee = id сотрудника). priority: 1 — срочно, 5 — когда-нибудь.",
         obj({"title": S, "description": S, "assignee": S, "priority": I}, ["title", "description", "assignee"]),
         t_create_task, {"*"}),
    Tool("submit_for_review", "Сдать СВОЮ задачу на проверку главному ассистенту. summary — что сделано и где результат; "
         "files — пути файлов результата в рабочей папке.",
         obj({"task_id": I, "summary": S, "files": {"type": "array", "items": S}}, ["task_id", "summary"]),
         t_submit_for_review, {"worker"}),
    Tool("report_blocker", "Задачу невозможно выполнить без внешней помощи — описать причину.",
         obj({"task_id": I, "reason": S}, ["task_id", "reason"]), t_report_blocker, {"worker"}),
    Tool("write_file", "Создать/перезаписать файл в рабочей папке офиса (продукты, код, тексты, отчёты).",
         obj({"path": S, "content": S}, ["path", "content"]), t_write_file, {"*"}),
    Tool("read_file", "Прочитать файл из рабочей папки.", obj({"path": S, "offset": I}, ["path"]), t_read_file, {"*"}),
    Tool("list_files", "Список файлов в рабочей папке (или подпапке).", obj({"path": S}), t_list_files, {"*"}),
    Tool("save_note", "Сохранить заметку в долговременную память (выводы, договорённости, идеи).",
         obj({"key": S, "value": S}, ["key", "value"]), t_save_note, {"*"}),
    Tool("read_notes", "Прочитать свои заметки (или заметки коллеги: agent=id).", obj({"agent": S}), t_read_notes, {"*"}),
    Tool("escalate_to_owner", "Задать вопрос владельцу. ТОЛЬКО для случаев из списка эскалации "
         "(деньги, договоры, возвраты, юридические риски). Всё остальное решайте сами.",
         obj({"question": S, "context": S}, ["question"]), t_escalate, {"*"}),
    Tool("add_contact", "Сохранить потенциального клиента. Только реальные публичные деловые контакты из открытых источников; "
         "в source укажите, где найден контакт.",
         obj({"addr": S, "channel": {"type": "string", "enum": ["email", "telegram"]}, "name": S, "company": S,
              "source": S, "notes": S}, ["addr", "source"]),
         t_add_contact, {"researcher", "sales", "marketer"}),
    Tool("update_contact", "Обновить контакт: stage (lead|contacted|replied|negotiating|customer|lost), заметки.",
         obj({"addr": S, "stage": {"type": "string", "enum": ["lead", "contacted", "replied", "negotiating", "customer", "lost"]},
              "notes": S, "name": S, "company": S}, ["addr"]),
         t_update_contact, {"sales", "director"}),
    Tool("list_contacts", "Список контактов (CRM).", obj({"stage": S}), t_list_contacts, {"*"}),
    Tool("get_conversation", "Вся переписка с контактом.", obj({"addr": S}, ["addr"]), t_conversation, {"*"}),
    Tool("send_message", "Написать человеку или опубликовать пост. channel: email (to=адрес), telegram (to=chat_id, "
         "только тем, кто сам написал боту), telegram_channel (пост в канал компании). Сообщение проверит главный ассистент.",
         obj({"channel": {"type": "string", "enum": ["email", "telegram", "telegram_channel"]}, "to": S,
              "subject": S, "body": S}, ["channel", "body"]),
         t_send_message, {"sales", "marketer"}),
    Tool("create_product", "Оформить готовый продукт для продажи (черновик). folder — папка с файлами продукта.",
         obj({"name": S, "description": S, "price": N, "currency": S, "folder": S}, ["name", "description", "price"]),
         t_create_product, {"developer", "copywriter", "sales", "director"}),
    Tool("list_products", "Каталог продуктов, цены, продажи, ссылки на оплату.", obj({}), t_list_products, {"*"}),
    # director only
    Tool("review_task", "Проверить сданную задачу. verdict: approve (принять), rework (вернуть с конкретными замечаниями), "
         "cancel (отменить бесполезную).",
         obj({"task_id": I, "verdict": {"type": "string", "enum": ["approve", "rework", "cancel"]}, "notes": S},
             ["task_id", "verdict", "notes"]), t_review_task, {"director"}),
    Tool("review_outbound", "Проверить исходящее сообщение перед отправкой человеку. verdict: approve | reject.",
         obj({"message_id": I, "verdict": {"type": "string", "enum": ["approve", "reject"]}, "note": S},
             ["message_id", "verdict", "note"]), t_review_outbound, {"director"}),
    Tool("publish_product", "Выставить проверенный продукт на продажу (создаёт реальную ссылку на оплату).",
         obj({"product_id": I}, ["product_id"]), t_publish_product, {"director"}),
]

WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
]
WEB_ROLES = {"director", "researcher", "sales", "marketer", "copywriter", "developer"}


def tools_for(role: str, web: bool) -> tuple[list[dict], dict[str, Tool]]:
    allowed = [t for t in TOOLS if t.allowed(role)]
    specs = [t.spec() for t in allowed]
    if web and role in WEB_ROLES:
        specs = specs + WEB_TOOLS
    return specs, {t.name: t for t in allowed}


def run_tool(tool: Tool, ctx: Ctx, args: dict) -> tuple[str, bool]:
    """Returns (result_text, is_error)."""
    missing = [k for k in tool.schema.get("required", []) if k not in args]
    if missing:
        return f"Не хватает параметров: {missing}", True
    try:
        return tool.handler(ctx, args), False
    except ToolError as e:
        return str(e), True
    except Exception as e:  # report, don't crash the office
        return f"Ошибка инструмента: {type(e).__name__}: {e}", True
