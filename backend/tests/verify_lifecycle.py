"""验证码策略 × 代理池生命周期修复的端到端验证。

覆盖任务要求的全部场景：
  1. 示例数据迁移兼容（旧行补新字段、历史识别记录原样保留、计数器对齐）
  2. 回收拦截：PROX-0001 回收后立即撤销 CAPT-0002 未开始识别任务的分配，
     冻结当时出站地域与响应毫秒，CAPT-0002 不能再用它做识别出口
  3. 重复提交：验证中且出口有效时连续点「提交策略验证」不再产生状态变化
  4. 单向推进：草稿/验证中/启用/降级只能向前走，非法跳转一律拦截
  5. 合法恢复：CAPT-0003 降级后基于新的存活代理（PROX-0003）重新验证并启用
  6. 并发争用：两个策略同时抢最后一个存活代理，只有一个成功
  7. 历史保留：识别记录按当时代理版本归档，后续操作不改写历史
  8. 口径一致：列表页、导出标签页、运营概览从同一调用链取数且结论一致

用法：先起后端（./run.sh），再执行 `.venv/bin/python tests/verify_lifecycle.py`。
"""
from __future__ import annotations

import copy
import json
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

# 允许从仓库任意目录执行：把 backend/ 根目录放进模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8000"
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_checks = 0
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if condition:
        print(f"  [{PASS}] {name}")
    else:
        _failures.append(name)
        print(f"  [{FAIL}] {name} {detail}")


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------
def api(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def action(module: str, entry_id: int, name: str) -> dict:
    return api("POST", f"/api/{module}/{entry_id}/actions", {"values": {"action": name}})


# ---------------------------------------------------------------------------
# 1. 迁移兼容（进程内，不依赖服务）
# ---------------------------------------------------------------------------
def verify_migration() -> None:
    print("\n== 1. 示例数据迁移兼容 ==")
    from app.seed import SEED_ROWS
    import app.store as store_mod

    legacy = copy.deepcopy(SEED_ROWS)
    # 构造一条“历史识别记录”混在旧数据里，验证迁移后原样保留
    legacy["captcha"][2]["识别记录"] = [
        {
            "结果编号": "RECO-0007",
            "策略编号": "CAPT-0003",
            "代理实例编号": "PROX-0099",
            "代理版本": 3,
            "代理出站地域": "历史地域",
            "代理响应毫秒": "88",
            "状态": "识别成功",
            "创建时间": "2026-09-01T00:00:00",
            "完成时间": "2026-09-01T00:00:01",
            "撤销原因": None,
        }
    ]
    original = store_mod.SEED_ROWS
    store_mod.SEED_ROWS = legacy
    try:
        fresh = store_mod.Store()
    finally:
        store_mod.SEED_ROWS = original

    proxies = {row["实例编号"]: row for row in fresh.rows("proxypool")}
    check("迁移：草稿代理版本从 0 开始", proxies["PROX-0001"]["代理版本"] == 0)
    check("迁移：可用代理版本从 1 开始", proxies["PROX-0003"]["代理版本"] == 1)
    check("迁移：代理补齐冻结字段", proxies["PROX-0001"]["冻结出站地域"] is None
          and "回收时间" in proxies["PROX-0001"])
    capt2 = next(row for row in fresh.rows("captcha") if row["策略编号"] == "CAPT-0002")
    check("迁移：旧策略补齐识别记录与分配字段", capt2["识别记录"] == []
          and capt2["当前分配代理"] is None)
    check("迁移：验证中但无有效分配的旧策略标记异常", capt2["abnormal"] is True)
    capt3 = next(row for row in fresh.rows("captcha") if row["策略编号"] == "CAPT-0003")
    check("迁移：历史识别记录原样保留", len(capt3["识别记录"]) == 1
          and capt3["识别记录"][0]["结果编号"] == "RECO-0007"
          and capt3["识别记录"][0]["代理版本"] == 3)
    check("迁移：识别记录计数器与历史对齐（不会撞号改写历史）",
          fresh.counters["recognition"] >= 7)
    check("迁移：schema 版本已登记", fresh.meta.get("schema_version") == 1)


# ---------------------------------------------------------------------------
# 2-8. 在线场景（需要服务已启动）
# ---------------------------------------------------------------------------
def verify_online() -> None:
    print("\n== 0. 服务健康 ==")
    health = api("GET", "/api/health")
    check("服务健康检查", health.get("ok") is True)

    print("\n== 2. 代理生命周期单向推进（PROX-0001） ==")
    prox1 = api("GET", "/api/proxypool/1")
    check("迁移后代理带版本字段", prox1.get("代理版本") == 0)
    r = action("proxypool", 1, "标记可用")
    check("草稿不能直接标记可用", r["ok"] is False, r.get("message"))
    r = action("proxypool", 1, "提交存活检测")
    check("草稿 → 存活检测中", r["ok"] is True and r["entry"]["status"] == "存活检测中")
    r = action("proxypool", 1, "提交存活检测")
    check("重复提交存活检测被拦截", r["ok"] is False)
    r = action("proxypool", 1, "标记可用")
    check("存活检测中 → 可用，版本升为 1", r["ok"] is True and r["entry"]["代理版本"] == 1)

    print("\n== 3. CAPT-0002 提交策略验证：分配存活代理作为识别出口 ==")
    capt2 = api("GET", "/api/captcha/2")
    check("迁移后 CAPT-0002 无有效分配且标记异常", capt2["当前分配代理"] is None
          and capt2["abnormal"] is True)
    r = action("captcha", 2, "提交策略验证")
    check("提交成功并分配到 PROX-0001", r["ok"] is True
          and r["entry"]["当前分配代理"] == "PROX-0001", r.get("message"))
    rec = r["entry"]["识别记录"][-1]
    check("识别记录快照当时代理版本与地域/耗时",
          rec["状态"] == "待识别" and rec["代理版本"] == 1
          and rec["代理出站地域"] == "代理池管理样例1")
    check("分配后异常标记清除", r["entry"]["abnormal"] is False)
    prox1 = api("GET", "/api/proxypool/1")
    check("代理侧登记占用策略", prox1["当前分配策略"] == "CAPT-0002")

    print("\n== 4. 重复提交被拦截，不再产生多次状态变化 ==")
    before = api("GET", "/api/captcha/2")
    r = action("captcha", 2, "提交策略验证")
    check("验证中且出口有效时重复提交被拒绝", r["ok"] is False, r.get("message"))
    r2 = action("captcha", 2, "提交策略验证")
    after = api("GET", "/api/captcha/2")
    check("连续点击后状态与识别记录均未变化",
          after["status"] == before["status"] == "策略验证中"
          and len(after["识别记录"]) == len(before["识别记录"]) == 1
          and after["abnormal"] == before["abnormal"])

    print("\n== 5. 回收拦截：PROX-0001 回收即撤销 CAPT-0002 未开始的分配 ==")
    r = action("proxypool", 1, "回收实例")
    check("回收成功且消息指明撤销了 CAPT-0002", r["ok"] is True
          and "CAPT-0002" in r.get("message", ""), r.get("message"))
    prox1 = api("GET", "/api/proxypool/1")
    check("代理已回收并冻结当时地域与响应毫秒",
          prox1["status"] == "已回收"
          and prox1["冻结出站地域"] == "代理池管理样例1"
          and prox1["冻结响应毫秒"] == "代理池管理样例1"
          and bool(prox1["回收时间"]))
    capt2 = api("GET", "/api/captcha/2")
    rec = capt2["识别记录"][-1]
    check("CAPT-0002 分配被撤销、出口清空、标记异常",
          capt2["当前分配代理"] is None and capt2["abnormal"] is True)
    check("未开始的识别记录置为已撤销且保留当时代理版本",
          rec["状态"] == "已撤销" and rec["代理版本"] == 1 and bool(rec["撤销原因"]))
    r = action("captcha", 2, "启用策略")
    check("出口已失效的策略不能启用（回收拦截生效）", r["ok"] is False, r.get("message"))
    r = action("proxypool", 1, "提交存活检测")
    check("已回收代理不可复活", r["ok"] is False)
    r = action("proxypool", 1, "回收实例")
    check("已回收代理不可重复回收", r["ok"] is False)

    print("\n== 6. 合法恢复：CAPT-0002 基于新的存活代理重新验证 ==")
    r = action("captcha", 2, "提交策略验证")
    check("出口被撤销后允许重新提交并分配到 PROX-0003", r["ok"] is True
          and r["entry"]["当前分配代理"] == "PROX-0003", r.get("message"))
    r = action("captcha", 2, "启用策略")
    check("验证完成后启用，代理释放", r["ok"] is True
          and r["entry"]["status"] == "已启用" and r["entry"]["当前分配代理"] is None)
    prox3 = api("GET", "/api/proxypool/3")
    check("PROX-0003 占用已释放", prox3["当前分配策略"] is None)

    print("\n== 7. 合法恢复：CAPT-0003 降级 → 新的存活代理重新验证 → 启用 ==")
    r = action("captcha", 3, "提交策略验证")
    check("已启用不能直接提交验证", r["ok"] is False)
    r = action("captcha", 3, "降级策略")
    check("已启用 → 已降级", r["ok"] is True and r["entry"]["status"] == "已降级")
    r = action("captcha", 3, "启用策略")
    check("已降级不能直接启用（单向推进）", r["ok"] is False)
    r = action("captcha", 3, "提交策略验证")
    check("已降级重新验证必须落在新的存活代理上", r["ok"] is True
          and r["entry"]["当前分配代理"] == "PROX-0003", r.get("message"))
    r = action("captcha", 3, "启用策略")
    check("重新验证后合法恢复为已启用", r["ok"] is True and r["entry"]["status"] == "已启用")
    capt3 = api("GET", "/api/captcha/3")
    rec3 = capt3["识别记录"][-1]
    check("CAPT-0003 识别记录按当时代理版本归档",
          rec3["状态"] == "识别成功" and rec3["代理实例编号"] == "PROX-0003"
          and rec3["代理版本"] == prox3["代理版本"])

    print("\n== 8. 并发争用最后一个存活代理：只有一个成功 ==")
    ra = api("POST", "/api/captcha", {"values": {"策略编号": "CAPT-9001", "验证码类型": "图像", "识别服务商": "vendorA"}})
    rb = api("POST", "/api/captcha", {"values": {"策略编号": "CAPT-9002", "验证码类型": "滑块", "识别服务商": "vendorB"}})
    check("两条新策略登记成功", ra["ok"] is True and rb["ok"] is True)
    id_a, id_b = ra["entry"]["id"], rb["entry"]["id"]
    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda: results.append(action("captcha", id_a, "提交策略验证"))),
        threading.Thread(target=lambda: results.append(action("captcha", id_b, "提交策略验证"))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if r["ok"]]
    check("两个策略并发抢最后一个代理，恰好一个成功", len(winners) == 1,
          json.dumps(results, ensure_ascii=False))
    winner_id = id_a if winners and winners[0]["entry"]["策略编号"] == "CAPT-9001" else id_b
    loser_id = id_b if winner_id == id_a else id_a
    r = action("captcha", loser_id, "提交策略验证")
    check("落败方重试仍被拒绝（无存活代理可分配）", r["ok"] is False)
    r = action("captcha", winner_id, "启用策略")
    check("胜方完成验证并释放代理", r["ok"] is True and r["entry"]["当前分配代理"] is None)

    print("\n== 9. 历史识别记录不被改写 ==")
    before = {row["策略编号"]: copy.deepcopy(row["识别记录"])
              for row in api("GET", "/api/captcha", params={"size": 200})["items"]}
    r = action("captcha", 3, "降级策略")
    r = action("captcha", 3, "提交策略验证")
    check("再次重新验证仍从存活代理开始", r["ok"] is True
          and r["entry"]["当前分配代理"] == "PROX-0003")
    r = action("captcha", 3, "启用策略")
    check("再次合法恢复", r["ok"] is True)
    after = {row["策略编号"]: row["识别记录"]
             for row in api("GET", "/api/captcha", params={"size": 200})["items"]}
    immutable = True
    for code, old_records in before.items():
        new_records = {rec["结果编号"]: rec for rec in after.get(code, [])}
        for old in old_records:
            if old["状态"] in ("识别成功", "已撤销") and new_records.get(old["结果编号"]) != old:
                immutable = False
    check("终态识别记录逐字节未变（历史不可改写）", immutable)
    prox1_refs = [rec for recs in after.values() for rec in recs
                  if rec["代理实例编号"] == "PROX-0001"]
    check("已回收代理再未出现在新识别结果里",
          len(prox1_refs) == 1 and prox1_refs[0]["状态"] == "已撤销")

    print("\n== 10. 列表页 / 导出标签页 / 运营概览口径一致 ==")
    lst = api("GET", "/api/captcha", params={"策略编号": "CAPT-0003"})
    exp = api("GET", "/api/captcha/export", params={"策略编号": "CAPT-0003"})
    check("导出标签页不再 422，且与列表页同一筛选口径",
          lst["total"] == exp["total"] == 1
          and lst["items"][0]["策略编号"] == exp["items"][0]["策略编号"])
    lst_all = api("GET", "/api/captcha", params={"size": 200})
    overview = api("GET", "/api/overview")
    cap_mod = next(m for m in overview["modules"] if m["name"] == "captcha")
    check("列表统计卡与概览页同源一致",
          lst_all["stats"]["验证码策略总数"] == cap_mod["created"]
          and sum(1 for r in lst_all["items"] if r["pending"]) == cap_mod["pending"]
          and sum(1 for r in lst_all["items"] if r["abnormal"]) == cap_mod["abnormal"])
    prox_lst = api("GET", "/api/proxypool", params={"实例编号": "PROX-0003"})
    prox_exp = api("GET", "/api/proxypool/export", params={"实例编号": "PROX-0003"})
    check("代理池导出标签页与列表页一致",
          prox_lst["total"] == prox_exp["total"] == 1
          and prox_lst["items"][0]["实例编号"] == prox_exp["items"][0]["实例编号"])
    check("代理池统计卡口径正确",
          prox_lst["stats"]["代理实例总数"] == 3
          and prox_lst["stats"]["可用实例数"] == 1
          and prox_lst["stats"]["检测中实例数"] == 1)


def main() -> int:
    verify_migration()
    verify_online()
    print(f"\n共 {_checks} 项检查，失败 {len(_failures)} 项")
    if _failures:
        print("失败项：", "；".join(_failures))
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
