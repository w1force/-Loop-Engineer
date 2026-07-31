"""Java/JVM MCP 工厂测试 - TDD RED 阶段。

锁定 create_java_jvm_mcp_manager 的配置语义: TDA jar 绝对路径、tda/memory-analyzer
两个 server、不在 import/构造阶段启动子进程、jar 缺失时失败明确。
"""
from pathlib import Path

import pytest

from core.mcp import MCPManager, MCPServerConfig
from core.types import Message, StreamEvent
from diagnose.platform_impl.java_jvm.mcp import create_java_jvm_mcp_manager


class _FakeProvider:
    def stream(self, **_: object):
        async def events():
            if False:
                yield StreamEvent(type="message_stop")

        return events()

    def count_tokens(self, messages: list[Message]) -> int:
        return 0


def _cfg_map(manager: MCPManager) -> dict[str, MCPServerConfig]:
    # MCPManager._configs 是 name -> config 的私有映射；此处只作纯配置断言,
    # 不启动 server。若未来提供公开 config view,应改用该公开接口。
    return dict(manager._configs)


@pytest.fixture
def project_root_with_tda(tmp_path) -> Path:
    jar = tmp_path / "mcp-assets/tda/tda-3.2.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"PK")  # 占位 jar；工厂不得读取或启动它
    return tmp_path


class TestTdaConfig:
    def test_returns_mcp_manager(self, project_root_with_tda):
        manager = create_java_jvm_mcp_manager(project_root=project_root_with_tda)
        assert isinstance(manager, MCPManager)

    def test_has_tda_and_memory_analyzer_servers(self, project_root_with_tda):
        manager = create_java_jvm_mcp_manager(project_root=project_root_with_tda)
        names = set(_cfg_map(manager).keys())
        assert {"tda", "memory-analyzer"} <= names

    def test_tda_uses_absolute_jar_path(self, project_root_with_tda):
        jar = project_root_with_tda / "mcp-assets/tda/tda-3.2.jar"
        manager = create_java_jvm_mcp_manager(project_root=project_root_with_tda)
        tda = _cfg_map(manager)["tda"]
        assert tda.command == "java"
        assert str(jar.resolve()) in tda.args
        assert Path(tda.args[tda.args.index("-jar") + 1]).is_absolute()

    def test_tda_args_exact_flags(self, project_root_with_tda):
        tda = _cfg_map(manager := create_java_jvm_mcp_manager(project_root=project_root_with_tda))["tda"]
        assert "-Djava.awt.headless=true" in tda.args
        assert "-jar" in tda.args
        assert "--mcp" in tda.args

    def test_memory_analyzer_uses_npx(self, project_root_with_tda):
        ma = _cfg_map(manager := create_java_jvm_mcp_manager(project_root=project_root_with_tda))["memory-analyzer"]
        assert ma.command == "npx"
        assert "-y" in ma.args
        assert "jvm-heap-dump-mcp" in ma.args

    def test_memory_analyzer_has_timeout_for_npx_download(self, project_root_with_tda):
        # npx 首次下载 + jvm-heap-dump-mcp 后台拉 MAT 库, 默认 10s 会握手超时; 工厂须给足预算。
        ma = _cfg_map(create_java_jvm_mcp_manager(project_root=project_root_with_tda))["memory-analyzer"]
        assert ma.timeout >= 30.0


class TestMissingJar:
    def test_missing_jar_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            create_java_jvm_mcp_manager(project_root=tmp_path)


class TestComposeAgent:
    def test_attaches_dedicated_mcp_manager(self, tmp_path):
        # compose 入口只注入配置, 不启动 manager, 无需避免真实 start。
        from diagnose.platform_impl.java_jvm import configure_java_jvm_diagnosis_agent
        from diagnose.agent import configure_diagnosis_agent  # noqa: F401
        from core.agent_loop import AgentConfig
        from diagnose.api import create_diagnosis_session
        from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
        from diagnose.platform_impl import builtin_platform_registry

        (tmp_path / "mcp-assets/tda").mkdir(parents=True)
        (tmp_path / "mcp-assets/tda/tda-3.2.jar").write_bytes(b"PK")
        (tmp_path / "td.txt").write_text("x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
        session = create_diagnosis_session(case, builtin_platform_registry())

        base = AgentConfig(provider=_FakeProvider(), system="diag", model="m", max_tokens=1)
        out = configure_java_jvm_diagnosis_agent(base, session, project_root=tmp_path)
        assert out.mcp_manager is not None
        # 控制工具被追加
        assert any(t.name == "GetDiagnosisContext" for t in out.tools)
        # system 不变
        assert out.system == "diag"

    def test_rejects_existing_mcp_manager(self, tmp_path):
        from diagnose.platform_impl.java_jvm import configure_java_jvm_diagnosis_agent
        from core.agent_loop import AgentConfig
        from core.mcp import MCPManager
        from diagnose.api import create_diagnosis_session
        from diagnose.model import ArtifactKind, ArtifactRef, DiagnosisCase
        from diagnose.platform_impl import builtin_platform_registry

        (tmp_path / "mcp-assets/tda").mkdir(parents=True)
        (tmp_path / "mcp-assets/tda/tda-3.2.jar").write_bytes(b"PK")
        (tmp_path / "td.txt").write_text("x")
        case = DiagnosisCase(id="c", platform_id="java-jvm", root_dir=str(tmp_path),
                             artifacts=[ArtifactRef(id="td", kind=ArtifactKind.THREAD_SNAPSHOT, path="td.txt")])
        session = create_diagnosis_session(case, builtin_platform_registry())
        base = AgentConfig(provider=_FakeProvider(), system="diag", model="m", max_tokens=1,
                           mcp_manager=MCPManager([]))
        import pytest
        with pytest.raises(ValueError):
            configure_java_jvm_diagnosis_agent(base, session, project_root=tmp_path)
