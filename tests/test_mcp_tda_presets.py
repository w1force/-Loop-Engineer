"""TDA MCP preset helpers."""
from __future__ import annotations

import zipfile

from core.mcp import build_tda_mcp_config, extract_tda_thread_dump_from_zip


def test_build_tda_config_does_not_embed_local_paths():
    config = build_tda_mcp_config("/opt/tools/tda.jar")

    assert config.command == "java"
    assert config.args == [
        "-Djava.awt.headless=true",
        "-jar",
        "/opt/tools/tda.jar",
        "--mcp",
    ]


def test_extract_thread_dump_from_zip_uses_generic_file_name(tmp_path):
    archive_path = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("evidence/a/thread-dump.txt", "old dump")
        archive.writestr("__MACOSX/evidence/a/thread-dump.txt", "metadata")
        archive.writestr("evidence/b/thread-dump.txt", "new dump")

    extracted = extract_tda_thread_dump_from_zip(archive_path, tmp_path / "out")

    assert extracted.name == "thread-dump.txt"
    assert extracted.read_text(encoding="utf-8") == "new dump"
