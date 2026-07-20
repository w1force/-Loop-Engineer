"""ripgrep 异步子进程封装(对齐 CC 的 src/utils/ripgrep.ts,简化版)。

glob 与 grep 都构建在这一个 ripgrep 引擎上;本模块是唯一 spawn ``rg`` 的地方。
用 ``asyncio.create_subprocess_exec`` 直接以子进程方式启动 rg 二进制(不经过
shell),参数以数组传入,捕获 stdout 解析成行。

对齐 CC 的核心行为:
  * 优先系统 ``rg``(或 ``RIPGREP_PATH`` 覆盖)
  * 退出码语义:0 = 有匹配,1 = 无匹配(都算成功),2 = 用法错误
  * 超时 kill;task 被取消(abort)时也 kill 子进程

刻意砍掉的非核心内容:EAGAIN 单线程重试、SIGTERM→SIGKILL 升级、内嵌 argv0
分发、20MB 缓冲上限、部分结果抢救。
"""
from __future__ import annotations

import asyncio
import os
import shutil
from functools import lru_cache

DEFAULT_TIMEOUT = 20.0  # 秒


class RipgrepTimeoutError(Exception):
    """ripgrep 超时。让调用方能区分「超时」与「无匹配」。"""


@lru_cache(maxsize=1)
def rg_command() -> str:
    """解析 rg 可执行文件:RIPGREP_PATH 覆盖 > PATH 上的 rg。"""
    override = os.environ.get("RIPGREP_PATH")
    if override:
        return override
    found = shutil.which("rg")
    if found:
        return found
    raise FileNotFoundError(
        "在 PATH 上找不到 ripgrep (rg)。请安装(brew/apt install ripgrep)或设 RIPGREP_PATH。"
    )


async def rip_grep(
    args: list[str],
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    """跑 rg,返回输出行(已去空行/去行尾 \\r)。

    非交互模式下 rg 需要一个路径作为最后参数,故 ``target`` 恒定追加在末尾。
    """
    rg = rg_command()
    proc = await asyncio.create_subprocess_exec(
        rg,
        *args,
        target,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RipgrepTimeoutError(f"ripgrep 搜索超时({timeout}s),请用更具体的路径或模式。")
    except asyncio.CancelledError:
        # 被 executor.discard() 取消时,先杀子进程再把取消抛上去。
        proc.kill()
        raise

    rc = proc.returncode
    if rc in (0, 1):  # 0=有匹配,1=无匹配,都算成功
        text = stdout.decode("utf-8", "replace")
        return [ln.rstrip("\r") for ln in text.strip().split("\n") if ln.strip()]

    # rc==2 或其他:用法错误 / rg 本身出错,抛给上层(统一入口会转成 error 结果)
    msg = stderr.decode("utf-8", "replace").strip()
    raise RuntimeError(f"ripgrep 退出码 {rc}: {msg}")
