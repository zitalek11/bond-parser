from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import TaskSource, TaskStatus
from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.bond import BondIssue


class ParsingTask(TimestampMixin, Base):
    """Staging + audit record for one source document passing through the pipeline.

    Production data lives in `bond_issues`; a task only reaches it after the
    Data Quality Gate approves it automatically or an operator confirms it.
    """

    __tablename__ = "parsing_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- Source ---
    source: Mapped[TaskSource] = mapped_column(
        Enum(TaskSource, native_enum=False, length=32), nullable=False
    )
    source_guid: Mapped[str | None] = mapped_column(String(512), unique=True)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(1024))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Stored file ---
    original_filename: Mapped[str | None] = mapped_column(String(512))
    file_path: Mapped[str | None] = mapped_column(String(1024))
    file_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    mime_type: Mapped[str | None] = mapped_column(String(128))

    # --- Pipeline state ---
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, native_enum=False, length=32),
        default=TaskStatus.PENDING,
        nullable=False,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

    # --- Extraction ---
    extractor_backend: Mapped[str | None] = mapped_column(String(32))
    extracted_text: Mapped[str | None] = mapped_column(Text)

    # --- LLM ---
    llm_model: Mapped[str | None] = mapped_column(String(128))
    llm_output: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # --- Data Quality Gate ---
    # [{"field": "issuer_inn", "code": "INN_CHECKSUM", "message": "...", "severity": "error"}]
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    moex_reference: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # --- HITL review ---
    reviewed_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)

    bond_issue: Mapped[BondIssue | None] = relationship(
        back_populates="task", uselist=False, lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<ParsingTask id={self.id} status={self.status} source={self.source}>"
