"""Progressive loader for verification test-generation SOP Skills.

Discovery reads only ``SKILL.md`` frontmatter plus the explicit scenario routing
metadata in ``selection.yaml``.  The Skill body and supporting resources are
snapshotted only after a planning Agent has selected the Skill.  These generation
Skills are deliberately separate from ``VerificationSkillLoader``: the latter
loads executable ``verification.yaml`` case packs for the trusted hard gate.
"""

from __future__ import annotations

import fnmatch
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
from typing import Iterable, Literal, Self

from pydantic import Field, field_validator, model_validator
import yaml

from .models import VerificationModel


_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_METADATA_BYTES = 128 * 1024
MAX_GENERATION_SKILL_FILES = 1_024
MAX_GENERATION_SKILL_BYTES = 32 * 1024 * 1024


class VerificationGenerationSkillError(ValueError):
    """A generation Skill cannot be discovered or frozen safely."""


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


class GenerationSkillMatch(VerificationModel):
    matched_rules: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    require_any: tuple[Literal["matched_rule", "changed_path", "risk_tag"], ...] = ()

    @field_validator("matched_rules", "changed_paths", "risk_tags", "require_any")
    @classmethod
    def _non_empty_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError("selection match values must be non-empty and unique")
        return value


def _expand_braces(pattern: str) -> tuple[str, ...]:
    """Expand bounded ``{a,b}`` groups used by routing globs."""

    expanded = (pattern,)
    while any("{" in item or "}" in item for item in expanded):
        next_patterns: list[str] = []
        for item in expanded:
            start = item.find("{")
            if start < 0:
                if "}" in item:
                    raise VerificationGenerationSkillError(
                        f"invalid changed_paths glob: {pattern}"
                    )
                next_patterns.append(item)
                continue
            end = item.find("}", start + 1)
            if end < 0 or "{" in item[start + 1 : end]:
                raise VerificationGenerationSkillError(
                    f"invalid changed_paths glob: {pattern}"
                )
            choices = item[start + 1 : end].split(",")
            if any(not choice for choice in choices):
                raise VerificationGenerationSkillError(
                    f"invalid changed_paths glob: {pattern}"
                )
            next_patterns.extend(
                item[:start] + choice + item[end + 1 :] for choice in choices
            )
            if len(next_patterns) > 64:
                raise VerificationGenerationSkillError(
                    f"changed_paths glob expands too broadly: {pattern}"
                )
        expanded = tuple(next_patterns)
    return expanded


def _glob_path_regex(pattern: str) -> re.Pattern[str]:
    parts = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if pattern[index : index + 2] == "**":
                if pattern[index + 2 : index + 3] == "/":
                    parts.append("(?:.*/)?")
                    index += 3
                else:
                    parts.append(".*")
                    index += 2
            else:
                parts.append("[^/]*")
                index += 1
        elif character == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(character))
            index += 1
    parts.append("$")
    return re.compile("".join(parts))


def _path_pattern_matches(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        _glob_path_regex(expanded).fullmatch(normalized) is not None
        for expanded in _expand_braces(pattern)
    )


