from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import re
import sqlite3
import string
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path as ApiPath
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = DATA_DIR / "state"
LOG_DIR = DATA_DIR / "logs"
EXPORT_DIR = DATA_DIR / "exports"
AGENTS_DIR = BASE_DIR / "agents"
DB_PATH = STATE_DIR / "arx_gateway.sqlite3"

for folder in [DATA_DIR, STATE_DIR, LOG_DIR, EXPORT_DIR, AGENTS_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

APP_TITLE = "ARX ChatGPT Action Gateway"
APP_VERSION = "3.0.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log_event(message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    logfile = LOG_DIR / f"gateway_{datetime.now().strftime('%Y%m%d')}.log"
    with logfile.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="")


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def load_config() -> Dict[str, Any]:
    cfg_path = BASE_DIR / "config.json"
    if not cfg_path.exists():
        return {}
    return json.loads(cfg_path.read_text(encoding="utf-8"))


CONFIG = load_config()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def mode() -> str:
    return env("ARX_MODE", "mock").lower() or "mock"


def model_name() -> str:
    return env("OPENAI_MODEL", "gpt-5.5") or "gpt-5.5"


def max_output_tokens() -> int:
    try:
        return int(env("ARX_MAX_OUTPUT_TOKENS", "1800"))
    except ValueError:
        return 1800


def max_workers() -> int:
    try:
        return max(1, int(env("ARX_MAX_WORKERS", "4")))
    except ValueError:
        return 4


def make_cycle_id() -> str:
    suffix = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"C{datetime.now().strftime('%Y%m%d_%H%M%S')}_{suffix}"


def make_approval_code() -> str:
    return "ARX-" + "".join(random.choice(string.digits) for _ in range(6))


