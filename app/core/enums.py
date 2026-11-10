from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """Lifecycle of a document in the pipeline."""

    PENDING = "PENDING"            # discovered, file not yet downloaded
    DOWNLOADED = "DOWNLOADED"      # file stored locally
    EXTRACTED = "EXTRACTED"        # text/markdown extracted
    PARSED = "PARSED"              # LLM produced structured data
    NEEDS_REVIEW = "NEEDS_REVIEW"  # Data Quality Gate failed -> HITL queue
    APPROVED = "APPROVED"          # written to production tables
    REJECTED = "REJECTED"          # operator rejected the document
    FAILED = "FAILED"              # technical error (download/extract/LLM)


class TaskSource(StrEnum):
    RSS = "RSS"
    MANUAL_UPLOAD = "MANUAL_UPLOAD"
    MOCK = "MOCK"


class CouponType(StrEnum):
    FIXED = "FIXED"
    FLOATER = "FLOATER"
    ZERO = "ZERO"
    OTHER = "OTHER"


class ApprovalMode(StrEnum):
    AUTO = "AUTO"        # passed Data Quality Gate
    MANUAL = "MANUAL"    # confirmed by operator in HITL UI
