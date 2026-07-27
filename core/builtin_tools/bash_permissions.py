"""Bash 权限策略。

对齐 CCB 的核心分工: BashTool 只负责执行;是否允许执行交给权限层判断。
本项目面向全链路 debug loop,第一版不用交互式 ask,而是三档:

- allow: debug loop 可自动跑的只读/本地验证命令;
- deny: 明确危险、不可自动执行的命令;
- escalate: 最终合入/发布闸门或无法静态判断的命令,停止自动链路并升级人工。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import shlex
from typing import Any


class BashPermissionAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class BashPermissionDecision:
    action: BashPermissionAction
    reason: str


_SHELL_CONTROL_PATTERNS = (
    r"\$\(",
    r"`",
    r"\|",
    r"\|\|",
    r"&&",
    r";",
    r"\n",
    r">",
    r"<",
)

_DANGEROUS_PATTERNS = (
    r"\brm\s+-[^\n]*r[^\n]*f\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+push\b[^\n]*(--force|--force-with-lease|\s-f\b|--delete|\s-d\b|--mirror)\b",
    r"\bsudo\b",
    r"\bchmod\s+-R\s+777\b",
    r"\bchown\s+-R\b",
    r"\bdd\s+",
    r":\(\)\s*\{",
)

_PIPE_TO_SHELL_PATTERN = r"\b(curl|wget)\b[^\n]*\|\s*(sh|bash)\b"
_PROTECTED_BRANCHES = {"main", "master", "develop"}
_DENIED_GIT_PUSH_FLAGS = {
    "-f",
    "--force",
    "--force-with-lease",
    "--delete",
    "-d",
    "--mirror",
}


def _tokenize(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _starts_with(tokens: list[str], *prefix: str) -> bool:
    return len(tokens) >= len(prefix) and tuple(tokens[: len(prefix)]) == prefix


def _is_git_push(tokens: list[str]) -> bool:
    return _starts_with(tokens, "git", "push")


def _strip_ref_prefix(ref: str) -> str:
    for prefix in ("refs/heads/", "origin/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _is_protected_push_ref(ref: str) -> bool:
    branch = _strip_ref_prefix(ref)
    if ":" in branch:
        branch = _strip_ref_prefix(branch.rsplit(":", 1)[1])
    return branch in _PROTECTED_BRANCHES


def _git_push_has_denied_flag(tokens: list[str]) -> bool:
    return any(token in _DENIED_GIT_PUSH_FLAGS for token in tokens[2:])


def _git_push_refspecs(tokens: list[str]) -> list[str]:
    """提取 git push 的目标分支/引用。

    第一版只处理阿里云 Loop 里需要的普通形状:git push origin <branch>
    以及 git push -u/--set-upstream origin <branch>。无法判断的 push 留给
    _is_escalated_git 升级人工。
    """
    refs: list[str] = []
    positional: list[str] = []
    skip_next = False
    options_with_value = {"--repo", "--receive-pack", "--exec"}
    for token in tokens[2:]:
        if skip_next:
            skip_next = False
            continue
        if token in options_with_value:
            skip_next = True
            continue
        if token.startswith("--repo=") or token.startswith("--receive-pack="):
            continue
        if token.startswith("-"):
            continue
        positional.append(token)

    # git push origin feature/foo -> positional = ["origin", "feature/foo"]
    if len(positional) >= 2:
        refs.extend(positional[1:])
    return refs


def _is_allowed_git_push(tokens: list[str]) -> bool:
    if not _is_git_push(tokens) or _git_push_has_denied_flag(tokens):
        return False
    refs = _git_push_refspecs(tokens)
    if not refs:
        return False
    return not any(_is_protected_push_ref(ref) for ref in refs)


def _is_allowed_git(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "git":
        return False
    if len(tokens) == 1:
        return False
    sub = tokens[1]
    if sub == "push":
        return _is_allowed_git_push(tokens)
    if sub in {"status", "diff", "log", "show", "blame"}:
        return True
    if sub == "branch" and (
        len(tokens) == 2 or "--show-current" in tokens or "-a" in tokens
    ):
        return True
    if sub == "rev-parse":
        return True
    return sub in {"ls-files", "grep"}


def _is_escalated_git(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "git" or len(tokens) == 1:
        return False
    if tokens[1] == "push":
        return True
    return tokens[1] in {
        "pull",
        "fetch",
        "merge",
        "rebase",
        "commit",
        "checkout",
        "switch",
        "tag",
        "cherry-pick",
        "stash",
    }


def _is_allowed_python(tokens: list[str]) -> bool:
    if not tokens or tokens[0] not in {"python", "python3", "pytest"}:
        return False
    if tokens[0] == "pytest":
        return True
    return _starts_with(tokens, tokens[0], "-m", "pytest") or _starts_with(
        tokens, tokens[0], "-m", "unittest"
    )


def _is_escalated_python(tokens: list[str]) -> bool:
    if not tokens or tokens[0] not in {"python", "python3"}:
        return False
    # 运行任意脚本可能修改环境/数据;debug loop 第一版先升级人工/上层策略。
    return not _is_allowed_python(tokens)


def _is_allowed_read_command(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if tokens[0] == "find":
        # find 默认是读/搜索;但这些 action 会删除文件或执行任意命令。
        return not any(
            arg in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
            for arg in tokens[1:]
        )
    return tokens[0] in {
        "pwd",
        "ls",
        "rg",
        "grep",
        "cat",
        "head",
        "tail",
        "wc",
        "tree",
        "du",
        "env",
        "which",
    }


def _is_release_gate(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if tokens[0] in {"kubectl", "helm", "terraform", "gh"}:
        return True
    joined = " ".join(tokens).lower()
    return any(word in joined for word in ("deploy", "release", "rollback", "prod"))


def _load_bash_parser() -> Any | None:
    """按需加载 tree-sitter bash parser;依赖不可用时返回 None。

    权限层必须 fail-closed:tree-sitter 是增强能力,不是运行前提。没有 parser 时
    继续走下面的保守字符串规则,避免因为环境缺包而放宽 Bash 权限。
    """
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_bash
    except ImportError:
        return None

    language = Language(tree_sitter_bash.language())
    try:
        return Parser(language)
    except TypeError:
        parser = Parser()
        parser.language = language
        return parser


def _node_text(source: bytes, node: Any) -> str:
    return source[node.start_byte:node.end_byte].decode()


def _all_named_children(node: Any) -> list[Any]:
    return [child for child in node.children if child.is_named]


def _node_has_error(node: Any) -> bool:
    if getattr(node, "has_error", False):
        return True
    return node.type in {"ERROR", "MISSING"}


def _is_allowed_simple_command(command: str) -> bool:
    tokens = _tokenize(command)
    if tokens is None:
        return False
    if _is_allowed_git(tokens) or _is_allowed_python(tokens):
        return True
    return _is_allowed_read_command(tokens)


def _classify_ast_node(source: bytes, node: Any) -> BashPermissionAction | None:
    """用 tree-sitter 判断安全组合命令。

    只做第一版最有价值的白名单:多个"简单只读/本地测试命令"通过 pipe 或 &&
    串起来时允许自动执行。其他 shell 结构返回 None,由外层升级人工或回退旧规则。
    """
    if _node_has_error(node):
        return None
    if node.type == "program":
        named = _all_named_children(node)
        if len(named) != 1:
            return None
        return _classify_ast_node(source, named[0])
    if node.type == "command":
        return (
            BashPermissionAction.ALLOW
            if _is_allowed_simple_command(_node_text(source, node))
            else None
        )
    if node.type == "pipeline":
        commands = [child for child in node.children if child.type == "command"]
        if len(commands) < 2:
            return None
        if all(
            _is_allowed_simple_command(_node_text(source, child))
            for child in commands
        ):
            return BashPermissionAction.ALLOW
        return None
    if node.type == "list":
        operators = [
            _node_text(source, child)
            for child in node.children
            if not child.is_named and _node_text(source, child).strip()
        ]
        commands = [child for child in node.children if child.type == "command"]
        if not commands or any(op != "&&" for op in operators):
            return None
        if all(
            _is_allowed_simple_command(_node_text(source, child))
            for child in commands
        ):
            return BashPermissionAction.ALLOW
        return None
    return None


def _classify_with_tree_sitter(command: str) -> BashPermissionDecision | None:
    parser = _load_bash_parser()
    if parser is None:
        return None

    source = command.encode()
    tree = parser.parse(source)
    action = _classify_ast_node(source, tree.root_node)
    if action is BashPermissionAction.ALLOW:
        return BashPermissionDecision(
            BashPermissionAction.ALLOW,
            "允许执行: tree-sitter 确认组合命令只包含只读诊断/本地测试子命令。",
        )
    return None


def classify_bash_command(command: str) -> BashPermissionDecision:
    """把 Bash 命令分成 allow / deny / escalate。

    这不是完整 shell 安全解析器。和 CCB tree-sitter 路径不同,本版遇到复杂结构
    直接升级人工,避免把"看不懂"误判成"安全"。
    """
    stripped = command.strip()
    if not stripped:
        return BashPermissionDecision(BashPermissionAction.DENY, "空 Bash 命令禁止执行。")

    if re.search(_PIPE_TO_SHELL_PATTERN, stripped):
        return BashPermissionDecision(
            BashPermissionAction.DENY,
            "禁止执行: 下载内容直接管道给 shell 属于高危命令。",
        )
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, stripped):
            return BashPermissionDecision(
                BashPermissionAction.DENY,
                "禁止执行: 命中明确危险 Bash 命令规则。",
            )

    ast_decision = _classify_with_tree_sitter(stripped)
    if ast_decision is not None:
        return ast_decision

    for pattern in _SHELL_CONTROL_PATTERNS:
        if re.search(pattern, stripped):
            return BashPermissionDecision(
                BashPermissionAction.ESCALATE,
                "升级人工: Bash 命令包含复杂 shell 控制结构,当前策略不自动执行。",
            )

    tokens = _tokenize(stripped)
    if tokens is None:
        return BashPermissionDecision(
            BashPermissionAction.ESCALATE,
            "升级人工: Bash 命令无法被安全解析。",
        )

    if _is_allowed_git(tokens):
        return BashPermissionDecision(
            BashPermissionAction.ALLOW,
            "允许执行: Git 只读诊断命令。",
        )
    if _is_escalated_git(tokens):
        return BashPermissionDecision(
            BashPermissionAction.ESCALATE,
            "升级人工: Git 分支/提交/远程操作属于合入或发布闸门。",
        )
    if _is_allowed_python(tokens):
        return BashPermissionDecision(
            BashPermissionAction.ALLOW,
            "允许执行: 本地测试命令。",
        )
    if _is_escalated_python(tokens):
        return BashPermissionDecision(
            BashPermissionAction.ESCALATE,
            "升级人工: 运行任意 Python 脚本可能产生副作用。",
        )
    if _is_allowed_read_command(tokens):
        return BashPermissionDecision(
            BashPermissionAction.ALLOW,
            "允许执行: 只读诊断命令。",
        )
    if _is_release_gate(tokens):
        return BashPermissionDecision(
            BashPermissionAction.ESCALATE,
            "升级人工: 部署、发布或远程资源操作必须经过人工闸门。",
        )

    return BashPermissionDecision(
        BashPermissionAction.ESCALATE,
        "升级人工: 未知 Bash 命令不在 debug loop 自动执行白名单内。",
    )
