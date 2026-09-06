"""
FluentYTDL 核心基础设施层

注意：此包仅包含跨功能域共享的基础设施服务。
功能领域特定的模块已迁移到对应的功能域包中：

- auth/          -> 身份验证 (AuthService, CookieManager, CookieSentinel)
- youtube/       -> YouTube 服务 (YoutubeService, YtDlpCli, POTManager)
- download/      -> 下载管理 (DownloadManager, Workers)
- processing/    -> 后处理 (AudioProcessor, SubtitleManager)
"""

from .config_manager import ConfigManager, config_manager
from .dependency_manager import DependencyManager, dependency_manager

__all__ = [
    # 配置管理
    "ConfigManager",
    "config_manager",
    # 依赖管理
    "DependencyManager",
    "dependency_manager",
]
