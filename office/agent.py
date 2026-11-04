"""An employee: system prompt + agent loop over its tools."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING

import anthropic

from .config import Employee
from .tools import Ctx, run_tool, tools_for

if TYPE_CHECKING:
    from .office import Office


BASE_RULES = """\
Правила офиса (обязательны):
1. Вы работаете автономно. Не спрашивайте разрешения у владельца на обычную работу — решайте сами
   в рамках своей роли. Владельцу пишите (escalate_to_owner) ТОЛЬКО по темам из списка эскалации.
2. Работа реальная: продукты должны быть рабочими и полезными, тексты — готовыми к публикации,
   код — работающим. Не выдумывайте факты, отзывы, цифры, клиентов и результаты.
3. Честность с людьми: не выдавайте себя за человека, если спросят — честно говорите, что вы ИИ-ассистент
   компании. Не обещайте того, что компания не может сделать. Никакого давления и манипуляций.
4. Антиспам: пишите только тем, кому предложение действительно может быть полезно, по публичным
   деловым контактам. Персонализируйте. Тех, кто отказался, больше не беспокоить.
5. Не тратьте реальные деньги, не подписывайте договоры, не делайте возвратов — это решает владелец.
6. Все результаты сохраняйте файлами в рабочую папку (write_file) и сдавайте задачу через submit_for_review.
7. Важные выводы сохраняйте в заметки (save_note), чтобы помнить их в следующих задачах.
8. Пишите кратко и по делу. Если задача невыполнима — report_blocker с причиной.
"""


def system_prompt(office: "Office", emp: Employee) -> str:
    cfg = office.cfg
    team = "\n".join(f"- {e.id}: {e.name}, {e.title} — {' '.join(e.duties.split())}" for e in cfg.employees)
    escalate = "\n".join(f"- {x}" for x in cfg.escalate_to_owner)
    c = cfg.company
    role_part = DIRECTOR_PROMPT if emp.is_director else WORKER_PROMPT
    return f"""Вы — {emp.name}, {emp.title} в компании «{c.get('name')}».
Владелец компании: {c.get('owner_name')}.

Миссия компании:
{' '.join(str(c.get('mission', '')).split())}

Ваши обязанности:
{' '.join(emp.duties.split())}

Команда:
{team}

{BASE_RULES}
Темы, которые решает только владелец (escalate_to_owner):
{escalate}

Лимиты: цены до {cfg.limit('max_price_without_owner_usd')} без согласования; реальные траты: \
{'разрешены' if cfg.limit('allow_real_spending') else 'запрещены'}.
Рабочий язык офиса: {c.get('language', 'ru')}. С клиентами пишите на их языке.
Подпись компании: {c.get('signature', c.get('name'))}

{role_part}"""


WORKER_PROMPT = """\
Как вы работаете: вам дают одну задачу. Выполните её полностью, сохраните результат файлами,
затем вызовите submit_for_review. Если задачу вернули на доработку — исправьте ВСЕ замечания.
Если для дела нужна работа коллеги — поставьте ему задачу (create_task)."""

DIRECTOR_PROMPT = """\
Вы — главный ассистент и контроль качества. Ваши обязанности на каждом цикле:
1. Проверить каждую сданную задачу: откройте файлы результата (read_file), найдите ошибки, неточности,
   выдуманные факты, нерабочий код, слабые тексты. Принимайте (approve) только то, что реально готово.
   Иначе — rework с конкретным списком исправлений.
2. Проверить каждое исходящее сообщение людям: честность, вежливость, польза для получателя, отсутствие
   спама и ложных обещаний, правильный получатель. Сомневаетесь — reject с причиной.
3. Разобрать блокеры: переформулировать задачу, назначить другому, или отменить.
4. Планировать работу так, чтобы все сотрудники были заняты задачами, ведущими к продажам:
   исследование → продукт → упаковка → продвижение → продажи → поддержка клиентов.
   Задачи ставьте конкретные, проверяемые, с понятным результатом.