class GenerationSkillScenario(VerificationModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    when: GenerationSkillMatch
    selection_prompt: str = Field(min_length=1, max_length=4_096)
    exclusions: tuple[str, ...] = ()

    @field_validator("exclusions")
    @classmethod
    def _valid_exclusions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError("selection exclusions must be non-empty and unique")
        return value


class GenerationSkillSelectionSpec(VerificationModel):
    name: str
    scenarios: tuple[GenerationSkillScenario, ...] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _SKILL_NAME.fullmatch(value) or not value.startswith("verification-"):
            raise ValueError("generation Skill name must use verification-<type>")
        return value

    @model_validator(mode="after")
    def _unique_scenarios(self) -> Self:
        ids = [item.id for item in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("generation Skill scenario ids must be unique")
        return self


class GenerationSkillAdvertisement(VerificationModel):
    """The only generation Skill data visible during model selection."""

    name: str
    description: str = Field(min_length=1, max_length=2_048)
    scenarios: tuple[GenerationSkillScenario, ...] = Field(min_length=1)
    metadata_digest: str = Field(pattern=_SHA256.pattern)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _SKILL_NAME.fullmatch(value) or not value.startswith("verification-"):
            raise ValueError("generation Skill name must use verification-<type>")
        return value


class GenerationSkillChoice(VerificationModel):
    skill_name: str
    scenario_ids: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2_048)

    @field_validator("skill_name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _SKILL_NAME.fullmatch(value) or not value.startswith("verification-"):
            raise ValueError("selected generation Skill name is invalid")
        return value

    @field_validator("scenario_ids")
    @classmethod
    def _unique_scenario_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("selected scenario ids must be unique")
        return value


class GenerationSkillSelection(VerificationModel):
    choices: tuple[GenerationSkillChoice, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_skills(self) -> Self:
        names = [item.skill_name for item in self.choices]
        if len(names) != len(set(names)):
            raise ValueError("generation Skill choices must be unique")
        return self

    @property
    def skill_names(self) -> tuple[str, ...]:
        return tuple(item.skill_name for item in self.choices)


class ResolvedGenerationSkill(VerificationModel):
    name: str
    description: str
    scenarios: tuple[GenerationSkillScenario, ...]
    instructions: str = Field(min_length=1)
    digest: str = Field(pattern=_SHA256.pattern)
    metadata_digest: str = Field(pattern=_SHA256.pattern)
    directory: str = Field(min_length=1)
    resource_paths: tuple[str, ...] = ()
    supporting_instructions: dict[str, str] = Field(default_factory=dict)


def _safe_yaml(raw: bytes, *, label: str) -> object:
    try:
        return yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise VerificationGenerationSkillError(f"{label} is invalid YAML: {exc}") from exc


def _frontmatter_from_prefix(path: Path) -> tuple[dict, bytes]:
    """Read through the closing frontmatter delimiter, never the Skill body."""

    collected = bytearray()
    try:
        with path.open("rb") as handle:
            first = handle.readline(MAX_METADATA_BYTES + 1)
            collected.extend(first)
            if first.rstrip(b"\r\n") != b"---":
                raise VerificationGenerationSkillError(
                    f"generation Skill requires YAML frontmatter: {path}"
                )
            while len(collected) <= MAX_METADATA_BYTES:
                line = handle.readline(MAX_METADATA_BYTES - len(collected) + 1)
                if not line:
                    break
                collected.extend(line)
                if line.rstrip(b"\r\n") == b"---":
                    break
    except OSError as exc:
        raise VerificationGenerationSkillError(
            f"cannot read generation Skill metadata {path}: {exc}"
        ) from exc
    if len(collected) > MAX_METADATA_BYTES:
        raise VerificationGenerationSkillError("generation Skill frontmatter is too large")
    lines = bytes(collected).splitlines()
    if len(lines) < 3 or lines[-1].strip() != b"---":
        raise VerificationGenerationSkillError(
            f"generation Skill frontmatter is not closed: {path}"
        )
    parsed = _safe_yaml(b"\n".join(lines[1:-1]), label=str(path))
    if not isinstance(parsed, dict):
        raise VerificationGenerationSkillError(
            f"generation Skill frontmatter must be an object: {path}"
        )
    return parsed, bytes(collected)


def _parse_advertisement(directory: Path) -> GenerationSkillAdvertisement:
    skill_path = directory / "SKILL.md"
    selection_path = directory / "selection.yaml"
    for path in (skill_path, selection_path):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise VerificationGenerationSkillError(
                f"missing generation Skill metadata file: {path}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise VerificationGenerationSkillError(
                f"generation Skill metadata must be a regular file: {path}"
            )
        if metadata.st_size > MAX_METADATA_BYTES:
            raise VerificationGenerationSkillError(
                f"generation Skill metadata file is too large: {path}"
            )

    frontmatter, frontmatter_bytes = _frontmatter_from_prefix(skill_path)
    if set(frontmatter) != {"name", "description"}:
        raise VerificationGenerationSkillError(
            f"{skill_path} frontmatter must contain only name and description"
        )
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        raise VerificationGenerationSkillError(
            f"{skill_path} name and description must be strings"
        )
    try:
        raw_selection = selection_path.read_bytes()
    except OSError as exc:
        raise VerificationGenerationSkillError(
            f"cannot read generation Skill selection metadata {selection_path}: {exc}"
        ) from exc
    selection = _safe_yaml(raw_selection, label=str(selection_path))
    if not isinstance(selection, dict):
        raise VerificationGenerationSkillError(
            f"{selection_path} must contain an object"
        )
    try:
        spec = GenerationSkillSelectionSpec.model_validate(selection)
    except Exception as exc:
        raise VerificationGenerationSkillError(
            f"{selection_path} does not match the selection schema: {exc}"
        ) from exc
    if name != directory.name or spec.name != directory.name:
        raise VerificationGenerationSkillError(
            f"generation Skill name mismatch for {directory}"
        )
    metadata_digest = sha256(
        frontmatter_bytes
        + b"\x00selection.yaml\x00"
        + raw_selection
    ).hexdigest()
    try:
        return GenerationSkillAdvertisement(
            name=name,
            description=description,
            scenarios=spec.scenarios,
            metadata_digest=metadata_digest,
        )
    except Exception as exc:
        raise VerificationGenerationSkillError(
            f"generation Skill advertisement is invalid for {directory}: {exc}"
        ) from exc


def _snapshot_directory(directory: Path) -> tuple[dict[str, bytes], str]:
    files: dict[str, bytes] = {}
    manifest: list[dict[str, str | int]] = []
    total_bytes = 0
    for path in sorted(directory.rglob("*")):
        try:
            before = path.lstat()
        except OSError as exc:
            raise VerificationGenerationSkillError(
                f"cannot inspect generation Skill file {path}: {exc}"
            ) from exc
        if stat.S_ISLNK(before.st_mode):
            raise VerificationGenerationSkillError(
                f"generation Skill cannot contain symlinks: {path}"
            )
        if stat.S_ISDIR(before.st_mode):
            continue
        if not stat.S_ISREG(before.st_mode):
            raise VerificationGenerationSkillError(
                f"generation Skill can contain only regular files: {path}"
            )
        try:
            content = path.read_bytes()
            after = path.lstat()
        except OSError as exc:
            raise VerificationGenerationSkillError(
                f"cannot read generation Skill file {path}: {exc}"
            ) from exc
        before_state = (before.st_mode, before.st_size, before.st_mtime_ns, before.st_ino)
        after_state = (after.st_mode, after.st_size, after.st_mtime_ns, after.st_ino)
        if before_state != after_state:
            raise VerificationGenerationSkillError(
                f"generation Skill changed while being read: {path}"
            )
        relative = path.relative_to(directory).as_posix()
        files[relative] = content
        total_bytes += len(content)
        if (
            len(files) > MAX_GENERATION_SKILL_FILES
            or total_bytes > MAX_GENERATION_SKILL_BYTES
        ):
            raise VerificationGenerationSkillError(
                "generation Skill file count or total size exceeds the limit"
            )
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


def _validate_provenance(
    raw: bytes,
    *,
    name: str,
    files: dict[str, bytes],
) -> None:
    provenance = _safe_yaml(raw, label=f"{name}/provenance.yaml")
    if not isinstance(provenance, dict):
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance.yaml must be an object"
        )
    declared_name = provenance.get("name")
    if declared_name is not None and declared_name != name:
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance name does not match"
        )
    source = provenance.get("upstream", provenance)
    if not isinstance(source, dict):
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance upstream must be an object"
        )
    required = {"repository", "commit", "source_path", "license"}
    missing = sorted(required - set(source))
    if missing:
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance is missing: {', '.join(missing)}"
        )
    if not isinstance(source["repository"], str) or not source[
        "repository"
    ].startswith("https://"):
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance repository must be HTTPS"
        )
    if not isinstance(source["commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", source["commit"]
    ):
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance commit must be a full Git SHA"
        )
    source_path = source["source_path"]
    if (
        not isinstance(source_path, str)
        or not source_path
        or Path(source_path).is_absolute()
        or ".." in Path(source_path).parts
    ):
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance source_path must be relative"
        )
    if not isinstance(source["license"], str) or not source["license"].strip():
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance license must be non-empty"
        )
    vendored = provenance.get("vendored", [])
    if not isinstance(vendored, list):
        raise VerificationGenerationSkillError(
            f"generation Skill {name} provenance vendored must be a list"
        )
    destinations: set[str] = set()
    for index, item in enumerate(vendored):
        if not isinstance(item, dict):
            raise VerificationGenerationSkillError(
                f"generation Skill {name} vendored[{index}] must be an object"
            )
        destination = item.get("destination_path")
        expected_digest = item.get("sha256")
        if (
            not isinstance(destination, str)
            or not destination
            or Path(destination).is_absolute()
            or ".." in Path(destination).parts
        ):
            raise VerificationGenerationSkillError(
                f"generation Skill {name} has an unsafe vendored destination"
            )
        if destination in destinations:
            raise VerificationGenerationSkillError(
                f"generation Skill {name} repeats vendored destination {destination}"
            )
        destinations.add(destination)
        if not isinstance(expected_digest, str) or not _SHA256.fullmatch(
            expected_digest
        ):
            raise VerificationGenerationSkillError(
                f"generation Skill {name} has an invalid vendored SHA-256"
            )
        content = files.get(destination)
        if content is None:
            raise VerificationGenerationSkillError(
                f"generation Skill {name} vendored file is missing: {destination}"
            )
        if sha256(content).hexdigest() != expected_digest:
            raise VerificationGenerationSkillError(
                f"generation Skill {name} vendored digest mismatch: {destination}"
            )


