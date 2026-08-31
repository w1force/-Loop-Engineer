"""Verification Skill 的 fail-closed 解析器。

通用 SkillLoader 为交互式 Agent 设计，会跳过坏 Skill；硬门禁不能继承这种
fail-open 语义，因此这里仅解析调用方显式给出的 skill_names。
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import stat
from typing import Iterable

import yaml
from pydantic import ValidationError

from .models import ResolvedVerificationSkill, VerificationSkillSpec


class VerificationSkillError(ValueError):
    pass


MAX_SKILL_FILES = 512
MAX_SKILL_BYTES = 8 * 1024 * 1024


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"unhashable mapping key: {key!r}",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _snapshot_directory(directory: Path) -> tuple[dict[str, bytes], str]:
    files: dict[str, bytes] = {}
    manifest: list[dict[str, str | int]] = []
    total_bytes = 0
    for path in sorted(directory.rglob("*")):
        try:
            before = path.lstat()
        except OSError as exc:
            raise VerificationSkillError(f"无法检查 skill 文件 {path}: {exc}") from exc
        if stat.S_ISLNK(before.st_mode):
            raise VerificationSkillError(
                f"verification skill 不允许符号链接: {path}"
            )
        if stat.S_ISDIR(before.st_mode):
            continue
        if not stat.S_ISREG(before.st_mode):
            raise VerificationSkillError(f"verification skill 只允许普通文件: {path}")
        relative = path.relative_to(directory).as_posix()
        try:
            content = path.read_bytes()
            after = path.lstat()
        except OSError as exc:
            raise VerificationSkillError(f"无法读取 skill 文件 {path}: {exc}") from exc
        before_state = (
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ino,
        )
        after_state = (
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        )
        if before_state != after_state:
            raise VerificationSkillError(f"读取期间 skill 文件发生变化: {path}")
        files[relative] = content
        total_bytes += len(content)
        if len(files) > MAX_SKILL_FILES or total_bytes > MAX_SKILL_BYTES:
            raise VerificationSkillError("verification skill 文件数量或总大小超限")
        manifest.append(
            {
                "path": relative,
                "mode": after.st_mode & 0o777,
                "size": len(content),
                "sha256": sha256(content).hexdigest(),
            }
        )

    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return files, sha256(encoded).hexdigest()


class VerificationSkillLoader:
    """从一个或多个受信根目录精确加载指定 Verification Skill。"""

    def __init__(self, roots: Iterable[str | Path]):
        try:
            resolved = tuple(Path(root).resolve() for root in roots)
        except (OSError, RuntimeError) as exc:
            raise VerificationSkillError(
                f"无法解析 verification skill 根目录: {exc}"
            ) from exc
        if not resolved:
            raise VerificationSkillError("至少需要一个 verification skill 根目录")
        self.roots = resolved

    def load_many(self, skill_names: tuple[str, ...]) -> tuple[ResolvedVerificationSkill, ...]:
        if not skill_names:
            raise VerificationSkillError("skill_names 不能为空")
        if len(skill_names) != len(set(skill_names)):
            raise VerificationSkillError("skill_names 不能重复")
        return tuple(self.load(name) for name in skill_names)

    def load(self, name: str) -> ResolvedVerificationSkill:
        candidates: list[Path] = []
        for root in self.roots:
            candidate = root / name
            try:
                resolved_candidate = candidate.resolve()
            except (OSError, RuntimeError) as exc:
                raise VerificationSkillError(
                    f"无法解析 verification skill 路径 {name}: {exc}"
                ) from exc
            try:
                resolved_candidate.relative_to(root)
            except ValueError as exc:
                raise VerificationSkillError(f"skill 路径越界: {name}") from exc
            if candidate.is_dir():
                candidates.append(candidate)

        if not candidates:
            raise VerificationSkillError(f"未知 verification skill: {name}")
        if len(candidates) > 1:
            locations = ", ".join(str(path) for path in candidates)
            raise VerificationSkillError(
                f"verification skill 重名，拒绝覆盖: {name}: {locations}"
            )

        directory = candidates[0]
        if directory.is_symlink():
            raise VerificationSkillError(f"verification skill 目录不能是符号链接: {name}")
        files, digest = _snapshot_directory(directory)
        confirmed_files, confirmed_digest = _snapshot_directory(directory)
        if files != confirmed_files or digest != confirmed_digest:
            raise VerificationSkillError(
                f"verification skill {name} 在冻结期间发生变化"
            )
        missing = [filename for filename in ("SKILL.md", "verification.yaml") if filename not in files]
        if missing:
            raise VerificationSkillError(
                f"verification skill {name} 缺少: {', '.join(missing)}"
            )
        try:
            instructions = files["SKILL.md"].decode("utf-8").strip()
            raw_spec = yaml.load(
                files["verification.yaml"].decode("utf-8"),
                Loader=_UniqueKeyLoader,
            )
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise VerificationSkillError(f"verification skill {name} 无法解析: {exc}") from exc
        if not instructions:
            raise VerificationSkillError(f"verification skill {name} 的 SKILL.md 为空")
        if not isinstance(raw_spec, dict):
            raise VerificationSkillError(
                f"verification skill {name} 的 verification.yaml 必须是对象"
            )
        try:
            spec = VerificationSkillSpec.model_validate(raw_spec)
        except ValidationError as exc:
            raise VerificationSkillError(
                f"verification skill {name} schema 非法: {exc}"
            ) from exc
        if spec.name != name or directory.name != name:
            raise VerificationSkillError(
                f"verification skill 名称不一致: requested={name}, declared={spec.name}"
            )
        return ResolvedVerificationSkill(
            name=name,
            spec=spec,
            instructions=instructions,
            digest=digest,
            directory=str(directory.resolve()),
        )


__all__ = ["VerificationSkillError", "VerificationSkillLoader"]
