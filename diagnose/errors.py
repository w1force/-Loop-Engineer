"""诊断内核领域异常

定义诊断内核在平台注册与查找过程中使用的领域异常层次。
本模块不依赖 core/,仅依赖 Python 标准库。
"""


class DiagnosisError(Exception):
    """诊断内核领域异常基类

    所有诊断内核抛出的领域异常都继承自此基类,
    便于上层调用方用 `except DiagnosisError` 统一捕获。
    """


class UnknownPlatformError(DiagnosisError):
    """未知平台异常

    当通过 platform_id 查找一个未注册的平台时抛出。
    Registry.get() 采用 fail-closed 策略: 抛此异常而非返回 None。
    """


class DuplicatePlatformError(DiagnosisError):
    """重复平台异常

    当向 Registry 注册一个已存在 platform_id 的平台时抛出。
    平台 ID 必须唯一,避免隐式覆盖。
    """


class InvalidEvidenceError(DiagnosisError):
    """证据不合法异常

    当 EvidenceDraft 自身字段不满足完整性约束时抛出:
    - dedup_key 非空
    - platform_id 非空
    - artifact_ids 非空

    Catalog 仅做 draft 自身字段完整性校验,跨 case 的 artifact 存在性
    与 platform_id 一致性校验由 session 层 (Task 4) 负责。
    """


class EvidenceConflictError(DiagnosisError):
    """证据冲突异常

    当一个 dedup_key 已登记,但新 draft 的其余字段内容与已登记记录不一致时抛出,
    防止分析器错误复用 dedup_key 导致语义漂移。
    批量 append 中任一冲突项都会导致整批拒绝 (无部分写入)。
    """


class InvalidArtifactPathError(DiagnosisError):
    """工件路径非法异常

    当 ArtifactRef.path 违反 inspect_case 的路径边界约束时抛出:
    - path 是绝对路径 (脱离 root_dir);
    - path 经规范化后越过 root_dir 边界 (含 .. 越界, 或符号链接逃逸)。

    inspect_case 采用 fail-closed 策略: 越界路径必须显式报错, 不静默忽略,
    避免诊断平台读取到 case.root_dir 之外的敏感文件。
    """


class ArtifactNotFoundError(DiagnosisError):
    """工件文件不存在异常

    当 ArtifactRef.path 指向的文件在 root_dir 内不存在时抛出。
    inspect_case 不静默忽略缺失工件, 而是明确报错, 让调用方知道证据包不完整。
    """


class InvalidReviewError(DiagnosisError):
    """审查结构或审查目标与当前 session 状态不一致。"""


class StaleReviewError(InvalidReviewError):
    """审查绑定的 revision 已不是 session 的当前 revision。"""
