"""FileTracer: 把每个 TraceEvent 全量写成一行 JSONL —— 统一运行日志 sink。

与 LoggingTracer 并列:
  - LoggingTracer 打到 logging(开发时实时看终端)
  - FileTracer 落盘成 jq 可查的 JSONL(默认 logs/{时间戳}.jsonl,或调用方传入的 path;事后排查 / 喂给 LLM 分析)

ctx(chain_id / turn)合并进每行顶层字段;child(**ctx) 派生子 tracer,
orchestrator 每轮 child(turn=n) 让同一轮的请求/工具/错误事件可按 turn join。
seq 在根 tracer 与所有 child 间共享(同一文件全局递增,保序)。

emit 同步 append 写(单行很快);任何 IO 异常静默吞掉 —— 对齐"埋点永不影响主流程"红线。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .events import TraceEvent


class FileTracer:
    def __init__(
        self,
        path: str | None = None,
        ctx: dict | None = None,
        enabled: bool = True,
        _seq: list[int] | None = None,
        _resolved_path: str | None = None,
    ):
        self._ctx = dict(ctx or {})
        self._enabled = enabled
        # seq 用共享 list,使 child() 派生的子 tracer 与根 tracer 共用同一计数器(全局递增保序)
        self._seq = _seq if _seq is not None else [0]
        if _resolved_path is not None:
            self._path = _resolved_path            # child():继承根实例已解析的最终路径(写同一文件)
            return
        # 根实例:path 未传 → 默认 logs/{时间戳}.jsonl(每次运行独立文件);
        # path 传入 → 原样使用,不做任何拼接(调用方自己决定文件名)。
        if path is None:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._path = f"logs/{ts}.jsonl"
        else:
            self._path = path
        if enabled:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: TraceEvent) -> None:
        if not self._enabled:
            return
        try:
            self._seq[0] += 1
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "seq": self._seq[0],
                **self._ctx,
                "kind": event.kind.value,
                "payload": event.payload,
            }
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # 埋点永不影响主流程

    def child(self, **ctx) -> "FileTracer":
        """派生子 tracer:合并额外 ctx(如 turn=n),共享同一文件与 seq 计数。"""
        return FileTracer(
            self._path, {**self._ctx, **ctx}, self._enabled,
            _seq=self._seq, _resolved_path=self._path,
        )
