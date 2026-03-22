from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    # src/fluentytdl/utils/paths.py -> src/fluentytdl/utils -> src/fluentytdl -> src -> root
    return Path(__file__).resolve().parents[3]


def user_data_dir(app_name: str = "FluentYTDL") -> Path:
    # Prefer Documents on Windows; aligns with existing log path choice.
    home = Path(os.path.expanduser("~"))
    return home / "Documents" / app_name


def resource_path(*parts: str) -> Path:
    # When frozen, resources live under sys._MEIPASS.
    base = Path(getattr(sys, "_MEIPASS", "")) if is_frozen() else project_root()
    return base.joinpath(*parts)


def frozen_internal_dir() -> Path:
    """Best-effort path to the PyInstaller onedir internal directory.

    In PyInstaller onedir builds, Python libs typically live under `_internal`.
    `sys._MEIPASS` often points to that folder, but we keep a fallback based on
    `sys.executable` to be robust across packaging variations.
    """

    if not is_frozen():
        return project_root()

    meipass = Path(getattr(sys, "_MEIPASS", "") or "")
    if str(meipass) and meipass.exists():
        return meipass

    exe_dir = Path(sys.executable).resolve().parent
    candidate = exe_dir / "_internal"
    if candidate.exists():
        return candidate
    return exe_dir


def frozen_app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def get_clean_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """获取一个干净的环境变量字典，剥离 PyInstaller 的 _MEIPASS 污染。
    
    PyInstaller 会在运行前将 _MEIPASS 注入到系统 PATH 的最前方。
    外部二进制工具 (如 ffmpeg, yt-dlp) 启动时如果加载了 _MEIPASS 中的同名 Qt/SSL DLL，
    会导致致命崩溃 (特别是 0 字节文件和静默闪退情况)。
    
    此函数会：
    1. 删除 PATH 中的所有 _MEIPASS / _MEIPASS2 路径片段
    2. 删除 OS 环境里的 _MEIPASS / _MEIPASS2 原始键
    """
    env = dict(base_env) if base_env is not None else os.environ.copy()
    
    if not is_frozen():
        return env
        
    meipass = getattr(sys, "_MEIPASS", "")
    meipass2 = getattr(sys, "_MEIPASS2", "")
    
    # 1. 删除特殊的 key
    env.pop("_MEIPASS", None)
    env.pop("_MEIPASS2", None)
    
    # 2. 清洗 PATH
    if "PATH" in env:
        paths = env["PATH"].split(os.pathsep)
        clean_paths = []
        for p in paths:
            if not p: continue
            try:
                # 忽略匹配的 _MEIPASS 路径
                if meipass and os.path.normpath(p).upper() == os.path.normpath(meipass).upper():
                    continue
                if meipass2 and os.path.normpath(p).upper() == os.path.normpath(meipass2).upper():
                    continue
            except Exception:
                pass
            clean_paths.append(p)
        env["PATH"] = os.pathsep.join(clean_paths)
        
    return env


def bundled_bin_dir() -> Path:
    # Legacy location (older builds): assets/bin
    # Note: depending on PyInstaller layout, assets may be placed next to the exe
    # rather than under sys._MEIPASS.
    p = resource_path("assets", "bin")
    if p.exists():
        return p
    p2 = frozen_app_dir() / "assets" / "bin"
    return p2


def find_bundled_executable(*relative_candidates: str) -> Path | None:
    """Find an executable shipped with the app.

    Examples:
    - find_bundled_executable("ffmpeg/ffmpeg.exe")
    - find_bundled_executable("js/deno.exe", "deno/deno.exe")
    """

    # Preferred locations (new layout):
    # - dist/_internal/ffmpeg
    # - dist/_internal/js_runtime
    internal = frozen_internal_dir()
    search_roots: list[Path] = [
        frozen_app_dir() / "bin",  # High priority: standard packaged bin folder
        internal / "ffmpeg",
        internal / "js_runtime",
        internal / "yt-dlp",
        internal / "assets" / "bin",
        bundled_bin_dir(),
    ]

    for rel in relative_candidates:
        rel_path = Path(rel)
        for root in search_roots:
            try:
                p = root / rel_path
                if p.exists():
                    return p
            except Exception:
                continue

        # Compatibility: if caller passes "ffmpeg/ffmpeg.exe", also try stripping prefix
        # for the new `internal/ffmpeg` layout.
        try:
            parts = rel_path.parts
            if (
                parts
                and parts[0].lower()
                in {"ffmpeg", "js", "node", "bun", "quickjs", "yt-dlp", "yt_dlp"}
                and len(parts) >= 2
            ):
                stripped = Path(*parts[1:])
                for root in search_roots:
                    try:
                        p = root / stripped
                        if p.exists():
                            return p
                    except Exception:
                        continue
        except Exception:
            pass

    return None


