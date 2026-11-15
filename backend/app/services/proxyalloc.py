"""代理分配与验证码识别记录的共享协调逻辑。

代理池与验证码策略是两个模块，但「分配代理 / 撤销分配 / 完成识别」需要
跨两张表原子生效，所以收口在这里：所有函数都假定调用方已持有 store.lock，
锁内一次性完成 代理分配 + 策略状态 + 识别结果 的写入，要么全部生效、要么不动。

识别记录只追加、不改写：待识别 记录可以推进为 识别成功 或 已撤销，
进入终态的记录即为历史，任何接口都不会再动它。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import statelib
from app.store import store

PROXY_MODULE = "proxypool"
CAPTCHA_MODULE = "captcha"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _next_recognition_code() -> str:
    seq = int(store.counters.get("recognition", 0)) + 1
    store.counters["recognition"] = seq
    return f"RECO-{seq:04d}"


def proxy_by_code(code: str | None) -> dict[str, Any] | None:
    if not code:
        return None
    for row in store.rows(PROXY_MODULE):
        if row.get("实例编号") == code:
            return row
    return None


def captcha_rows() -> list[dict[str, Any]]:
    return store.rows(CAPTCHA_MODULE)


def active_record(policy: dict[str, Any]) -> dict[str, Any] | None:
    """策略当前分配对应的待识别记录（未开始的识别任务）。"""
    code = policy.get("当前分配代理")
    if not code:
        return None
    for rec in policy.get("识别记录", []):
        if (
            isinstance(rec, dict)
            and rec.get("状态") == statelib.RECOG_PENDING
            and rec.get("代理实例编号") == code
            and rec.get("代理版本") == policy.get("当前分配代理版本")
        ):
            return rec
    return None


def has_active_allocation(policy: dict[str, Any]) -> bool:
    return active_record(policy) is not None


def allocatable_proxies() -> list[dict[str, Any]]:
    """当前可分配的存活代理：状态可用且未被其他策略占用。"""
    return [
        row
        for row in store.rows(PROXY_MODULE)
        if row.get("status") == "可用" and not row.get("当前分配策略")
    ]


def allocate_exit_proxy(policy: dict[str, Any]) -> dict[str, Any] | None:
    """为策略分配一个新的存活代理作为识别出口，并落一条待识别记录。

    重新验证（已降级再次提交）也走这里：只从当前存活且空闲的代理里挑，
    已回收/被冻结的代理版本永远不会再被选中，保证重新验证一定建立在
    新的存活代理结果上。返回分到的代理；没有可用代理时返回 None，且
    不产生任何副作用（调用方据此整体拒绝，保证原子性）。
    """
    candidates = sorted(allocatable_proxies(), key=lambda row: int(row.get("id", 0)))
    if not candidates:
        return None
    proxy = candidates[0]
    record = {
        "结果编号": _next_recognition_code(),
        "策略编号": policy.get("策略编号"),
        "代理实例编号": proxy.get("实例编号"),
        "代理版本": int(proxy.get("代理版本", 0)),
        # 分配当时冻结快照：历史识别结果按当时代理版本保留
        "代理出站地域": proxy.get("出站地域"),
        "代理响应毫秒": proxy.get("响应毫秒"),
        "状态": statelib.RECOG_PENDING,
        "创建时间": now_iso(),
        "完成时间": None,
        "撤销原因": None,
    }
    policy.setdefault("识别记录", []).append(record)
    policy["当前分配代理"] = proxy.get("实例编号")
    policy["当前分配代理版本"] = int(proxy.get("代理版本", 0))
    proxy["当前分配策略"] = policy.get("策略编号")
    return proxy


def release_allocation(policy: dict[str, Any]) -> None:
    """释放策略当前占用的代理（识别完成后调用）。"""
    proxy = proxy_by_code(policy.get("当前分配代理"))
    if proxy is not None and proxy.get("当前分配策略") == policy.get("策略编号"):
        proxy["当前分配策略"] = None
    policy["当前分配代理"] = None
    policy["当前分配代理版本"] = None


def complete_recognition(policy: dict[str, Any]) -> dict[str, Any] | None:
    """把当前待识别记录推进为识别成功（历史由此定型，之后不再改写）。"""
    record = active_record(policy)
    if record is None:
        return None
    record["状态"] = statelib.RECOG_SUCCESS
    record["完成时间"] = now_iso()
    return record


def revoke_unstarted_for_proxy(proxy: dict[str, Any], *, reason: str) -> list[dict[str, Any]]:
    """代理被回收时，撤销它名下尚未开始识别任务的分配。

    只动 待识别 记录（未占用）：置为 已撤销 并写明原因；
    已完成的识别记录是历史，原样保留。返回被撤销分配的策略列表。
    """
    revoked: list[dict[str, Any]] = []
    code = proxy.get("实例编号")
    for policy in captcha_rows():
        if policy.get("当前分配代理") != code:
            continue
        record = active_record(policy)
        if record is None:
            continue
        record["状态"] = statelib.RECOG_REVOKED
        record["撤销原因"] = reason
        record["完成时间"] = now_iso()
        policy["当前分配代理"] = None
        policy["当前分配代理版本"] = None
        policy["abnormal"] = True  # 识别出口失效，列表页与概览页同步可见
        revoked.append(policy)
    if proxy.get("当前分配策略"):
        proxy["当前分配策略"] = None
    return revoked