class VerificationGenerationSkillCatalog:
    """Discover descriptions cheaply and freeze full selected Skills on demand."""

    def __init__(self, roots: Iterable[str | Path]):
        try:
            resolved = tuple(Path(root).resolve() for root in roots)
        except (OSError, RuntimeError) as exc:
            raise VerificationGenerationSkillError(
                f"cannot resolve generation Skill roots: {exc}"
            ) from exc
        if not resolved:
            raise VerificationGenerationSkillError(
                "at least one generation Skill root is required"
            )
        self.roots = resolved

    def _directories(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        duplicates: dict[str, list[Path]] = {}
        for root in self.roots:
            if not root.is_dir():
                raise VerificationGenerationSkillError(
                    f"generation Skill root does not exist: {root}"
                )
            for child in sorted(root.iterdir()):
                if child.is_symlink():
                    raise VerificationGenerationSkillError(
                        f"generation Skill directory cannot be a symlink: {child}"
                    )
                if not child.is_dir() or not (child / "SKILL.md").exists():
                    continue
                if child.name in found:
                    duplicates.setdefault(child.name, [found[child.name]]).append(child)
                else:
                    found[child.name] = child
        if duplicates:
            details = "; ".join(
                f"{name}: {', '.join(str(path) for path in paths)}"
                for name, paths in sorted(duplicates.items())
            )
            raise VerificationGenerationSkillError(
                "duplicate generation Skill names: " + details
            )
        return found

    def discover(self) -> tuple[GenerationSkillAdvertisement, ...]:
        advertisements = tuple(
            _parse_advertisement(directory)
            for _, directory in sorted(self._directories().items())
        )
        if not advertisements:
            raise VerificationGenerationSkillError(
                "no verification generation Skills were discovered"
            )
        return advertisements

    def load_selected(
        self,
        names: tuple[str, ...],
        *,
        expected_metadata_digests: dict[str, str] | None = None,
    ) -> tuple[ResolvedGenerationSkill, ...]:
        if not names or len(names) != len(set(names)):
            raise VerificationGenerationSkillError(
                "selected generation Skill names must be non-empty and unique"
            )
        directories = self._directories()
        resolved: list[ResolvedGenerationSkill] = []
        for name in names:
            directory = directories.get(name)
            if directory is None:
                raise VerificationGenerationSkillError(
                    f"unknown verification generation Skill: {name}"
                )
            advertisement = _parse_advertisement(directory)
            if expected_metadata_digests is not None:
                expected = expected_metadata_digests.get(name)
                if expected != advertisement.metadata_digest:
                    raise VerificationGenerationSkillError(
                        f"generation Skill metadata changed after selection: {name}"
                    )
            files, digest = _snapshot_directory(directory)
            confirmed_files, confirmed_digest = _snapshot_directory(directory)
            if files != confirmed_files or digest != confirmed_digest:
                raise VerificationGenerationSkillError(
                    f"generation Skill changed while being frozen: {name}"
                )
            missing = [
                filename
                for filename in ("SKILL.md", "selection.yaml", "provenance.yaml")
                if filename not in files
            ]
            if missing:
                raise VerificationGenerationSkillError(
                    f"generation Skill {name} is missing: {', '.join(missing)}"
                )
            _validate_provenance(
                files["provenance.yaml"],
                name=name,
                files=files,
            )
            try:
                instructions = files["SKILL.md"].decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise VerificationGenerationSkillError(
                    f"generation Skill {name} SKILL.md is not UTF-8"
                ) from exc
            if not instructions:
                raise VerificationGenerationSkillError(
                    f"generation Skill {name} SKILL.md is empty"
                )
            resource_paths = tuple(
                path
                for path in sorted(files)
                if path not in {"SKILL.md", "selection.yaml", "provenance.yaml"}
            )
            supporting_instructions: dict[str, str] = {}
            for path in resource_paths:
                resource = Path(path)
                if (
                    not path.startswith("references/")
                    or path.startswith("references/upstream/")
                    or resource.suffix.lower() not in {".json", ".md", ".yaml", ".yml"}
                ):
                    continue
                try:
                    supporting_instructions[path] = files[path].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise VerificationGenerationSkillError(
                        f"generation Skill trusted reference is not UTF-8: {name}/{path}"
                    ) from exc
            resolved.append(
                ResolvedGenerationSkill(
                    name=name,
                    description=advertisement.description,
                    scenarios=advertisement.scenarios,
                    instructions=instructions,
                    digest=digest,
                    metadata_digest=advertisement.metadata_digest,
                    directory=str(directory.resolve()),
                    resource_paths=resource_paths,
                    supporting_instructions=supporting_instructions,
                )
            )
        return tuple(resolved)


def render_generation_skill_catalog(
    advertisements: tuple[GenerationSkillAdvertisement, ...],
) -> str:
    """Render description and scenario prompts without any Skill body text."""

    sections: list[str] = []
    for skill in advertisements:
        scenarios = "\n".join(
            "\n".join(
                (
                    f"  - scenario_id: {scenario.id}",
                    f"    selection_prompt: {' '.join(scenario.selection_prompt.split())}",
                    "    matched_rules: "
                    + json.dumps(scenario.when.matched_rules, ensure_ascii=False),
                    "    changed_paths: "
                    + json.dumps(scenario.when.changed_paths, ensure_ascii=False),
                    "    risk_tags: "
                    + json.dumps(scenario.when.risk_tags, ensure_ascii=False),
                    "    require_any: "
                    + json.dumps(scenario.when.require_any, ensure_ascii=False),
                    "    exclusions: "
                    + json.dumps(scenario.exclusions, ensure_ascii=False),
                )
            )
            for scenario in skill.scenarios
        )
        sections.append(
            f"- name: {skill.name}\n"
            f"  description: {' '.join(skill.description.split())}\n"
            f"  scenarios:\n{scenarios}"
        )
    return "\n".join(sections)


def validate_generation_skill_selection(
    selection: GenerationSkillSelection,
    advertisements: tuple[GenerationSkillAdvertisement, ...],
    *,
    matched_rule: str | None = None,
    changed_paths: tuple[str, ...] = (),
    risk_tags: tuple[str, ...] = (),
) -> None:
    available = {item.name: item for item in advertisements}
    for choice in selection.choices:
        skill = available.get(choice.skill_name)
        if skill is None:
            raise VerificationGenerationSkillError(
                f"selection contains unknown generation Skill: {choice.skill_name}"
            )
        valid_ids = {item.id for item in skill.scenarios}
        unknown = sorted(set(choice.scenario_ids) - valid_ids)
        if unknown:
            raise VerificationGenerationSkillError(
                f"selection contains unknown scenarios for {choice.skill_name}: "
                + ", ".join(unknown)
            )
        if matched_rule is None and not changed_paths and not risk_tags:
            continue
        scenarios = {item.id: item for item in skill.scenarios}
        for scenario_id in choice.scenario_ids:
            routing = scenarios[scenario_id].when
            signal_matches = {
                "matched_rule": (
                    matched_rule is not None
                    and any(
                        fnmatch.fnmatchcase(matched_rule, pattern)
                        for pattern in routing.matched_rules
                    )
                ),
                "changed_path": any(
                    _path_pattern_matches(path, pattern)
                    for path in changed_paths
                    for pattern in routing.changed_paths
                ),
                "risk_tag": bool(set(risk_tags) & set(routing.risk_tags)),
            }
            has_conditions = any(
                (routing.matched_rules, routing.changed_paths, routing.risk_tags)
            )
            matches = not has_conditions or any(signal_matches.values())
            if not matches:
                raise VerificationGenerationSkillError(
                    f"selected scenario does not match incident routing metadata: "
                    f"{choice.skill_name}:{scenario_id}"
                )
            if routing.require_any and not any(
                signal_matches[signal] for signal in routing.require_any
            ):
                raise VerificationGenerationSkillError(
                    "selected scenario lacks a required routing signal: "
                    f"{choice.skill_name}:{scenario_id} requires one of "
                    + ", ".join(routing.require_any)
                )
    if selection.skill_names == ("verification-property-oracle",):
        raise VerificationGenerationSkillError(
            "verification-property-oracle is an enhancement and requires a primary Skill"
        )


__all__ = [
    "GenerationSkillAdvertisement",
    "GenerationSkillChoice",
    "GenerationSkillMatch",
    "GenerationSkillScenario",
    "GenerationSkillSelection",
    "GenerationSkillSelectionSpec",
    "ResolvedGenerationSkill",
    "VerificationGenerationSkillCatalog",
    "VerificationGenerationSkillError",
    "render_generation_skill_catalog",
    "validate_generation_skill_selection",
]
