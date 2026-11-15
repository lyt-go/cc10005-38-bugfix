"""代理池管理接口：维护代理实例，覆盖提交存活检测、标记可用、回收实例等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.proxypool import FILTERABLE_FIELDS, ProxypoolService

router = APIRouter(prefix="/api/proxypool", tags=["代理池管理"])

service = ProxypoolService()

LIST_FIELDS = ["实例编号", "代理地址", "代理端口", "匿名等级", "出站地域", "响应毫秒", "存活检测时间", "分配权重"]
STATUSES = ["草稿", "存活检测中", "可用", "已回收"]


def _field_filters(request: Request) -> dict[str, str]:
    """从查询串里提取字段级筛选，列表与导出共用同一口径，保证两处结果一致。"""
    return {
        field: str(request.query_params[field]).strip()
        for field in FILTERABLE_FIELDS
        if request.query_params.get(field)
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按实例编号检索"),
    status: str | None = Query(default=None, description="草稿、存活检测中、可用、已回收"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按实例编号与状态过滤代理池管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total, stats = service.list_entries(
        keyword=keyword, status=status, field_filters=_field_filters(request), page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size, stats=stats)


# 注意：/export 必须注册在 /{entry_id} 之前，否则 "export" 会被当成 entry_id 解析，
# 导出标签页直接 422 —— 列表页有数据、标签页报错，两处结论对不上。
@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """导出代理池管理清单：与列表页走同一查询链路和同一套筛选条件。"""
    items, total, _ = service.list_entries(
        keyword=keyword, status=status, field_filters=_field_filters(request), page=1, size=10000
    )
    return {"module": "proxypool", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条代理实例明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"代理实例 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条代理实例，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="代理实例已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条代理实例执行提交存活检测、标记可用、回收实例；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
