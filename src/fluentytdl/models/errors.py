"""错误相关的模型。

诊断结果模型（``Diagnosis`` / ``DiagnosticEvent`` / ``RetryPolicy``）已迁移到
:mod:`fluentytdl.diagnostics.models`，那里用稳定的字符串错误码取代了旧的
``ErrorCode`` IntEnum —— 17 个数字枚举无法表达"会员专属 / 年龄限制 / 私人视频"
这类需要不同处置方式的细分场景。

本模块现在只保留子进程异常本身。
"""


class YtDlpExecutionError(Exception):
    """当 yt-dlp 子进程非正常退出时抛出，携带完整的上下文字段以便后续诊断"""

    def __init__(self, exit_code: int, stderr: str, parsed_json: dict | None = None):
        super().__init__(f"yt-dlp 执行失败 (退出码: {exit_code})")
        self.exit_code = exit_code
        self.stderr = stderr
        self.parsed_json = parsed_json or {}
