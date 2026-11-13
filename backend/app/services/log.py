"""采集日志业务规则：状态流转、字段校验与筛选口径都收在这里。

重试续跑约定：
- 每条执行日志按批次明细记录采集进度，断点 = 最早的失败/未执行批次号；
- 触发重试时从断点继续采集，断点之前的成功批次不再重复采集；
- 每次重试生成一条新的执行日志（新日志编号、新时间点），通过
  来源日志编号/原始日志编号 关联旧记录，不与原始日志混成一条；
- 原日志的开始时间、结束时间、采集行数、成功条数、失败条数、错误信息
  保持当时的值，续跑不回写这些历史字段；
- 并发重复提交只认第一次：同一日志在续跑进行中或已有成功续跑记录时，
  后续提交直接拒绝；
- 续跑成功后回写关联采集任务的待办标记，看板汇总（/api/overview）
  随 pending 实时刷新。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import store

MODULE = "log"
TASK_MODULE = "task"
REQUIRED_FIELDS = ["日志编号", "关联任务", "开始时间"]
OPTIONAL_FIELDS = ["结束时间", "采集行数", "成功条数", "失败条数", "错误信息"]
STATUS_ORDER = ["成功", "部分失败", "完全失败"]
RETRY_ACTION = "触发重试"
ACTION_RULES = {"标记已读": "成功", "归档日志": "完全失败"}
NEGATIVE_ACTIONS: list[str] = []

BATCH_DONE = "成功"
BATCH_FAILED = "失败"
BATCH_PENDING = "未执行"
# 断点只认最早的失败段；失败段之后（含未执行批次）都属于待续跑范围
BATCH_TODO = {BATCH_FAILED, BATCH_PENDING}

# 续跑进行中的日志 id：并发重复提交只认第一次
_IN_FLIGHT: set[int] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def _to_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _normalize_batches(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """取出日志的批次明细；旧数据没有批次明细时按整单一个批次兜底，保证兼容。"""
    raw = entry.get("批次明细")
    batches: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            batch_no = _to_int(item.get("批次号"))
            if batch_no <= 0:
                continue
            batches.append({
                "批次号": batch_no,
                "状态": str(item.get("状态") or BATCH_FAILED),
                "采集行数": _to_int(item.get("采集行数")),
                "成功条数": _to_int(item.get("成功条数")),
                "失败条数": _to_int(item.get("失败条数")),
                "错误信息": str(item.get("错误信息") or ""),
            })
    if batches:
        return sorted(batches, key=lambda batch: batch["批次号"])
    total = _to_int(entry.get("采集行数"))
    failed = _to_int(entry.get("失败条数"))
    done = entry.get("status") == STATUS_ORDER[0]
    return [{
        "批次号": 1,
        "状态": BATCH_DONE if done else BATCH_FAILED,
        "采集行数": total,
        "成功条数": total if done else max(total - failed, 0),
        "失败条数": 0 if done else failed,
        "错误信息": str(entry.get("错误信息") or ""),
    }]


def _next_log_number(rows: list[dict[str, Any]]) -> str:
    """在现有日志编号序列上取下一个号；非 LOG-xxxx 编号不影响取号。"""
    seq = 0
    for row in rows:
        match = re.fullmatch(r"LOG-(\d+)", str(row.get("日志编号") or ""))
        if match:
            seq = max(seq, int(match.group(1)))
    return f"LOG-{seq + 1:04d}"


class LogService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("日志编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"执行日志 {entry_id} 不存在或已归档"
        if action == RETRY_ACTION:
            return self.retry_entry(entry_id)
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集日志可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"执行日志已{action}"

    def retry_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """从断点续跑单条执行日志；并发重复提交只认第一次。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"执行日志 {entry_id} 不存在或已归档"
        number = str(entry.get("日志编号") or f"ID-{entry_id}")
        with _IN_FLIGHT_LOCK:
            if entry_id in _IN_FLIGHT:
                return None, f"执行日志 {number} 正在续跑，并发重复提交只认第一次"
            _IN_FLIGHT.add(entry_id)
        try:
            return self._resume_from_breakpoint(entry, number)
        finally:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.discard(entry_id)

    def retry_many(self, entry_ids: list[Any]) -> list[dict[str, Any]]:
        """批量重试：逐条从断点续跑、逐行返回结果，单条失败不影响其余。"""
        results: list[dict[str, Any]] = []
        for entry_id in entry_ids:
            try:
                eid = int(entry_id)
            except (TypeError, ValueError):
                results.append({
                    "id": entry_id,
                    "日志编号": "",
                    "ok": False,
                    "message": f"日志 id「{entry_id}」不是有效编号",
                    "entry": None,
                })
                continue
            existing = store.find(MODULE, eid)
            number = str(existing.get("日志编号") or "") if existing else ""
            entry, message = self.retry_entry(eid)
            results.append({
                "id": eid,
                "日志编号": number,
                "ok": entry is not None,
                "message": message,
                "entry": entry,
            })
        return results

    def _resume_from_breakpoint(
        self, entry: dict[str, Any], number: str
    ) -> tuple[dict[str, Any] | None, str]:
        rows = store.rows(MODULE)
        # 已成功批次不能重复采集：已有续跑记录时只允许沿着最新一条继续
        children = [row for row in rows if str(row.get("来源日志编号") or "") == number]
        done = [row for row in children if row.get("status") == STATUS_ORDER[0]]
        if done:
            done_no = str(done[0].get("日志编号") or "")
            return None, f"执行日志 {number} 的失败批次已由 {done_no} 续跑完成，已成功批次不能重复采集"
        if children:
            latest = max(children, key=lambda row: int(row.get("id", 0)))
            return None, f"执行日志 {number} 已生成续跑记录 {latest.get('日志编号')}，请对最新续跑记录发起重试"

        batches = _normalize_batches(entry)
        todo = [batch for batch in batches if batch["状态"] in BATCH_TODO]
        if not todo:
            return None, f"执行日志 {number} 没有失败批次，无需重试"
        # 同一批次只从最早的失败段继续，断点之前的成功批次不重采
        breakpoint_no = min(batch["批次号"] for batch in todo)
        resumed = [batch for batch in batches if batch["批次号"] >= breakpoint_no]

        started = datetime.now()
        finished = started + timedelta(seconds=len(resumed))
        new_batches = [{
            "批次号": batch["批次号"],  # 续跑批次保持原批次号
            "状态": BATCH_DONE,
            "采集行数": batch["采集行数"],
            "成功条数": batch["采集行数"],
            "失败条数": 0,
            "错误信息": "",
        } for batch in resumed]
        collected = sum(batch["采集行数"] for batch in new_batches)
        attempt = _to_int(entry.get("重试次序")) + 1
        new_entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "日志编号": _next_log_number(rows),
            "关联任务": entry.get("关联任务"),
            "开始时间": started.strftime("%Y-%m-%d %H:%M:%S"),
            "结束时间": finished.strftime("%Y-%m-%d %H:%M:%S"),
            "采集行数": collected,
            "成功条数": collected,
            "失败条数": 0,
            "错误信息": "",
            "status": STATUS_ORDER[0],
            "pending": False,
            "abnormal": False,
            "批次明细": new_batches,
            "断点批次": breakpoint_no,
            "来源日志编号": number,
            "原始日志编号": str(entry.get("原始日志编号") or number),
            "重试次序": attempt,
        }
        rows.append(new_entry)
        # 原日志只刷新待办，历史字段（采集行数、失败条数、错误信息等）保持当时的值
        entry["pending"] = False
        self._write_back_task(entry, new_entry)
        return new_entry, (
            f"已从断点第 {breakpoint_no} 批续跑，生成重试日志 {new_entry['日志编号']}"
            f"（来源 {number}），采集 {collected} 行，失败 0 条"
        )

    def _write_back_task(self, source_entry: dict[str, Any], new_entry: dict[str, Any]) -> None:
        """任务回写：续跑完成后刷新关联采集任务的待办，看板汇总随 pending 实时变化。"""
        task_key = str(source_entry.get("关联任务") or "").strip()
        if not task_key:
            return
        for task in store.rows(TASK_MODULE):
            keys = {str(task.get("任务编号") or ""), str(task.get("任务名称") or "")}
            if task_key in keys:
                task["pending"] = False
                task["abnormal"] = False
                task["最近执行日志编号"] = new_entry["日志编号"]
