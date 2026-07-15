"""BatchToolExecutor(spec §4.5): 攒批 + partition(连续 safe 合批并发,非 safe 单独串行)。"""
from __future__ import annotations

import asyncio

from .base import ToolExecutor, TrackedTool


class BatchToolExecutor(ToolExecutor):
    """批量执行器: add_tool 只收集(_on_add noop), get_results 时统一 partition+执行。

    - 批内(连续 is_concurrency_safe=True 的工具): asyncio.gather 并发;
    - 非 safe 工具: 单独成批串行执行,避免竞态;
    - 跳过非 queued 的(未知工具在 add_tool 已标 completed,无需再跑)。
    """

    def _on_add(self, tracked: TrackedTool) -> None:
        pass  # 只收集,执行留到 _run_all

    def _partition(self) -> list[list[TrackedTool]]:
        """连续 is_concurrency_safe 工具合批,非安全工具单独一批(reduce 保序,不 sort)。"""
        batches: list[list[TrackedTool]] = []
        cur: list[TrackedTool] = []
        for t in self._tracked:
            if t.status != "queued":
                continue
            safe = self._tools[t.block.name].is_concurrency_safe
            if safe:
                cur.append(t)
            else:
                # 非 safe: 先冲掉当前 safe 批,再单独成批
                if cur:
                    batches.append(cur)
                    cur = []
                batches.append([t])
        if cur:
            batches.append(cur)
        return batches

    async def _run_all(self) -> None:
        for batch in self._partition():
            await asyncio.gather(*(self._execute_single(t) for t in batch))  # 批内并发,批间串行
