from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.services.pipeline import BondPipeline


@lru_cache
def get_pipeline() -> BondPipeline:
    return BondPipeline()


def pipeline_from_app(request: Request) -> BondPipeline:
    pipeline = getattr(request.app.state, "pipeline", None)
    return pipeline or get_pipeline()


SessionDep = Annotated[AsyncSession, Depends(get_session)]
PipelineDep = Annotated[BondPipeline, Depends(pipeline_from_app)]
