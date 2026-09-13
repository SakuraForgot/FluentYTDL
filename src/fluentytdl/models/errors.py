"""错误相关的模型。

诊断结果模型（``Diagnosis`` / ``DiagnosticEvent`` / ``RetryPolicy``）已迁移到
:mod:`fluentytdl.diagnostics.models`，那里用稳定的字符串错误码取代了旧的
``ErrorCode`` IntEnum —— 17 个数字枚举无法表达"会员专属 / 年龄限制 / 私人视频"
这类需要不同处置方式的细分场景。

本模块现在只保留子进程异常本身。
"""

from fluentytdl.utils.message_catalog import english


class YtDlpExecutionError(Exception):
    """当 yt-dlp 子进程非正常退出时抛出，携带完整的上下文字段以便后续诊断"""

    def __init__(
        self,
        exit_code: int,
        stderr: str,
        parsed_json: dict | None = None,
        *,
        phase: str = "",
    ):
        super().__init__(english("yt-dlp 执行失败 (退出码: {0})", exit_code))
        self.exit_code = exit_code
        self.stderr = stderr
        self.parsed_json = parsed_json or {}
        #: 失败发生在哪一步：`parse`（连格式都没挑出来）/ `select`（挑完格式但
        #: 一个字节都没下）/ `download`。取值来自 `observability.STAGES` 闭集，
        #: 由 executor 根据"见过哪些输出行"判定；算不出来时留空串。
        #: keyword-only + 默认值 —— 只有 executor 有这个上下文，其余 raise 点不必改。
        self.phase = phase
