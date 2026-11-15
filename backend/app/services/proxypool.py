"""代理池管理业务规则：状态流转、字段校验与筛选口径都收在这里。

生命周期单向推进：草稿 → 存活检测中 → 可用 → 已回收。
回收实例是终态动作：立即撤销该代理名下尚未开始识别任务的分配，
并冻结当时的出站地域与响应毫秒，已回收代理不可再复活、不可再分配。
"""
from __future__ import annotations

from typing import Any

from app import statelib
from app.services import proxyalloc
from app.store import store

MODULE = "proxypool"
REQUIRED_FIELDS = ["实例编号", "代理地址", "代理端口"]
STATUS_ORDER = statelib.PROXY_STATUS_ORDER
ACTION_RULES = {action: target for action, (_, target) in statelib.PROXY_TRANSITIONS.items()}

FILTERABLE_FIELDS = ["实例编号", "代理地址", "代理端口"]


class ProxypoolService:
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
            "代理实例总数": len(rows),
            "可用实例数": sum(1 for row in rows if row.get("status") == "可用"),
            "检测中实例数": sum(1 for row in rows if row.get("status") == "存活检测中"),
        }
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("实例编号", ""))]
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
            entry["pending"], entry["abnormal"] = statelib.proxy_flags(entry["status"])
            entry["代理版本"] = 0
            entry["当前分配策略"] = None
            entry["冻结出站地域"] = None
            entry["冻结响应毫秒"] = None
            entry["回收时间"] = None
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        # 锁内完成 查状态 + 校验 + 流转 + 撤销分配，保证跨模块原子性
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"代理实例 {entry_id} 不存在或已归档"
            transition = statelib.PROXY_TRANSITIONS.get(action)
            if transition is None:
                return None, f"动作「{action}」不属于代理池管理可执行范围"
            allowed_from, target = transition
            current = str(entry.get("status", ""))
            if current not in allowed_from:
                if current == "已回收":
                    return None, f"代理实例 {entry.get('实例编号')} 已回收，生命周期已终结，不可再{action}"
                return None, (
                    f"代理实例 {entry.get('实例编号')} 当前为「{current}」，"
                    f"不能执行「{action}」（生命周期单向推进：{' → '.join(STATUS_ORDER)}）"
                )
            if action == "回收实例":
                return self._recycle(entry)
            if action == "标记可用":
                # 每一次存活检测通过都开启一个新的代理版本，历史识别结果仍指向旧版本
                entry["代理版本"] = int(entry.get("代理版本", 0)) + 1
                entry["存活检测时间"] = proxyalloc.now_iso()[:10]
            entry["status"] = target
            entry["pending"], entry["abnormal"] = statelib.proxy_flags(target)
            return entry, f"代理实例已{action}"

    def _recycle(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        # 冻结回收当时的出站地域与响应毫秒，此后该代理版本不再变化
        entry["冻结出站地域"] = entry.get("出站地域")
        entry["冻结响应毫秒"] = entry.get("响应毫秒")
        entry["回收时间"] = proxyalloc.now_iso()
        revoked = proxyalloc.revoke_unstarted_for_proxy(
            entry, reason=f"代理 {entry.get('实例编号')} 已回收"
        )
        entry["status"] = "已回收"
        entry["pending"], entry["abnormal"] = statelib.proxy_flags("已回收")
        message = f"代理实例已回收，已冻结出站地域与响应毫秒（版本 {entry.get('代理版本')}）"
        if revoked:
            codes = "、".join(str(policy.get("策略编号")) for policy in revoked)
            message += f"；已撤销未开始识别任务的分配：{codes}"
        return entry, message
