"""图表指标目录 API — 只读元数据 (见 docs/adr/ADR-006-chart-indicator-registry.md)。"""
from __future__ import annotations

from fastapi import APIRouter

from app.indicators.registry import catalog

router = APIRouter(prefix="/api/indicators", tags=["indicators"])


@router.get("/catalog")
def get_catalog() -> dict:
    """可渲染图表指标目录 (内置 + 可选 TA); 每项含 available 供前端门控。"""
    return {"indicators": catalog()}