5. Готовые проверенные продукты выставляйте на продажу (publish_product).
6. Ведите заметки о стратегии и о том, что работает (save_note)."""


class Agent:
    def __init__(self, office: "Office", emp: Employee):
        self.office = office
        self.emp = emp
        self.state = "idle"        # idle|working|reviewing|walking|break
        self.bubble = ""
        self.current_task: int | None = None

    @property
    def model(self) -> str:
        m = self.office.cfg.models
        return m.get("director" if self.emp.is_director else "worker", "claude-opus-5-5")

    @property
    def effort(self) -> str:
        m = self.office.cfg.models
        return m.get("director_effort" if self.emp.is_director else "worker_effort", "medium")

    def say(self, text: str, kind: str = "speech", **data) -> None:
        self.bubble = text[:200]
        self.office.db.log(self.emp.id, kind, text, **data)

    def run(self, prompt: str, task: dict | None = None) -> str:
        """Agent loop: model → tools → model … until it stops calling tools or the task is finished."""
        office = self.office
        db = office.db
        tools, registry = tools_for(self.emp.role, bool(office.cfg.auto("web_tools", True)))
        ctx = Ctx(office=office, employee=self.emp, task=task)
        system = system_prompt(office, self.emp)
        messages: list = [{"role": "user", "content": prompt}]
        max_steps = int(office.cfg.auto("max_steps_per_task", 30))
        final_text = ""

        for step in range(max_steps):
            if office.policy.paused():
                self.say("Офис на паузе — останавливаюсь.", "system")
                break
            if not office.policy.budget_ok():
                self.say("Дневной бюджет на ИИ исчерпан — прекращаю работу до завтра.", "system")
                break
            try:
                resp = office.llm.create(model=self.model, system=system, messages=messages, tools=tools,
                                         effort=self.effort)
            except anthropic.RateLimitError:
                self.say("Лимит запросов к ИИ — подожду.", "system")
                time.sleep(30)
                continue
            except (anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                self.say(f"Сеть/сервер ИИ недоступен: {type(e).__name__}", "error")
                break
            except anthropic.APIStatusError as e:
                self.say(f"Ошибка API {e.status_code}: {e.message}", "error")
                break

            db.add_usage(self.emp.id, resp.model or self.model, resp.input_tokens, resp.output_tokens, resp.cost_usd)
            if resp.stop_reason == "refusal":
                self.say(f"Модель отказалась выполнять: {resp.extra.get('refusal', '')}", "error")
                break
            if resp.text:
                final_text = resp.text
                self.say(resp.text, "speech")

            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "pause_turn":
                continue
            if not resp.tool_calls:
                if resp.stop_reason == "max_tokens":
                    messages.append({"role": "user", "content": "Ответ оборвался. Продолжайте, разбейте работу на части."})
                    continue
                break

            results = []
            for call in resp.tool_calls:
                tool = registry.get(call.name)
                if not tool:
                    out, err = f"Инструмент {call.name} вам недоступен.", True
                else:
                    self.bubble = _bubble_for(call.name, call.input)
                    out, err = run_tool(tool, ctx, call.input)
                db.log(self.emp.id, "tool", f"{call.name}: {out[:300]}", tool=call.name, error=err,
                       args=_short_args(call.input))
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": err})
            messages.append({"role": "user", "content": results})
            if ctx.finished:
                break
        else:
            self.say(f"Достиг лимита шагов ({max_steps}).", "system")

        # Worker stopped without submitting → hand the work in so it does not get stuck.
        if task and not ctx.finished:
            t = db.task(task["id"])
            if t and t["status"] == "in_progress":
                if office.policy.paused() or not office.policy.budget_ok():
                    db.update_task(task["id"], status="todo")
                else:
                    db.update_task(task["id"], status="review",
                                   result=(final_text or "Сотрудник завершил работу без отчёта.")[:4000])
                    db.log(self.emp.id, "submitted", f"Задача #{task['id']} автоматически сдана на проверку",
                           task_id=task["id"])
        return final_text

    def work_on(self, task: dict) -> None:
        db = self.office.db
        self.current_task = task["id"]
        self.state = "working"
        db.update_task(task["id"], status="in_progress")
        self.say(f"Беру задачу #{task['id']}: {task['title']}", "start", task_id=task["id"])
        try:
            self.run(self.task_prompt(task), task)
        finally:
            self.state = "walking"   # walks to the director with the result
            self.current_task = None

    def task_prompt(self, task: dict) -> str:
        notes = self.office.db.query("SELECT key,value FROM notes WHERE agent=? ORDER BY updated_at DESC LIMIT 15",
                                     (self.emp.id,))
        parts = [f"Сегодня {datetime.now().strftime('%Y-%m-%d %H:%M')}.",
                 f"Задача #{task['id']}: {task['title']}",
                 f"Приоритет: {task['priority']}. Поставил: {task['created_by']}.",
                 f"Описание:\n{task['description']}"]
        if task["status"] == "rework" or task["review_notes"]:
            parts.append(f"ЗАДАЧА ВОЗВРАЩЕНА НА ДОРАБОТКУ. Замечания главного ассистента:\n{task['review_notes']}\n"
                         f"Предыдущий результат:\n{task['result']}\nФайлы: {', '.join(task['files']) or '—'}")
        if notes:
            parts.append("Ваши заметки:\n" + "\n".join(f"- {n['key']}: {n['value']}" for n in notes))
        parts.append(f"Когда закончите — submit_for_review с task_id={task['id']}.")
        return "\n\n".join(parts)


def _short_args(args: dict) -> dict:
    return {k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in args.items()}


BUBBLES = {
    "write_file": "Пишу {path}", "read_file": "Читаю {path}", "send_message": "Пишу сообщение…",
    "create_task": "Ставлю задачу: {title}", "review_task": "Проверяю задачу #{task_id}",
    "review_outbound": "Проверяю письмо #{message_id}", "submit_for_review": "Сдаю работу на проверку",
    "add_contact": "Нашёл клиента: {name}", "create_product": "Оформляю продукт «{name}»",
    "publish_product": "Выставляю продукт на продажу", "escalate_to_owner": "Вопрос владельцу",
}


def _bubble_for(name: str, args: dict) -> str:
    tpl = BUBBLES.get(name, name)
    try:
        return tpl.format(**{k: (str(v)[:40]) for k, v in args.items()})
    except (KeyError, IndexError):
        return tpl.split("{")[0].strip() or name


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