def code_hash(cycle_id: str, approval_code: str) -> str:
    return hashlib.sha256(f"{cycle_id}:{approval_code}".encode("utf-8")).hexdigest()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS cycles (
                cycle_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                ceo_message TEXT NOT NULL,
                target TEXT NOT NULL,
                priority TEXT NOT NULL,
                subject TEXT,
                draft_order TEXT,
                approval_code_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                approved_at TEXT,
                completed_at TEXT,
                ceo_resolution TEXT,
                staff_summary TEXT,
                mode TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL,
                response TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(cycle_id) REFERENCES cycles(cycle_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def db_execute(sql: str, params: tuple = ()) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(sql, params)
        con.commit()


def db_query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.execute(sql, params)
        return cur.fetchone()


def db_query_all(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.execute(sql, params)
        return list(cur.fetchall())


def add_event(cycle_id: Optional[str], event_type: str, payload: Dict[str, Any]) -> None:
    db_execute(
        "INSERT INTO events(cycle_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (cycle_id, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
    )


def get_agent_instructions(agent_id: str) -> str:
    common = read_text(AGENTS_DIR / "COMMON_RULES.md")
    agent = read_text(AGENTS_DIR / f"{agent_id}.md")
    if not agent:
        agent = f"Ты — {agent_id}. Ответь строго в рамках роли и безопасности проекта."
    return f"{common}\n\n---\n\n{agent}"


def openai_or_mock(agent_id: str, prompt: str, purpose: str) -> str:
    current_mode = mode()
    if current_mode != "api":
        return mock_agent_response(agent_id, prompt, purpose)

    if OpenAI is None:
        raise RuntimeError("OpenAI SDK is not installed. Run 00_INSTALL_REQUIREMENTS.bat")
    if not env("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is empty. Fill .env or use ARX_MODE=mock")

    client = OpenAI()
    instructions = get_agent_instructions(agent_id)
    response = client.responses.create(
        model=model_name(),
        instructions=instructions,
        input=prompt,
        max_output_tokens=max_output_tokens(),
    )
    text = getattr(response, "output_text", "")
    if not text:
        text = str(response)
    return text


def short_subject(text: str, fallback: str = "Распоряжение") -> str:
    line = re.sub(r"\s+", " ", text.strip()).strip()
    if len(line) > 80:
        line = line[:77] + "..."
    return line or fallback


def mock_agent_response(agent_id: str, prompt: str, purpose: str) -> str:
    if agent_id == "ARX-01" and purpose == "draft_order":
        cycle = re.search(r"CYCLE_ID:\s*(\S+)", prompt)
        cycle_id = cycle.group(1) if cycle else "CYCLE-MOCK"
        subject = short_subject(prompt, "Тестовое распоряжение")
        return f"""[[ARX:MSG]]
ID: {cycle_id}-ORDER-001
FROM: ARX-01
TO: TEAM
TAG: ORDER
PRIORITY: P1
REQUIRES_REPLY: yes
SUBJECT: {subject}
CEO_APPROVED: no
---
Команда, это проект распоряжения, подготовленный штабом в mock-режиме.

Задача: подтвердить получение распоряжения, указать состояние своего направления, главный риск, следующий безопасный шаг и статус green/yellow/red.

Ограничения: никаких удалений, переносов, переименований, cleanup, BAT, PowerShell, EXE и действий с реальными дисками.

Каждый участник отвечает HANDOFF-блоком для АРХ-01.
[[/ARX:MSG]]"""

    if agent_id == "ARX-01" and purpose == "staff_summary":
        return """[[ARX:MSG]]
FROM: ARX-01
TO: CEO
TAG: STAFF_SUMMARY
PRIORITY: P1
REQUIRES_REPLY: yes
SUBJECT: Сводка цикла mock
---
1. Executive summary: mock-цикл завершён, все рабочие агенты дали ответы.
2. Общий статус: green для контура связи; yellow для перехода к боевому API без проверки ключей и публичного HTTPS.
3. Главный риск: Action-шлюз должен быть защищён Bearer token и approval code.
4. Требует решения CEO: подтвердить переход ARX_MODE=api и публичный HTTPS endpoint.
5. Следующий шаг: настроить Custom GPT Action и выполнить короткий реальный цикл.
[[/ARX:MSG]]"""

    role = {
        "ARX-02": "DATAOPS | SOURCE REGISTRY",
        "ARX-03": "ENGINEERING | EVIDENCE PIPELINE",
        "ARX-04": "DATABASE | DIGITAL MEMORY DB",
        "ARX-05": "QA SECURITY | SAFETY CONTROL",
        "ARX-06": "HISTORIAN | LEGACY HANDOFF",
        "ARX-07": "PMO | TASKS RISKS REPORTING",
        "ARX-08": "SOFTWARE | PRODUCT ENGINEERING",
    }.get(agent_id, "WORKSTREAM")
    return f"""HANDOFF-БЛОК ДЛЯ АРХ-01 | ШТАБ
CHAT_ID:
{agent_id} | {role}

РАСПОРЯЖЕНИЕ:
Получено распоряжение в mock-режиме. Исполняю только безопасный аналитический контур.

SOURCE VISIBILITY CHECK:
В mock-режиме Project Sources не читаются. Используется текст распоряжения, переданный шлюзом.

ЧТО СДЕЛАНО:
Подтверждено получение. Сформирован тестовый handoff для проверки маршрутизации.

СОЗДАННЫЕ АРТЕФАКТЫ:
{agent_id}_MOCK_HANDOFF_v0.1 — текстовый ответ в базе шлюза.

ИЗМЕНЕНИЯ В РЕЕСТРАХ:
Физические реестры не изменялись.

РИСКИ:
До перехода в api-режим это только технологический тест, не содержательная работа проекта.

ТРЕБУЕТ РЕШЕНИЯ CEO:
Подтвердить подключение OpenAI API и публичного HTTPS для ChatGPT Action.

СЛЕДУЮЩИЙ ШАГ:
Перейти к короткому api-тесту после настройки .env.

СТАТУС: green"""


class CreateCycleRequest(BaseModel):
    ceo_message: str = Field(..., description="Полный текст задачи CEO для штаба.")
    target: str = Field("TEAM", description="TEAM или список адресатов, например ARX-02,ARX-04.")
    priority: str = Field("P1", description="P1/P2/P3")
    subject: Optional[str] = Field(None, description="Краткая тема цикла, если известна.")


class CreateCycleResponse(BaseModel):
    cycle_id: str
    status: str
    draft_order: str
    approval_code: str
    approval_phrase: str
    message_for_ceo: str


class ApproveCycleRequest(BaseModel):
    cycle_id: str
    approval_code: str = Field(..., description="Код одобрения, возвращённый create_cycle.")
    approval_text: str = Field(..., description="Полная фраза CEO, например: ОДОБРЯЮ C... ARX-123456")
    ceo_resolution: Optional[str] = Field(None, description="Дополнительная резолюция CEO.")


class RejectCycleRequest(BaseModel):
    cycle_id: str
    reason: str = Field(..., description="Причина отклонения проекта распоряжения.")


class ApproveCycleResponse(BaseModel):
    cycle_id: str
    status: str
    agent_response_count: int
    staff_summary: str
    message_for_ceo: str


class CycleStatusResponse(BaseModel):
    cycle_id: str
    status: str
    created_at: str
    approved_at: Optional[str]
    completed_at: Optional[str]
    target: str
    priority: str
    subject: Optional[str]
    draft_order: Optional[str]
    staff_summary: Optional[str]
    agent_runs: List[Dict[str, Any]]


@dataclass
class CycleRecord:
    cycle_id: str
    status: str
    ceo_message: str
    target: str
    priority: str
    subject: Optional[str]
    draft_order: Optional[str]
    approval_code_sha256: str
    created_at: str
    approved_at: Optional[str]
    completed_at: Optional[str]
    ceo_resolution: Optional[str]
    staff_summary: Optional[str]
    mode: str


def row_to_cycle(row: sqlite3.Row) -> CycleRecord:
    return CycleRecord(**{k: row[k] for k in row.keys()})


def resolve_targets(target: str) -> List[str]:
    members = CONFIG.get("team", {}).get("members", ["ARX-02", "ARX-03", "ARX-04", "ARX-05", "ARX-06", "ARX-07", "ARX-08"])
    target = (target or "TEAM").strip().upper()
    if target in {"TEAM", "ALL", "ВСЕМ", "КОМАНДА"}:
        return members
    parts = [p.strip().upper() for p in re.split(r"[,;\s]+", target) if p.strip()]
    return [p for p in parts if re.fullmatch(r"ARX-0[2-8]", p)] or members


def create_cycle_logic(req: CreateCycleRequest) -> CreateCycleResponse:
    init_db()
    cycle_id = make_cycle_id()
    approval_code = make_approval_code()
    subject = req.subject or short_subject(req.ceo_message, "Задача CEO")

    prompt = f"""CYCLE_ID: {cycle_id}
CEO_MESSAGE:
{req.ceo_message}

Подготовь проект распоряжения для адресатов: {req.target}.
Приоритет: {req.priority}.
Тема: {subject}.

Требования:
- строго формат [[ARX:MSG]] ... [[/ARX:MSG]];
- FROM: ARX-01;
- TO: {req.target};
- CEO_APPROVED: no;
- включить запреты проекта;
- потребовать handoff-ответы от рабочих агентов.
"""
    draft = openai_or_mock("ARX-01", prompt, "draft_order")

    db_execute(
        """INSERT INTO cycles(cycle_id, status, ceo_message, target, priority, subject, draft_order,
           approval_code_sha256, created_at, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            cycle_id,
            "pending_ceo_approval",
            req.ceo_message,
            req.target,
            req.priority,
            subject,
            draft,
            code_hash(cycle_id, approval_code),
            utc_now(),
            mode(),
        ),
    )
    add_event(cycle_id, "cycle_created", {"target": req.target, "priority": req.priority, "subject": subject})
    export_path = EXPORT_DIR / f"{local_stamp()}__{cycle_id}__DRAFT_ORDER.md"
    export_path.write_text(draft, encoding="utf-8")
    log_event(f"CYCLE CREATED {cycle_id}: pending approval; draft exported {export_path.name}")

    phrase = f"ОДОБРЯЮ {cycle_id} {approval_code}"
    return CreateCycleResponse(
        cycle_id=cycle_id,
        status="pending_ceo_approval",
        draft_order=draft,
        approval_code=approval_code,
        approval_phrase=phrase,
        message_for_ceo=(
            "Проект распоряжения подготовлен. Рассылка не выполнена. "
            f"Для запуска напишите точную фразу: {phrase}"
        ),
    )


def get_cycle_or_404(cycle_id: str) -> CycleRecord:
    row = db_query_one("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,))
    if not row:
        raise HTTPException(status_code=404, detail=f"Cycle not found: {cycle_id}")
    return row_to_cycle(row)


def validate_approval(cycle: CycleRecord, approval_code: str, approval_text: str) -> None:
    if cycle.status not in {"pending_ceo_approval", "rejected"}:
        raise HTTPException(status_code=409, detail=f"Cycle cannot be approved from status: {cycle.status}")
    if code_hash(cycle.cycle_id, approval_code) != cycle.approval_code_sha256:
        raise HTTPException(status_code=403, detail="Invalid approval code")
    upper = approval_text.upper()
    if "ОДОБР" not in upper and "APPROV" not in upper:
        raise HTTPException(status_code=403, detail="Approval text must contain explicit approval")
    if cycle.cycle_id not in approval_text or approval_code not in approval_text:
        raise HTTPException(status_code=403, detail="Approval text must include cycle_id and approval_code")


def run_member_agent(agent_id: str, cycle: CycleRecord) -> str:
    prompt = f"""Ты получил одобренное CEO распоряжение штаба.

CYCLE_ID: {cycle.cycle_id}
TARGET_AGENT: {agent_id}
CEO_ORIGINAL_TASK:
{cycle.ceo_message}

APPROVED_ORDER:
{cycle.draft_order}

Ответь только HANDOFF-БЛОКОМ ДЛЯ АРХ-01. Не выполняй опасных действий. Никаких BAT/PowerShell/EXE, удаления, переноса, переименования, cleanup и действий с реальными дисками.
"""
    return openai_or_mock(agent_id, prompt, "handoff")


def build_staff_summary(cycle: CycleRecord, responses: Dict[str, str]) -> str:
    joined = []
    for agent_id, text in responses.items():
        joined.append(f"\n===== {agent_id} HANDOFF =====\n{text}\n")
    prompt = f"""CYCLE_ID: {cycle.cycle_id}
CEO_ORIGINAL_TASK:
{cycle.ceo_message}

APPROVED_ORDER:
{cycle.draft_order}

TEAM_HANDOFFS:
{''.join(joined)}

Подготовь сводку для CEO: кратко, управленчески, с рисками, решениями CEO и следующим циклом. Формат [[ARX:MSG]] FROM ARX-01 TO CEO TAG STAFF_SUMMARY.
"""
    return openai_or_mock("ARX-01", prompt, "staff_summary")


def approve_cycle_logic(req: ApproveCycleRequest) -> ApproveCycleResponse:
    init_db()
    cycle = get_cycle_or_404(req.cycle_id)
    validate_approval(cycle, req.approval_code, req.approval_text)

    db_execute(
        "UPDATE cycles SET status = ?, approved_at = ?, ceo_resolution = ? WHERE cycle_id = ?",
        ("dispatching", utc_now(), req.ceo_resolution or req.approval_text, req.cycle_id),
    )
    add_event(req.cycle_id, "cycle_approved", {"approval_text": req.approval_text, "resolution": req.ceo_resolution})
    log_event(f"CYCLE APPROVED {req.cycle_id}: dispatching")

    # Reload after approval fields changed.
    cycle = get_cycle_or_404(req.cycle_id)
    targets = resolve_targets(cycle.target)
    responses: Dict[str, str] = {}
    errors: Dict[str, str] = {}

    def call(agent_id: str) -> tuple[str, str, Optional[str]]:
        try:
            text = run_member_agent(agent_id, cycle)
            return agent_id, text, None
        except Exception as exc:  # pragma: no cover
            return agent_id, "", str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers()) as executor:
        futures = [executor.submit(call, agent_id) for agent_id in targets]
        for fut in concurrent.futures.as_completed(futures):
            agent_id, text, err = fut.result()
            if err:
                errors[agent_id] = err
                db_execute(
                    "INSERT INTO agent_runs(cycle_id, agent_id, status, response, created_at) VALUES (?, ?, ?, ?, ?)",
                    (cycle.cycle_id, agent_id, "error", err, utc_now()),
                )
                log_event(f"AGENT ERROR {cycle.cycle_id} {agent_id}: {err}")
            else:
                responses[agent_id] = text
                db_execute(
                    "INSERT INTO agent_runs(cycle_id, agent_id, status, response, created_at) VALUES (?, ?, ?, ?, ?)",
                    (cycle.cycle_id, agent_id, "completed", text, utc_now()),
                )
                out = EXPORT_DIR / f"{local_stamp()}__{cycle.cycle_id}__{agent_id}__HANDOFF.md"
                out.write_text(text, encoding="utf-8")
                log_event(f"AGENT COMPLETED {cycle.cycle_id} {agent_id}: exported {out.name}")

    summary = build_staff_summary(cycle, responses)
    summary_path = EXPORT_DIR / f"{local_stamp()}__{cycle.cycle_id}__ARX-01__STAFF_SUMMARY.md"
    summary_path.write_text(summary, encoding="utf-8")

    final_status = "completed" if not errors else "completed_with_errors"
    db_execute(
        "UPDATE cycles SET status = ?, completed_at = ?, staff_summary = ? WHERE cycle_id = ?",
        (final_status, utc_now(), summary, cycle.cycle_id),
    )
    add_event(cycle.cycle_id, "cycle_completed", {"responses": list(responses.keys()), "errors": errors})
    log_event(f"CYCLE COMPLETED {cycle.cycle_id}: status={final_status}; responses={len(responses)}")

    return ApproveCycleResponse(
        cycle_id=cycle.cycle_id,
        status=final_status,
        agent_response_count=len(responses),
        staff_summary=summary,
        message_for_ceo=(
            f"Распоряжение одобрено и обработано внутренними API-агентами: {', '.join(sorted(responses.keys()))}. "
            f"Сводка АРХ-01 готова. Статус: {final_status}."
        ),
    )


def reject_cycle_logic(req: RejectCycleRequest) -> Dict[str, str]:
    cycle = get_cycle_or_404(req.cycle_id)
    if cycle.status != "pending_ceo_approval":
        raise HTTPException(status_code=409, detail=f"Cycle cannot be rejected from status: {cycle.status}")
    db_execute(
        "UPDATE cycles SET status = ?, ceo_resolution = ? WHERE cycle_id = ?",
        ("rejected", req.reason, req.cycle_id),
    )
    add_event(req.cycle_id, "cycle_rejected", {"reason": req.reason})
    log_event(f"CYCLE REJECTED {req.cycle_id}: {req.reason}")
    return {"cycle_id": req.cycle_id, "status": "rejected", "message": "Проект распоряжения отклонён CEO. Рассылка не выполнялась."}


def status_logic(cycle_id: str) -> CycleStatusResponse:
    cycle = get_cycle_or_404(cycle_id)
    rows = db_query_all("SELECT agent_id, status, response, created_at FROM agent_runs WHERE cycle_id = ? ORDER BY agent_id", (cycle_id,))
    runs = [dict(r) for r in rows]
    return CycleStatusResponse(
        cycle_id=cycle.cycle_id,
        status=cycle.status,
        created_at=cycle.created_at,
        approved_at=cycle.approved_at,
        completed_at=cycle.completed_at,
        target=cycle.target,
        priority=cycle.priority,
        subject=cycle.subject,
        draft_order=cycle.draft_order,
        staff_summary=cycle.staff_summary,
        agent_runs=runs,
    )


security = HTTPBearer(auto_error=False)


def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> None:
    token = env("ARX_ACTION_TOKEN", "change_me_long_random_token")
    if not token or token == "change_me_long_random_token":
        # In mock/local testing, allow if token not configured. In api mode, require a real token.
        if mode() == "mock":
            return
    if credentials is None or credentials.scheme.lower() != "bearer" or credentials.credentials != token:
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")


app = FastAPI(title=APP_TITLE, version=APP_VERSION)


@app.get("/health")
def health() -> Dict[str, Any]:
    init_db()
    return {"ok": True, "app": APP_TITLE, "version": APP_VERSION, "mode": mode(), "time": utc_now()}


@app.post("/api/v1/cycles/create", response_model=CreateCycleResponse, dependencies=[Depends(require_auth)])
def create_cycle(req: CreateCycleRequest) -> CreateCycleResponse:
    if len(req.ceo_message.strip()) < 5:
        raise HTTPException(status_code=400, detail="ceo_message is too short")
    return create_cycle_logic(req)


@app.post("/api/v1/cycles/approve", response_model=ApproveCycleResponse, dependencies=[Depends(require_auth)])
def approve_cycle(req: ApproveCycleRequest) -> ApproveCycleResponse:
    return approve_cycle_logic(req)


@app.post("/api/v1/cycles/reject", dependencies=[Depends(require_auth)])
def reject_cycle(req: RejectCycleRequest) -> Dict[str, str]:
    return reject_cycle_logic(req)


@app.get("/api/v1/cycles/{cycle_id}", response_model=CycleStatusResponse, dependencies=[Depends(require_auth)])
def get_cycle_status(cycle_id: str = ApiPath(..., description="Cycle ID")) -> CycleStatusResponse:
    return status_logic(cycle_id)


def run_self_test() -> int:
    os.environ["ARX_MODE"] = "mock"
    init_db()
    print("[ARX] Self-test mode: mock")
    create = create_cycle_logic(
        CreateCycleRequest(
            ceo_message="Проверить контур ChatGPT Action Gateway: проект распоряжения, approval gate, рассылка ARX-02...ARX-08, сводка ARX-01.",
            target="TEAM",
            priority="P1",
            subject="Self-test ARX Action Gateway",
        )
    )
    print("[ARX] Created:", create.cycle_id)
    print("[ARX] Approval phrase:", create.approval_phrase)
    approved = approve_cycle_logic(
        ApproveCycleRequest(
            cycle_id=create.cycle_id,
            approval_code=create.approval_code,
            approval_text=create.approval_phrase,
            ceo_resolution="Self-test approval",
        )
    )
    print("[ARX] Approved status:", approved.status)
    print("[ARX] Agent responses:", approved.agent_response_count)
    print("[ARX] Staff summary preview:")
    print(approved.staff_summary[:1000])
    print("\n[ARX] Self-test completed. Check data\\exports and data\\logs.")
    return 0


def run_server() -> int:
    import uvicorn

    host = env("ARX_HOST", "127.0.0.1") or "127.0.0.1"
    try:
        port = int(env("ARX_PORT", "8787"))
    except ValueError:
        port = 8787
    init_db()
    log_event(f"SERVER START host={host} port={port} mode={mode()}")
    uvicorn.run(app, host=host, port=port)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ARX ChatGPT Action Gateway")
    parser.add_argument("--serve", action="store_true", help="Start FastAPI gateway")
    parser.add_argument("--self-test", action="store_true", help="Run mock self-test")
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    if args.serve:
        return run_server()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
