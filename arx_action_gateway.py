from __future__ import annotations

import os
import random
import string
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

APP_TITLE = "ARX ChatGPT Action Gateway"
APP_VERSION = "3.0.0"

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

security = HTTPBearer(auto_error=False)
CYCLES: Dict[str, Dict[str, Any]] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def mode() -> str:
    return os.getenv("ARX_MODE", "mock").strip().lower() or "mock"


def model_name() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5"


def make_cycle_id() -> str:
    suffix = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"C{datetime.now().strftime('%Y%m%d_%H%M%S')}_{suffix}"


def make_approval_code() -> str:
    return "ARX-" + "".join(random.choice(string.digits) for _ in range(6))


def health_payload() -> Dict[str, Any]:
    return {
        "ok": True,
        "app": APP_TITLE,
        "version": APP_VERSION,
        "mode": mode(),
        "time": utc_now(),
    }


def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> None:
    token = os.getenv("ARX_ACTION_TOKEN", "").strip()
    if not token or mode() == "mock":
        return
    if credentials is None or credentials.scheme.lower() != "bearer" or credentials.credentials != token:
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")


def call_llm(agent_id: str, prompt: str) -> str:
    if mode() != "api":
        return mock_response(agent_id, prompt)
    if OpenAI is None:
        raise HTTPException(status_code=500, detail="OpenAI SDK is not installed")
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not configured")
    client = OpenAI()
    response = client.responses.create(
        model=model_name(),
        instructions=f"You are {agent_id} in the ARX digital archaeology project. Be concise and safe.",
        input=prompt,
        max_output_tokens=int(os.getenv("ARX_MAX_OUTPUT_TOKENS", "1800")),
    )
    return getattr(response, "output_text", str(response))


def mock_response(agent_id: str, prompt: str) -> str:
    if agent_id == "ARX-01-DRAFT":
        cycle_id = prompt.split("CYCLE_ID:")[-1].splitlines()[0].strip() if "CYCLE_ID:" in prompt else "CYCLE"
        return f"""[[ARX:MSG]]
ID: {cycle_id}-ORDER-001
FROM: ARX-01
TO: TEAM
TAG: ORDER
PRIORITY: P1
REQUIRES_REPLY: yes
SUBJECT: Test Action Gateway
CEO_APPROVED: no
---
Команда, это проект распоряжения для безопасного теста Action Gateway.
Никаких действий с файлами, дисками, BAT, PowerShell, EXE, cleanup, удаления, переноса или переименования.
Каждый агент должен дать краткий HANDOFF: роль, статус, риск, следующий безопасный шаг.
[[/ARX:MSG]]"""
    if agent_id == "ARX-01-SUMMARY":
        return """[[ARX:MSG]]
FROM: ARX-01
TO: CEO
TAG: STAFF_SUMMARY
PRIORITY: P1
REQUIRES_REPLY: yes
SUBJECT: Сводка тестового цикла
---
Тестовый цикл Action Gateway выполнен в mock-режиме. Все семь агентов вернули handoff. Контур связи работает. Следующий шаг: перейти к api-режиму после решения CEO.
[[/ARX:MSG]]"""
    return f"""HANDOFF-БЛОК ДЛЯ АРХ-01 | ШТАБ
CHAT_ID:
{agent_id}

ЧТО СДЕЛАНО:
Получено тестовое распоряжение. Подтверждаю работу канала.

РИСКИ:
До api-режима это технический тест, а не содержательная работа проекта.

СЛЕДУЮЩИЙ ШАГ:
После approval проверить полный цикл в безопасном режиме.

СТАТУС: green"""


class CreateCycleRequest(BaseModel):
    ceo_message: str = Field(..., description="Полный текст задачи CEO для штаба.")
    target: str = Field("TEAM", description="TEAM или список адресатов.")
    priority: str = Field("P1", description="P1/P2/P3")
    subject: Optional[str] = Field(None, description="Краткая тема цикла.")


class CreateCycleResponse(BaseModel):
    cycle_id: str
    status: str
    draft_order: str
    approval_code: str
    approval_phrase: str
    message_for_ceo: str


class ApproveCycleRequest(BaseModel):
    cycle_id: str
    approval_code: str
    approval_text: str
    ceo_resolution: Optional[str] = None


