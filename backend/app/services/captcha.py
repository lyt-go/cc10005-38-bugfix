"""验证码处理业务规则：状态流转、字段校验与筛选口径都收在这里。

策略生命周期单向推进：草稿 → 策略验证中 → 已启用 → 已降级。
准入规则：
- 提交策略验证必须先分到一个新的存活代理作为识别出口，分配、状态、
  识别记录在同一把锁内原子落库；没有存活代理时整体拒绝、不产生副作用；
- 已在验证中且分配有效的策略重复提交会被拦下，不会产生多次状态变化；
- 已降级可以重新验证，但只能从新的存活代理开始（已回收的代理版本
  永远不会再被分配）；识别出口被回收撤销的策略可重新提交以恢复验证。
"""
from __future__ import annotations

from typing import Any

from app import statelib
from app.services import proxyalloc
from app.store import store

MODULE = "captcha"
REQUIRED_FIELDS = ["策略编号", "验证码类型", "识别服务商"]
STATUS_ORDER = statelib.CAPTCHA_STATUS_ORDER
ACTION_RULES = {"提交策略验证": "策略验证中", "启用策略": "已启用", "降级策略": "已降级"}

FILTERABLE_FIELDS = ["策略编号", "验证码类型", "识别服务商"]


class CaptchaService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        field_filters: dict[str, str] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        rows = store.rows(MODULE)
        stats = {
            "验证码策略总数": len(rows),
            "已启用策略": sum(1 for row in rows if row.get("status") == "已启用"),
            "验证中策略": sum(1 for row in rows if row.get("status") == "策略验证中"),
        }
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        for field, value in (field_filters or {}).items():
            if field in FILTERABLE_FIELDS and value:
                rows = [row for row in rows if value.lower() in str(row.get(field, "")).lower()]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total, stats

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"], entry["abnormal"] = statelib.captcha_flags(
                entry["status"], has_active_allocation=False
            )
            entry["识别记录"] = []
            entry["当前分配代理"] = None
            entry["当前分配代理版本"] = None
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        # 锁内完成 查状态 + 校验 + 分配代理 + 状态流转 + 识别记录，保证原子性：
        # 两个策略并发争用最后一个存活代理时，只会有一个在锁内分配成功。
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"验证码策略 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于验证码处理可执行范围"
            current = str(entry.get("status", ""))
            if action == "提交策略验证":
                return self._submit_verification(entry, current)
            if action == "启用策略":
                return self._enable(entry, current)
            return self._degrade(entry, current)

    # ------------------------------------------------------------------
    # 动作实现（均在 store.lock 内被调用）
    # ------------------------------------------------------------------
    def _submit_verification(
        self, entry: dict[str, Any], current: str
    ) -> tuple[dict[str, Any] | None, str]:
        if current == "已启用":
            return None, f"验证码策略 {entry.get('策略编号')} 已启用，如需变更请先降级再重新验证"
        if current == "策略验证中" and proxyalloc.has_active_allocation(entry):
            return None, (
                f"验证码策略 {entry.get('策略编号')} 已在策略验证中，"
                f"识别出口 {entry.get('当前分配代理')} 仍然有效，重复提交不会产生新的状态变化"
            )
        # 草稿首次验证 / 已降级重新验证 / 验证中但出口被回收撤销后的恢复：
        # 都必须建立在新的存活代理结果上
        proxy = proxyalloc.allocate_exit_proxy(entry)
        if proxy is None:
            return None, (
                f"验证码策略 {entry.get('策略编号')} 提交失败：暂无可用存活代理作为识别出口，"
                "请先在代理池标记可用实例"
            )
        entry["status"] = "策略验证中"
        entry["pending"], entry["abnormal"] = statelib.captcha_flags(
            "策略验证中", has_active_allocation=True
        )
        prefix = "重新验证" if current == "已降级" else "已提交策略验证"
        return entry, (
            f"验证码策略{prefix}，已分配新的存活代理 {proxy.get('实例编号')}"
            f"（版本 {proxy.get('代理版本')}）作为识别出口"
        )

    def _enable(self, entry: dict[str, Any], current: str) -> tuple[dict[str, Any] | None, str]:
        if current != "策略验证中":
            return None, (
                f"验证码策略 {entry.get('策略编号')} 当前为「{current}」，"
                f"不能执行「启用策略」（生命周期单向推进：{' → '.join(STATUS_ORDER)}）"
            )
        record = proxyalloc.complete_recognition(entry)
        if record is None:
            return None, (
                f"验证码策略 {entry.get('策略编号')} 的识别出口已失效或尚未分配存活代理，"
                "请重新提交策略验证"
            )
        proxyalloc.release_allocation(entry)
        entry["status"] = "已启用"
        entry["pending"], entry["abnormal"] = statelib.captcha_flags(
            "已启用", has_active_allocation=False
        )
        return entry, (
            f"验证码策略已启用，识别记录 {record.get('结果编号')} 识别成功"
            f"（代理 {record.get('代理实例编号')} 版本 {record.get('代理版本')}，已归档为历史）"
        )

    def _degrade(self, entry: dict[str, Any], current: str) -> tuple[dict[str, Any] | None, str]:
        if current != "已启用":
            return None, (
                f"验证码策略 {entry.get('策略编号')} 当前为「{current}」，"
                f"不能执行「降级策略」（生命周期单向推进：{' → '.join(STATUS_ORDER)}）"
            )
        entry["status"] = "已降级"
        entry["pending"], entry["abnormal"] = statelib.captcha_flags(
            "已降级", has_active_allocation=False
        )
        return entry, "验证码策略已降级，如需恢复请基于新的存活代理重新提交策略验证"
