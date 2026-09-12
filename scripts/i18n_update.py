import ast
import shutil
import subprocess
from pathlib import Path


def update_runtime_markers(src_dir: Path) -> None:
    """Make shared service/log templates visible to lupdate, deterministically."""
    sources = set()
    paths = sorted(src_dir.rglob("*.py"))
    main = src_dir.parents[1] / "main.py"
    if main.exists():
        paths.append(main)
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            index = {"tr_text": 0, "log_text": 2, "english": 0, "standalone_text": 0}.get(
                node.func.id
            )
            if index is not None and len(node.args) > index:
                arg = node.args[index]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    sources.add(arg.value)
    path = src_dir / "utils" / "ui_text.py"
    raw = path.read_text(encoding="utf-8")
    prefix, rest = raw.split("# BEGIN GENERATED MARKERS\n", 1)
    _, suffix = rest.split("# END GENERATED MARKERS", 1)
    import json

    rows = [
        f'    QT_TRANSLATE_NOOP("RuntimeText", {json.dumps(source, ensure_ascii=False)}),'
        for source in sorted(sources)
    ]
    path.write_text(
        prefix
        + "# BEGIN GENERATED MARKERS\n# fmt: off\n_SOURCES = (\n"
        + "\n".join(rows)
        + "\n)\n# fmt: on\n# END GENERATED MARKERS"
        + suffix,
        encoding="utf-8",
        newline="\n",
    )


def _find_lupdate() -> str:
    """定位 lupdate。

    直接调 PySide6 包内的可执行文件，绕过 uv trampoline 错误。原先这里硬编码
    ``.venv/Lib/site-packages/PySide6/lupdate.exe``（Windows 专用布局），Linux CI 上
    必然落到裸 ``lupdate`` 兜底 —— 而 PySide6 并不往 PATH 里装这个名字，
    于是 CI 里跑不起来。按 PySide6.__file__ 解析才是跨平台的写法。
    """
    try:
        import PySide6

        pyside_dir = Path(PySide6.__file__).parent
        for name in ("lupdate.exe", "lupdate"):
            candidate = pyside_dir / name
            if candidate.exists():
                return str(candidate)
    except ImportError:
        pass

    for name in ("pyside6-lupdate", "lupdate"):
        found = shutil.which(name)
        if found:
            return found

    raise SystemExit("找不到 lupdate，请先安装 PySide6（uv sync --extra dev）")


def main():
    root_dir = Path(__file__).resolve().parent.parent
    src_dir = root_dir / "src" / "fluentytdl"
    locales_dir = root_dir / "assets" / "locales"

    locales_dir.mkdir(parents=True, exist_ok=True)
    update_runtime_markers(src_dir)

    TARGET_LANGUAGES = ["en_US", "zh_CN"]

    lupdate_exe = _find_lupdate()

    # 排序保证跨平台、跨机器的抽取顺序一致 —— 否则 CI 的 `git diff --exit-code`
    # 会因为 rglob 顺序不同而误报"有未抽取的字符串"。
    py_files = sorted(str(p) for p in src_dir.rglob("*.py"))
    main_py = root_dir / "main.py"
    if main_py.exists():
        py_files.append(str(main_py))

    for lang in TARGET_LANGUAGES:
        ts_file = locales_dir / f"fluentytdl_{lang}.ts"
        print(f"Updating {ts_file}...")

        cmd = [lupdate_exe, *py_files, "-ts", str(ts_file)]
        subprocess.run(cmd, check=True)

    print("i18n update completed.")


if __name__ == "__main__":
    main()