class ApproveCycleResponse(BaseModel):
    cycle_id: str
    status: str
    agent_response_count: int
    staff_summary: str
    message_for_ceo: str


class RejectCycleRequest(BaseModel):
    cycle_id: str
    reason: str


class CycleStatusResponse(BaseModel):
    cycle_id: str
    status: str
    created_at: str
    approved_at: Optional[str] = None
    completed_at: Optional[str] = None
    target: str
    priority: str
    subject: Optional[str] = None
    draft_order: Optional[str] = None
    staff_summary: Optional[str] = None
    agent_runs: List[Dict[str, Any]] = []


@app.api_route("/", methods=["GET", "HEAD"])
def root() -> Dict[str, Any]:
    return health_payload()


@app.get("/health")
def health() -> Dict[str, Any]:
    return health_payload()


@app.post("/api/v1/cycles/create", response_model=CreateCycleResponse, dependencies=[Depends(require_auth)])
def create_cycle(req: CreateCycleRequest) -> CreateCycleResponse:
    if len(req.ceo_message.strip()) < 5:
        raise HTTPException(status_code=400, detail="ceo_message is too short")
    cycle_id = make_cycle_id()
    approval_code = make_approval_code()
    draft = call_llm("ARX-01-DRAFT", f"CYCLE_ID: {cycle_id}\nCEO_MESSAGE:\n{req.ceo_message}")
    phrase = f"ОДОБРЯЮ {cycle_id} {approval_code}"
    CYCLES[cycle_id] = {
        "cycle_id": cycle_id,
        "status": "pending_ceo_approval",
        "created_at": utc_now(),
        "approved_at": None,
        "completed_at": None,
        "target": req.target,
        "priority": req.priority,
        "subject": req.subject,
        "ceo_message": req.ceo_message,
        "draft_order": draft,
        "approval_code": approval_code,
        "staff_summary": None,
        "agent_runs": [],
    }
    return CreateCycleResponse(
        cycle_id=cycle_id,
        status="pending_ceo_approval",
        draft_order=draft,
        approval_code=approval_code,
        approval_phrase=phrase,
        message_for_ceo=f"Проект распоряжения создан. Для запуска напишите: {phrase}",
    )


@app.post("/api/v1/cycles/approve", response_model=ApproveCycleResponse, dependencies=[Depends(require_auth)])
def approve_cycle(req: ApproveCycleRequest) -> ApproveCycleResponse:
    cycle = CYCLES.get(req.cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    if req.approval_code != cycle["approval_code"]:
        raise HTTPException(status_code=403, detail="Invalid approval code")
    if req.cycle_id not in req.approval_text or req.approval_code not in req.approval_text:
        raise HTTPException(status_code=403, detail="Approval text must include cycle_id and approval_code")
    agents = ["ARX-02", "ARX-03", "ARX-04", "ARX-05", "ARX-06", "ARX-07", "ARX-08"]
    runs = []
    for agent in agents:
        text = call_llm(agent, cycle["draft_order"])
        runs.append({"agent_id": agent, "status": "completed", "response": text})
    summary = call_llm("ARX-01-SUMMARY", "\n\n".join(r["response"] for r in runs))
    cycle["status"] = "completed"
    cycle["approved_at"] = utc_now()
    cycle["completed_at"] = utc_now()
    cycle["staff_summary"] = summary
    cycle["agent_runs"] = runs
    return ApproveCycleResponse(
        cycle_id=req.cycle_id,
        status="completed",
        agent_response_count=len(runs),
        staff_summary=summary,
        message_for_ceo="Цикл одобрен, агенты обработаны, сводка готова.",
    )


@app.post("/api/v1/cycles/reject", dependencies=[Depends(require_auth)])
def reject_cycle(req: RejectCycleRequest) -> Dict[str, str]:
    cycle = CYCLES.get(req.cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    cycle["status"] = "rejected"
    return {"cycle_id": req.cycle_id, "status": "rejected", "message": req.reason}


@app.get("/api/v1/cycles/{cycle_id}", response_model=CycleStatusResponse, dependencies=[Depends(require_auth)])
def get_cycle_status(cycle_id: str) -> CycleStatusResponse:
    cycle = CYCLES.get(cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    return CycleStatusResponse(**cycle)