def locate_runtime_tool(*relative_candidates: str) -> Path:
    """Locate a required runtime tool.

    Priority:
    1) Explicit check in exe_dir/bin (standard packaged structure)
    2) `bin` directory adjacent to the frozen exe (generic search)
    3) system PATH (via `shutil.which`)
    If not found, raises FileNotFoundError.
    """

    # --- 0. Explicit High-Priority Check for Packaged Tools ---
    # This addresses issues where tools are definitely in `bin/` next to the exe
    # but generic logic might miss them due to path combination complexity.
    if is_frozen():
        exe_bin = frozen_app_dir() / "bin"
        if exe_bin.exists():
            for rel in relative_candidates:
                # Case A: bin/ffmpeg.exe
                p1 = exe_bin / Path(rel).name
                if p1.exists():
                    return p1.resolve()

                # Case B: bin/ffmpeg/ffmpeg.exe (if rel is "ffmpeg/ffmpeg.exe")
                p2 = exe_bin / rel
                if p2.exists():
                    return p2.resolve()

                # Case C: bin/ffmpeg/ffmpeg.exe (automatic subfolder guessing)
                # If searching for "ffmpeg.exe", try checking "bin/ffmpeg/ffmpeg.exe"
                name = Path(rel).name
                stem = Path(rel).stem  # e.g. "ffmpeg"
                p3 = exe_bin / stem / name
                if p3.exists():
                    return p3.resolve()

    # Build a list of candidate local roots to check.
    # Priority: current working directory (where user launched the exe),
    # then frozen exe dir / internal assets, then project-level assets (dev).
    local_roots: list[Path] = []

    # 0) current working directory - important for onefile builds launched from their folder
    try:
        cwd = Path.cwd()
        local_roots.append(cwd)
        local_roots.append(cwd / "bin")
        local_roots.append(cwd / "assets" / "bin")
    except Exception:
        pass

    # 1) frozen exe / internal locations
    exe_dir = frozen_app_dir()
    local_roots += [
        exe_dir,
        exe_dir / "bin",
        exe_dir / "assets" / "bin",
        frozen_internal_dir() / "assets" / "bin",
        frozen_internal_dir() / "ffmpeg",
        frozen_internal_dir() / "js_runtime",
    ]

    # 2) development/project locations
    pr = project_root()
    local_roots += [
        pr / "src" / "fluentytdl" / "assets" / "bin",
        pr / "assets" / "bin",
        pr / "bin",
    ]

    # 1) check local roots for the tool
    for root in local_roots:
        for rel in relative_candidates:
            rel_path = Path(rel)
            try:
                p = root / rel_path
                if p.exists():
                    return p.resolve()
            except Exception:
                pass
            try:
                p2 = root / rel_path.name
                if p2.exists():
                    return p2.resolve()
            except Exception:
                pass

    # 2) fallback to PATH
    for rel in relative_candidates:
        name = Path(rel).name
        try:
            found = shutil.which(name)
        except Exception:
            found = None
        if found:
            return Path(found).resolve()

    # not found
    raise FileNotFoundError(
        f"工具未找到: {relative_candidates}. 请将相应可执行文件放入 'bin' 目录，或将其加入系统 PATH，或在设置中指定路径。"
    )


def config_path() -> Path:
    # Dev: keep repo-root config.json for convenience.
    # Frozen: store config under a writable per-user directory.
    if is_frozen():
        return user_data_dir() / "config.json"
    return project_root() / "config.json"


def legacy_config_path() -> Path:
    # Old versions always used repo root.
    return project_root() / "config.json"


def doc_path() -> Path:
    """Return the path to the documentation directory."""
    if is_frozen():
        # Frozen app: check relative to exe or internal
        exe_dir = frozen_app_dir()
        candidates = [
            exe_dir / "docs",
            frozen_internal_dir() / "docs",
            resource_path("docs"),
        ]
        for p in candidates:
            if p.exists():
                return p
        return exe_dir / "docs"  # Default fallback

    # Dev mode
    return project_root() / "docs"
