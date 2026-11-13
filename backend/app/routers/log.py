"""采集日志接口：维护执行日志，覆盖标记已读、触发重试、归档日志等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.log import LogService

router = APIRouter(prefix="/api/log", tags=["采集日志"])

service = LogService()

LIST_FIELDS = ["日志编号", "关联任务", "开始时间", "结束时间", "采集行数", "成功条数", "失败条数", "错误信息", "断点批次", "来源日志编号"]
STATUSES = ["成功", "部分失败", "完全失败"]


class BatchRetryPayload(BaseModel):
    """批量重试入参：按日志 id 逐条续跑。"""

    ids: list[int] = []


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按日志编号检索"),
    status: str | None = Query(default=None, description="成功、部分失败、完全失败"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按日志编号与状态过滤采集日志列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条执行日志，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="执行日志已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集日志清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "log", "total": total, "items": items}


@router.post("/retry-batch")
def retry_batch(payload: BatchRetryPayload) -> dict[str, Any]:
    """批量重试：逐条从断点续跑并逐行返回结果，单条失败不影响其余。"""
    if not payload.ids:
        return {"ok": False, "message": "请先选择要重试的执行日志", "results": []}
    results = service.retry_many(payload.ids)
    done = sum(1 for item in results if item["ok"])
    return {
        "ok": done == len(results),
        "message": f"批量重试完成：成功 {done} 条，跳过或失败 {len(results) - done} 条",
        "results": results,
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条执行日志明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"执行日志 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条执行日志执行标记已读、触发重试、归档日志；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
