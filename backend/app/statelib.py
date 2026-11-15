"""代理池与验证码策略的生命周期规则：状态序列、允许的动作与状态标记口径。

这一模块是两个业务面（列表页 / 概览页）共用的唯一事实来源：
- 状态流转只许单向推进，动作与前置状态在这里登记；
- pending / abnormal 标记由状态推导，任何入口（动作、迁移）都走同一套口径，
  避免列表页和概览页各算各的、结论对不上。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 代理实例生命周期：草稿 → 存活检测中 → 可用 → 已回收（单向，已回收为终态）
# ---------------------------------------------------------------------------
PROXY_STATUS_ORDER = ["草稿", "存活检测中", "可用", "已回收"]

# 动作 -> (允许的前置状态集合, 目标状态)
PROXY_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "提交存活检测": (frozenset({"草稿"}), "存活检测中"),
    "标记可用": (frozenset({"存活检测中"}), "可用"),
    # 回收是终态动作：任何未回收状态都可以回收，已回收不可重复回收
    "回收实例": (frozenset({"草稿", "存活检测中", "可用"}), "已回收"),
}

PROXY_STATUS_FLAGS: dict[str, tuple[bool, bool]] = {
    # 状态 -> (pending, abnormal)
    "草稿": (True, False),
    "存活检测中": (True, False),
    "可用": (False, False),
    "已回收": (False, False),
}

# ---------------------------------------------------------------------------
# 验证码策略生命周期：草稿 → 策略验证中 → 已启用 → 已降级（单向推进）
# 已降级允许重新提交验证，但必须建立在新的存活代理结果上（见 captcha 服务）。
# ---------------------------------------------------------------------------
CAPTCHA_STATUS_ORDER = ["草稿", "策略验证中", "已启用", "已降级"]

# 识别记录状态：待识别（未开始，可被回收撤销）→ 识别成功 / 已撤销（终态，历史不可改写）
RECOG_PENDING = "待识别"
RECOG_SUCCESS = "识别成功"
RECOG_REVOKED = "已撤销"
RECOG_TERMINAL = frozenset({RECOG_SUCCESS, RECOG_REVOKED})


def proxy_flags(status: str) -> tuple[bool, bool]:
    """代理实例状态对应的 (pending, abnormal)。"""
    return PROXY_STATUS_FLAGS.get(status, (True, False))


def captcha_flags(status: str, *, has_active_allocation: bool) -> tuple[bool, bool]:
    """验证码策略状态对应的 (pending, abnormal)。

    策略验证中但识别出口（代理分配）已失效的，标记为异常，
    这样列表页与概览页对「异常量」的结论一致。
    """
    if status == "草稿":
        return True, False
    if status == "策略验证中":
        return True, not has_active_allocation
    if status == "已启用":
        return False, False
    if status == "已降级":
        # 降级运行是需要关注的异常态
        return False, True
    return True, False
