"""Bash 工具:执行 shell 命令

  * 「别抢专用工具的活」= 纯 prompt 软引导。CC 代码层面**不拦** bash 里的
    grep/find/cat/sed/echo(反而因判为只读而顺畅放行),只在 description 里劝导
    改用专用工具 + 给对照表,并留"unless 专用工具搞不定"的逃生阀。本工具照此:
    只在 DESCRIPTION 里软引导,func 不做命令黑名单。
  * 「别干危险的事」= 属于权限层(can_use_tool)。对齐 CC「安全边界在权限系统」的
    分工,本工具不自造命令黑名单/沙箱 —— 危险拦截交给 can_use_tool。
  * 执行边界照搬 CC 常量:超时默认 2min、上限 10min;输出截断 30K 字符;
    捕获 stdout+stderr+退出码;取消/超时时 kill 子进程。

砍掉的非核心:run_in_background(需后台任务子系统,本项目暂无)、tree-sitter 命令
解析、只读命令动态并发判定
"""
from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel, Field

from ..tools import ToolContext, build_tool

# 执行边界常量
DEFAULT_TIMEOUT_MS = 120_000   # 2 分钟
MAX_TIMEOUT_MS = 600_000       # 10 分钟
MAX_OUTPUT_CHARS = 30_000      # 对齐 CC maxResultSizeChars


def _resolve_timeout_ms(timeout_ms: int | None) -> int:
    """解析超时:入参 > 默认;并封顶到上限。支持环境变量覆盖(对齐 CC)。"""
    default = int(os.environ.get("BASH_DEFAULT_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS)
    maximum = int(os.environ.get("BASH_MAX_TIMEOUT_MS") or MAX_TIMEOUT_MS)
    t = timeout_ms if timeout_ms is not None else default
    return min(t, maximum)


class BashInput(BaseModel):
    command: str = Field(description="要执行的 bash 命令")
    timeout: int | None = Field(
        default=None, description=f"超时毫秒数,上限 {MAX_TIMEOUT_MS}(10 分钟);省略用默认 2 分钟"
    )
    description: str | None = Field(
        default=None, description="用 5-10 个词、主动语态描述这条命令做什么(如「运行单元测试」)"
    )


async def _bash_func(inp: BashInput, ctx: ToolContext) -> str:
    timeout_s = _resolve_timeout_ms(inp.timeout) / 1000

    # 用 bash -c 执行整条命令:command 作为单个 argv 传入,保留管道/重定向等 shell 能力
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", inp.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError(f"命令超时(> {int(timeout_s * 1000)}ms),已被终止。") from None
    except asyncio.CancelledError:
        # 被 executor.discard() 取消:先杀子进程再抛出(与 ripgrep 一致)
        proc.kill()
        raise

    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    parts: list[str] = []
    if out.strip():
        parts.append(out.rstrip("\n"))
    if err.strip():
        parts.append(err.rstrip("\n"))  # stderr 一并返回(对齐 CC)
    body = "\n".join(parts) if parts else "(命令无输出)"

    if len(body) > MAX_OUTPUT_CHARS:
        body = body[:MAX_OUTPUT_CHARS] + f"\n\n[输出已截断:超过 {MAX_OUTPUT_CHARS} 字符]"
    if proc.returncode != 0:
        body += f"\n\n[退出码: {proc.returncode}]"
    return body


# 软边界 prompt:对齐 CC BashTool/prompt.ts 的 getSimplePrompt
# —— 劝导 + 对照表 + 逃生阀。代码不拦,只靠这段文案引导模型用专用工具。
DESCRIPTION = (
    "执行一条 bash 命令并返回其输出(stdout + stderr + 退出码)。\n\n"
    "IMPORTANT:除非被明确要求,或你已确认专用工具无法完成任务,否则请避免用本工具运行 "
    "`find`、`grep`、`cat`、`head`、`tail`、`sed`、`awk`、`echo` 这类命令。"
    "应改用对应的专用工具 —— 它们体验更好、也更便于审查工具调用:\n"
    "- 文件查找:用 Glob(不要用 find 或 ls)\n"
    "- 内容搜索:用 Grep(不要用 grep 或 rg)\n"
    "- 读文件:用 Read(不要用 cat / head / tail)\n"
    "- 改文件:用 Edit(不要用 sed / awk)\n"
    "- 写文件:用 Write(不要用 echo > 或 cat <<EOF)\n"
    "- 与用户沟通:直接输出文字(不要用 echo / printf)\n\n"
    "本工具适合专用工具做不了的执行类任务:运行程序/脚本、跑测试(python、pytest)、"
    "git 操作、包管理等。"
)


BASH_TOOL = build_tool(
    name="Bash",
    description=DESCRIPTION,
    input_model=BashInput,
    func=_bash_func,
    # bash 的 isReadOnly 默认 false;。
)
