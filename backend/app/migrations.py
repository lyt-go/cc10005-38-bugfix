"""示例数据迁移：把旧版（无代理分配/识别记录字段）的行升级到当前结构。

设计约束：
- 只增不删：旧行里的业务字段原样保留，新字段按状态推导补默认值；
- 历史验证码识别记录（识别记录 列表）已存在时原样保留，绝不改写；
- 迁移可重复执行（幂等），本地开发环境每次启动都会过一遍，保证兼容。
"""
from __future__ import annotations

from typing import Any

from app import statelib

SCHEMA_VERSION = 1


def _migrate_proxy(row: dict[str, Any], report: dict[str, int]) -> None:
    # 代理版本：可用代理至少经历过一次存活检测，从 1 开始；其余待激活为 0
    if "代理版本" not in row:
        row["代理版本"] = 1 if row.get("status") == "可用" else 0
    row.setdefault("当前分配策略", None)
    row.setdefault("冻结出站地域", None)
    row.setdefault("冻结响应毫秒", None)
    row.setdefault("回收时间", None)
    pending, abnormal = statelib.proxy_flags(str(row.get("status", "草稿")))
    row["pending"] = pending
    row["abnormal"] = abnormal
    report["proxypool_rows"] += 1


def _migrate_captcha(row: dict[str, Any], report: dict[str, int]) -> None:
    records = row.setdefault("识别记录", [])
    if not isinstance(records, list):  # 异常旧数据兜底：不丢内容，包成列表
        row["识别记录"] = [records]
        records = row["识别记录"]
    report["preserved_recognitions"] += len(records)
    row.setdefault("当前分配代理", None)
    row.setdefault("当前分配代理版本", None)
    has_active = bool(row.get("当前分配代理")) and any(
        isinstance(rec, dict) and rec.get("状态") == statelib.RECOG_PENDING for rec in records
    )
    pending, abnormal = statelib.captcha_flags(
        str(row.get("status", "草稿")), has_active_allocation=has_active
    )
    row["pending"] = pending
    row["abnormal"] = abnormal
    report["captcha_rows"] += 1


def apply(store: Any) -> dict[str, int]:
    """把 store 里的示例数据迁移到当前 schema；返回迁移报告供启动日志与验证使用。"""
    report = {"proxypool_rows": 0, "captcha_rows": 0, "preserved_recognitions": 0}
    for row in store.rows("proxypool"):
        _migrate_proxy(row, report)
    for row in store.rows("captcha"):
        _migrate_captcha(row, report)
    # 识别记录编号计数器：从存量历史记录的最大编号继续，避免新记录撞号改写历史
    seq = 0
    for row in store.rows("captcha"):
        for rec in row.get("识别记录", []):
            code = str(rec.get("结果编号", "")) if isinstance(rec, dict) else ""
            if code.startswith("RECO-"):
                try:
                    seq = max(seq, int(code.split("-", 1)[1]))
                except ValueError:
                    continue
    store.counters["recognition"] = max(int(store.counters.get("recognition", 0)), seq)
    store.meta["schema_version"] = SCHEMA_VERSION
    store.migration_report = report
    return report
