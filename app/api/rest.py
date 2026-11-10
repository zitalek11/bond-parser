from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import PipelineDep, SessionDep
from app.core.enums import TaskStatus
from app.models import BondIssue, ParsingTask
from app.schemas.bond_document import BondDocumentSchema
from app.services.exporter import BondExporter
from app.services.pipeline import dashboard_stats

router = APIRouter(prefix="/api", tags=["api"])


class TaskOut(BaseModel):
    id: int
    status: str
    source: str
    title: str | None = None
    original_filename: str | None = None
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    llm_output: dict[str, Any] | None = None
    moex_reference: dict[str, Any] | None = None

    model_config = {"from_attributes": True}

    @field_validator("status", "source", mode="before")
    @classmethod
    def _enum_str(cls, value: Any) -> Any:
        return getattr(value, "value", value)


class BondOut(BaseModel):
    id: int
    isin: str | None
    reg_number: str
    issuer_name: str
    issuer_inn: str
    nominal_value: float
    currency: str
    coupon_type: str
    maturity_date: date | None = None
    approval_mode: str

    model_config = {"from_attributes": True}

    @field_validator("nominal_value", mode="before")
    @classmethod
    def _num(cls, value: Any) -> Any:
        return float(value) if value is not None else value

    @field_validator("coupon_type", "approval_mode", mode="before")
    @classmethod
    def _enum_str(cls, value: Any) -> Any:
        return getattr(value, "value", value)


class ApproveIn(BaseModel):
    data: BondDocumentSchema
    reviewer: str = "operator"
    comment: str | None = None


class RejectIn(BaseModel):
    reviewer: str = "operator"
    comment: str | None = None


class CollectOut(BaseModel):
    enqueued: int
    processed: int
    failed: int


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/stats")
async def stats(session: SessionDep) -> dict[str, Any]:
    return await dashboard_stats(session)


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    session: SessionDep,
    status: TaskStatus | None = None,
    limit: int = 50,
) -> list[ParsingTask]:
    stmt = select(ParsingTask).order_by(ParsingTask.created_at.desc()).limit(min(limit, 200))
    if status:
        stmt = stmt.where(ParsingTask.status == status)
    return list(await session.scalars(stmt))


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: int, session: SessionDep) -> ParsingTask:
    task = await session.get(ParsingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/upload", response_model=TaskOut)
async def upload_task(
    session: SessionDep,
    pipeline: PipelineDep,
    file: UploadFile = File(...),
) -> ParsingTask:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Empty filename")
    content = await file.read()
    try:
        task = await pipeline.ingest_upload(
            session, content=content, filename=file.filename, run_pipeline=True
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(task)
    return task


@router.post("/collect", response_model=CollectOut)
async def collect_now(session: SessionDep, pipeline: PipelineDep) -> CollectOut:
    result = await pipeline.run_collection_cycle(session)
    await session.commit()
    return CollectOut(**result)


@router.post("/tasks/{task_id}/approve", response_model=BondOut)
async def approve_task(
    task_id: int,
    body: ApproveIn,
    session: SessionDep,
    pipeline: PipelineDep,
) -> BondIssue:
    task = await session.get(ParsingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    bond = await pipeline.approve_task(
        session,
        task,
        reviewed_data=body.data.model_dump(mode="json"),
        reviewer=body.reviewer,
        comment=body.comment,
    )
    await session.commit()
    await session.refresh(bond)
    return bond


@router.post("/tasks/{task_id}/reject", response_model=TaskOut)
async def reject_task(
    task_id: int,
    body: RejectIn,
    session: SessionDep,
    pipeline: PipelineDep,
) -> ParsingTask:
    task = await session.get(ParsingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await pipeline.reject_task(
        session, task, reviewer=body.reviewer, comment=body.comment
    )
    await session.commit()
    await session.refresh(task)
    return task


@router.get("/bonds", response_model=list[BondOut])
async def list_bonds(session: SessionDep, limit: int = 50) -> list[BondIssue]:
    stmt = select(BondIssue).order_by(BondIssue.updated_at.desc()).limit(min(limit, 200))
    return list(await session.scalars(stmt))


def _parse_optional_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


@router.get("/export/xlsx")
async def export_xlsx(
    session: SessionDep,
    date_from: str | None = Query(default=None, description="YYYY-MM-DD по approved_at"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD по approved_at"),
    only_new: bool = Query(default=False, description="Только ещё не экспортированные"),
    mark_exported: bool = Query(default=True, description="Проставить exported_at"),
) -> Response:
    exporter = BondExporter()
    bonds = await exporter.fetch_bonds(
        session,
        date_from=_parse_optional_date(date_from),
        date_to=_parse_optional_date(date_to),
        only_new=only_new,
    )
    payload = exporter.to_xlsx(bonds)
    if mark_exported and bonds:
        await exporter.mark_exported(session, bonds)
        await session.commit()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "new" if only_new else "all"
    filename = f"bond_issues_{suffix}_{stamp}.xlsx"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/csv")
async def export_csv(
    session: SessionDep,
    date_from: str | None = Query(default=None, description="YYYY-MM-DD по approved_at"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD по approved_at"),
    only_new: bool = Query(default=False, description="Только ещё не экспортированные"),
    mark_exported: bool = Query(default=False, description="Проставить exported_at"),
) -> Response:
    exporter = BondExporter()
    bonds = await exporter.fetch_bonds(
        session,
        date_from=_parse_optional_date(date_from),
        date_to=_parse_optional_date(date_to),
        only_new=only_new,
    )
    payload = exporter.to_csv(bonds)
    if mark_exported and bonds:
        await exporter.mark_exported(session, bonds)
        await session.commit()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bond_registry_{stamp}.csv"
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
