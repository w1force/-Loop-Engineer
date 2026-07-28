"""异步 LSP 诊断注册表。
publishDiagnostics 到达时先登记；主 Agent 下一次请求模型前统一取出、去重和限流。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import json
import time
from typing import Literal
from urllib.parse import unquote, urlparse
from uuid import uuid4

DiagnosticSeverity = Literal["Error", "Warning", "Info", "Hint"]

MAX_DIAGNOSTICS_PER_FILE = 10
MAX_TOTAL_DIAGNOSTICS = 30
MAX_DELIVERED_FILES = 500


@dataclass(frozen=True)
class DiagnosticPosition:
    line: int
    character: int


@dataclass(frozen=True)
class DiagnosticRange:
    start: DiagnosticPosition
    end: DiagnosticPosition


@dataclass(frozen=True)
class Diagnostic:
    message: str
    severity: DiagnosticSeverity
    range: DiagnosticRange
    source: str | None = None
    code: str | None = None


@dataclass
class DiagnosticFile:
    uri: str
    diagnostics: list[Diagnostic]


@dataclass
class PendingLSPDiagnostic:
    server_name: str
    files: list[DiagnosticFile]
    timestamp: float
    attachment_sent: bool = False


@dataclass
class LSPDiagnosticSet:
    server_name: str
    files: list[DiagnosticFile]


_pending_diagnostics: dict[str, PendingLSPDiagnostic] = {}
_delivered_diagnostics: OrderedDict[str, set[str]] = OrderedDict()


def normalize_diagnostic_uri(uri: str) -> str:
    """把 file URI 和本地路径归一成同一个注册表 key。"""
    if not uri.startswith("file://"):
        return uri
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"
    return path


def register_pending_lsp_diagnostic(
    server_name: str, files: list[DiagnosticFile]
) -> None:
    _pending_diagnostics[str(uuid4())] = PendingLSPDiagnostic(
        server_name=server_name,
        files=files,
        timestamp=time.time(),
    )


def _severity_number(severity: DiagnosticSeverity) -> int:
    return {"Error": 1, "Warning": 2, "Info": 3, "Hint": 4}[severity]


def _diagnostic_key(diagnostic: Diagnostic) -> str:
    return json.dumps(
        asdict(diagnostic),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _get_delivered(uri: str) -> set[str]:
    delivered = _delivered_diagnostics.get(uri)
    if delivered is None:
        return set()
    _delivered_diagnostics.move_to_end(uri)
    return delivered


def _track_delivered(uri: str, keys: set[str]) -> None:
    delivered = _delivered_diagnostics.setdefault(uri, set())
    delivered.update(keys)
    _delivered_diagnostics.move_to_end(uri)
    while len(_delivered_diagnostics) > MAX_DELIVERED_FILES:
        _delivered_diagnostics.popitem(last=False)


def _deduplicate_files(files: list[DiagnosticFile]) -> list[DiagnosticFile]:
    grouped: dict[str, DiagnosticFile] = {}
    seen: dict[str, set[str]] = {}
    for file in files:
        uri = normalize_diagnostic_uri(file.uri)
        target = grouped.setdefault(uri, DiagnosticFile(uri=uri, diagnostics=[]))
        batch_seen = seen.setdefault(uri, set())
        previously_delivered = _get_delivered(uri)
        for diagnostic in file.diagnostics:
            key = _diagnostic_key(diagnostic)
            if key in batch_seen or key in previously_delivered:
                continue
            batch_seen.add(key)
            target.diagnostics.append(diagnostic)
    return [file for file in grouped.values() if file.diagnostics]


def check_for_lsp_diagnostics() -> list[LSPDiagnosticSet]:
    """取出尚未投递的诊断，并按相应规则去重、排序和限流。"""
    files: list[DiagnosticFile] = []
    server_names: set[str] = set()
    consumed_ids: list[str] = []
    for diagnostic_id, pending in _pending_diagnostics.items():
        if pending.attachment_sent:
            continue
        files.extend(pending.files)
        server_names.add(pending.server_name)
        consumed_ids.append(diagnostic_id)

    if not files:
        return []

    deduplicated = _deduplicate_files(files)
    for diagnostic_id in consumed_ids:
        pending = _pending_diagnostics.get(diagnostic_id)
        if pending is not None:
            pending.attachment_sent = True
        _pending_diagnostics.pop(diagnostic_id, None)

    total = 0
    limited_files: list[DiagnosticFile] = []
    for file in deduplicated:
        ordered = sorted(file.diagnostics, key=lambda item: _severity_number(item.severity))
        ordered = ordered[:MAX_DIAGNOSTICS_PER_FILE]
        remaining = MAX_TOTAL_DIAGNOSTICS - total
        if remaining <= 0:
            break
        ordered = ordered[:remaining]
        if not ordered:
            continue
        limited_files.append(DiagnosticFile(uri=file.uri, diagnostics=ordered))
        total += len(ordered)

    if not limited_files:
        return []

    for file in limited_files:
        _track_delivered(
            file.uri, {_diagnostic_key(diagnostic) for diagnostic in file.diagnostics}
        )

    return [
        LSPDiagnosticSet(
            server_name=", ".join(sorted(server_names)),
            files=limited_files,
        )
    ]


def clear_all_lsp_diagnostics() -> None:
    _pending_diagnostics.clear()


def reset_all_lsp_diagnostic_state() -> None:
    _pending_diagnostics.clear()
    _delivered_diagnostics.clear()


def clear_delivered_diagnostics_for_file(file_uri: str) -> None:
    _delivered_diagnostics.pop(normalize_diagnostic_uri(file_uri), None)
