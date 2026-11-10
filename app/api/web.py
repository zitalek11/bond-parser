from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select

from app.api.deps import PipelineDep, SessionDep
from app.api.forms import bond_document_from_form
from app.core.config import settings
from app.core.enums import TaskStatus
from app.core.labels import (
    APPROVAL_MODE_RU,
    COUPON_TYPE_RU,
    SEVERITY_RU,
    SOURCE_RU,
    STATUS_RU,
    ru_label,
)
from app.models import BondIssue, ParsingTask
from app.services.pipeline import dashboard_stats

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
templates.env.filters["tojson"] = lambda value, indent=None: json.dumps(
    value, ensure_ascii=False, indent=indent, default=str
)
templates.env.filters["status_ru"] = lambda v: ru_label(STATUS_RU, v)
templates.env.filters["source_ru"] = lambda v: ru_label(SOURCE_RU, v)
templates.env.filters["coupon_type_ru"] = lambda v: ru_label(COUPON_TYPE_RU, v)
templates.env.filters["approval_mode_ru"] = lambda v: ru_label(APPROVAL_MODE_RU, v)
templates.env.filters["severity_ru"] = lambda v: ru_label(SEVERITY_RU, v)


def _errors_by_field(task: ParsingTask) -> dict[str, list[str]]:
    by_field: dict[str, list[str]] = {}
    for issue in task.validation_errors or []:
        if issue.get("severity") == "warning":
            continue
        field = issue.get("field") or "_general"
        by_field.setdefault(field, []).append(issue.get("message") or issue.get("code") or "")
    return by_field


def _editable_payload(task: ParsingTask) -> dict[str, Any]:
    if task.reviewed_data:
        return task.reviewed_data
    return task.llm_output or {}


def _load_quality_report() -> dict[str, Any] | None:
    path = Path(__file__).resolve().parents[2] / "storage" / "eval50" / "report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: SessionDep) -> HTMLResponse:
    stats = await dashboard_stats(session)
    recent = (
        await session.scalars(
            select(ParsingTask).order_by(ParsingTask.created_at.desc()).limit(10)
        )
    ).all()
    bonds = (
        await session.scalars(select(BondIssue).order_by(BondIssue.updated_at.desc()).limit(5))
    ).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "recent": recent,
            "bonds": bonds,
            "quality": _load_quality_report(),
            "app_name": settings.app_name,
        },
    )


@router.get("/queue", response_class=HTMLResponse)
async def review_queue(request: Request, session: SessionDep) -> HTMLResponse:
    tasks = (
        await session.scalars(
            select(ParsingTask)
            .where(ParsingTask.status == TaskStatus.NEEDS_REVIEW)
            .order_by(ParsingTask.created_at.asc())
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "queue.html",
        {"tasks": tasks, "app_name": settings.app_name},
    )


@router.get("/review/{task_id}", response_class=HTMLResponse)
async def review_screen(request: Request, task_id: int, session: SessionDep) -> HTMLResponse:
    task = await session.get(ParsingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    payload = _editable_payload(task)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "task": task,
            "data": payload,
            "errors_by_field": _errors_by_field(task),
            "validation_errors": task.validation_errors or [],
            "coupons_json": json.dumps(payload.get("coupons") or [], ensure_ascii=False, indent=2),
            "amortization_json": json.dumps(
                payload.get("amortization") or [], ensure_ascii=False, indent=2
            ),
            "quotes_json": json.dumps(payload.get("quotes") or {}, ensure_ascii=False, indent=2),
            "app_name": settings.app_name,
        },
    )


@router.post("/review/{task_id}/approve")
async def approve_review(
    request: Request,
    task_id: int,
    session: SessionDep,
    pipeline: PipelineDep,
    reviewer: str = Form(default="operator"),
    comment: str | None = Form(default=None),
) -> RedirectResponse:
    task = await session.get(ParsingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.status not in {TaskStatus.NEEDS_REVIEW, TaskStatus.PARSED, TaskStatus.REJECTED}:
        raise HTTPException(
            status_code=400,
            detail=f"Нельзя подтвердить задачу в статусе «{ru_label(STATUS_RU, task.status)}»",
        )

    try:
        document = await bond_document_from_form(request)
    except (ValidationError, json.JSONDecodeError, ValueError) as exc:
        payload = _editable_payload(task)
        return templates.TemplateResponse(
            request,
            "review.html",
            {
                "task": task,
                "data": payload,
                "errors_by_field": {"_form": [str(exc)]},
                "validation_errors": task.validation_errors or [],
                "form_error": str(exc),
                "coupons_json": str((await request.form()).get("coupons_json") or "[]"),
                "amortization_json": str((await request.form()).get("amortization_json") or "[]"),
                "quotes_json": str((await request.form()).get("quotes_json") or "{}"),
                "app_name": settings.app_name,
            },
            status_code=400,
        )

    await pipeline.approve_task(
        session,
        task,
        reviewed_data=document.model_dump(mode="json"),
        reviewer=reviewer or "operator",
        comment=comment,
    )
    await session.commit()
    return RedirectResponse(url="/queue", status_code=303)


@router.post("/review/{task_id}/reject")
async def reject_review(
    task_id: int,
    session: SessionDep,
    pipeline: PipelineDep,
    reviewer: str = Form(default="operator"),
    comment: str | None = Form(default=None),
) -> RedirectResponse:
    task = await session.get(ParsingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    await pipeline.reject_task(
        session, task, reviewer=reviewer or "operator", comment=comment
    )
    await session.commit()
    return RedirectResponse(url="/queue", status_code=303)


@router.post("/upload")
async def upload_document(
    session: SessionDep,
    pipeline: PipelineDep,
    file: UploadFile = File(...),
) -> RedirectResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Пустое имя файла")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Пустой файл")

    try:
        task = await pipeline.ingest_upload(
            session, content=content, filename=file.filename, run_pipeline=True
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Ошибка конвейера: {exc}") from exc

    await session.commit()

    if task.status == TaskStatus.NEEDS_REVIEW:
        return RedirectResponse(url=f"/review/{task.id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@router.post("/collect")
async def run_collect_now(session: SessionDep, pipeline: PipelineDep) -> RedirectResponse:
    await pipeline.run_collection_cycle(session)
    await session.commit()
    return RedirectResponse(url="/", status_code=303)


@router.get("/files/{task_id}")
async def get_task_file(task_id: int, session: SessionDep) -> FileResponse:
    task = await session.get(ParsingTask, task_id)
    if not task or not task.file_path:
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = Path(task.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл отсутствует на диске")
    media = task.mime_type or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media,
        filename=task.original_filename or path.name,
        content_disposition_type="inline",
    )
