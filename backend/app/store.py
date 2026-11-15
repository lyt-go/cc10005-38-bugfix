"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

并发说明：FastAPI 的同步接口跑在线程池里，代理分配、策略状态与识别结果
需要跨表原子更新，因此仓库提供一把可重入锁（store.lock），所有状态流转
都在锁内完成，等价于真实数据库里的事务边界。
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from app import migrations
from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        # 跨模块写操作（代理分配 + 策略状态 + 识别结果）的互斥锁
        self.lock = threading.RLock()
        # 业务编号计数器（如识别记录 RECO-0001），迁移时会与存量数据对齐
        self.counters: dict[str, int] = {}
        self.meta: dict[str, Any] = {}
        self.migration_report: dict[str, int] = {}
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: copy.deepcopy(rows) for name, rows in SEED_ROWS.items()
        }
        # 启动即迁移：旧版示例数据升级到当前 schema，历史识别记录原样保留
        self.migration_report = migrations.apply(self)

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
