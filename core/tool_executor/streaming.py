"""StreamingToolExecutor(spec §4.4): 机会主义,事件驱动。

add_tool 即 _try_schedule:能跑就 create_task(不 await),完成回调再扫;
_run_all 收尾:推进未启动的 + await 全部完成。
"""
from __future__ import annotations

import asyncio
import logging

from .base import ToolExecutor, TrackedTool

logger = logging.getLogger("tool_executor")


class StreamingToolExecutor(ToolExecutor):
    """流式执行器: 来一个 tool_use 立即尝试启动(_on_add→_try_schedule)。

    - 机会主义: 能跑就 fire-and-forget(create_task 不 await);
    - 并发规则(_can_execute): 无人跑→可;否则仅当本工具安全且当前 executing 全安全→可;
    - 保序: 遇到跑不了的**非安全**工具则 break, 不让它后面的插队;
    - 事件驱动收尾: 每个 task 完成的 finally 里再 _try_schedule, 启动等待者;
      _run_all 再兜底推进 + await 全部完成(含 cancelled)。
    """

    def _on_add(self, tracked: TrackedTool) -> None:
        # 机会主义: 来一个就立刻尝试启动
        self._try_schedule()

    def _is_safe(self, tracked: TrackedTool) -> bool:
        tool = self._tools.get(tracked.block.name)
        return bool(tool and tool.is_concurrency_safe)

    def _can_execute(self, tracked: TrackedTool) -> bool:
        """md §4.2: 无人跑→可;否则仅当本工具安全且当前 executing 都安全→可。"""
        executing = [t for t in self._tracked if t.status == "executing"]
        if not executing:
            return True
        ok = self._is_safe(tracked) and all(self._is_safe(t) for t in executing)
        logger.info("can_execute %s %s: %s (executing=%d)",
                     tracked.block.id, tracked.block.name, ok, len(executing))
        return ok

    def _try_schedule(self) -> None:
        """扫描 queued, 能跑的启动; 遇到跑不了的**非安全**工具 break 保序。"""
        for t in self._tracked:
            if t.status != "queued":
                continue
            if self._can_execute(t):
                t.status = "executing"
                logger.info("schedule %s %s start", t.block.id, t.block.name)
                t.task = asyncio.create_task(self._run(t))
            elif not self._is_safe(t):
                logger.info("schedule %s %s break (unsafe blocked, 保序)", t.block.id, t.block.name)
                break  # 非安全跑不了→停(给它后面的保序, 不让插队)

    async def _run(self, tracked: TrackedTool) -> None:
        try:
            await self._execute_single(tracked)
        finally:
            # 完成回调: 再扫一遍启动等待中的(事件驱动)
            self._try_schedule()

    async def _run_all(self) -> None:
        self._try_schedule()
        # 等全部完成; 兼顾 cancelled(discard 后 status="cancelled", 不应卡死)
        while any(t.status not in ("completed", "cancelled") for t in self._tracked):
            pending = [t.task for t in self._tracked if t.task is not None and not t.task.done()]
            if not pending:
                break  # 无在跑却仍有未完成→防御退出(理论上不会发生)
            await asyncio.gather(*pending, return_exceptions=True)
