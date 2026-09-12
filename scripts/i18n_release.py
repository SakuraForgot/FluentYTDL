import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

PREFIX = "fluentytdl"


def write_runtime_catalog(locales_dir: Path) -> None:
    root = ET.parse(locales_dir / "fluentytdl_en_US.ts").getroot()
    entries = {}
    for context in root.findall("context"):
        if context.findtext("name") != "RuntimeText":
            continue
        for message in context.findall("message"):
            translation = message.find("translation")
            if translation is not None and translation.get("type") not in {"vanished", "obsolete"}:
                source = message.findtext("source", "")
                text = message.findtext("translation", "")
                if not text.strip() or translation.get("type") == "unfinished":
                    raise SystemExit(f"Unreviewed runtime translation: {source}")
                entries[source] = text
    (locales_dir / "runtime_en.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _locale_of(ts_file: Path) -> str:
    """``fluentytdl_en_US.ts`` -> ``en_US``。"""
    return ts_file.stem[len(PREFIX) + 1 :] if ts_file.stem.startswith(PREFIX + "_") else ""


def _write_language_aliases(locales_dir: Path) -> None:
    """为每个"只有一个地区变体"的语言额外产出一份 bare-language .qm。

    ``QTranslator.load(QLocale("en_GB"), ...)`` 的回退链是
    ``en_GB -> en_Latn_GB -> en``，**永远不会**落到 ``en_US``。仓库里只有
    ``fluentytdl_en_US.qm``，所以 auto 模式下的 en_GB/en_AU/en_CA 用户拿不到任何翻译器，
    界面 100% 回退成中文源串（ISSUE #88）。补一份 ``fluentytdl_en.qm`` 让 ``en`` 这一级命中。

    应用只支持 en_US 和 zh_CN，因此分别生成 en / zh 地区别名。
    仍保留唯一变体检查，防止将来新增变体时静默指向错误目录。
    """
    by_language: dict[str, list[str]] = {}
    for ts_file in sorted(locales_dir.glob("*.ts")):
        locale = _locale_of(ts_file)
        if not locale:
            continue
        by_language.setdefault(locale.split("_")[0], []).append(locale)

    for language, locales in sorted(by_language.items()):
        if language in locales:  # 已经有 bare-language 的 .ts，无需别名
            continue
        if len(locales) != 1:
            print(f"  skip alias '{language}': ambiguous ({', '.join(sorted(locales))})")
            continue
        source = locales_dir / f"{PREFIX}_{locales[0]}.qm"
        if not source.exists():
            continue
        alias = locales_dir / f"{PREFIX}_{language}.qm"
        shutil.copyfile(source, alias)
        print(f"  alias {alias.name} <- {source.name}")


def main():
    root_dir = Path(__file__).resolve().parent.parent
    locales_dir = Path(os.environ.get("FLUENTYTDL_LOCALES_DIR", root_dir / "assets" / "locales"))

    if not locales_dir.exists():
        print("No locales directory found.")
        return

    for ts_file in sorted(locales_dir.glob("*.ts")):
        qm_file = ts_file.with_suffix(".qm")
        print(f"Releasing {qm_file}...")

        # 获取 PySide6 的安装路径
        import PySide6

        pyside_dir = Path(PySide6.__file__).parent

        lrelease_exe = pyside_dir / "lrelease.exe"
        if not lrelease_exe.exists():
            lrelease_exe = pyside_dir / "lrelease"
            if not lrelease_exe.exists():
                lrelease_exe = "lrelease"
        cmd = [str(lrelease_exe), str(ts_file), "-qm", str(qm_file)]
        subprocess.run(cmd, check=True)

    _write_language_aliases(locales_dir)
    write_runtime_catalog(locales_dir)

    print("i18n release completed.")


if __name__ == "__main__":
    main()
