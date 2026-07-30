"""常见 MCP server 配置入口。"""
from __future__ import annotations

import zipfile
from pathlib import Path

from .types import MCPServerConfig, MCPTransport


def build_tda_mcp_config(
    jar_path: str | Path,
    *,
    name: str = "tda",
    command: str = "java",
    java_args: list[str] | None = None,
    timeout: float = 20.0,
    disabled: bool = False,
) -> MCPServerConfig:
    """构造真实 TDA stdio MCP server 配置。

    这里不实现 TDA 的 thread dump 解析,只把真实 TDA jar 的 MCP 启动命令收敛
    到一个固定入口:java -Djava.awt.headless=true -jar tda.jar --mcp。
    """

    args = list(java_args) if java_args is not None else ["-Djava.awt.headless=true"]
    args.extend(["-jar", str(Path(jar_path)), "--mcp"])
    return MCPServerConfig(
        name=name,
        command=command,
        args=args,
        timeout=timeout,
        transport=MCPTransport.STDIO,
        disabled=disabled,
    )


def extract_tda_thread_dump_from_zip(zip_path: str | Path, output_dir: str | Path) -> Path:
    """从真实运行现场 zip 中取出 TDA 要分析的 thread-dump.txt。"""

    archive_path = Path(zip_path)
    target_dir = Path(output_dir)
    with zipfile.ZipFile(archive_path) as archive:
        candidates = [
            name
            for name in archive.namelist()
            if name.endswith("thread-dump.txt") and "__MACOSX/" not in name
        ]
        if not candidates:
            raise FileNotFoundError(f"No thread-dump.txt found in {archive_path}")
        member = _pick_runtime_evidence_thread_dump(candidates)
        target = target_dir / Path(member).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(member))
    return target.resolve()


def _pick_runtime_evidence_thread_dump(candidates: list[str]) -> str:
    return sorted(candidates)[-1]
