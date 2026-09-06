"""BCP-47 语言标签匹配 —— 全项目唯一权威。

## 为什么需要这个模块

`--sub-langs` 的每一项**被 yt-dlp 当成锚定正则**去匹配字幕键，而 YouTube 的真实字幕键
几乎从不是裸语种码：

| 真实键 | 含义 |
| --- | --- |
| `en-GB` | 人工字幕，英语（英国） |
| `en-en-GB` | 由 `en-GB` 自动生成/翻译成英语 |
| `zh-Hans-en-GB` | 由 `en-GB` 自动翻译成简体中文 |

所以裸 `en` **匹配不到** `en-GB`，`zh-Hans` **匹配不到** `zh-Hans-en-GB`。把用户偏好
原样塞进 `--sub-langs` 的结果就是一个 `.vtt` 都不写、而且 yt-dlp 只留一句
`[info] There are no subtitles for the requested languages`。

匹配语义**逐字沿用** `utils/format_scorer.py` 里已在音频路径上验证有效的实现
（`-S lang:en,lang:en-gb,…`），本模块把它提升为公共 API，字幕路径不再各写一套。

> ⚠️ 不要退回"按 `-` 切开比首段"那种双向前缀比较：它会把 `zh-Hans` 和 `zh-Hant`
> 判成同一种语言。（`processing/subtitle_manager` 里那个 `_lang_matches()` 已经删了。）
"""

from __future__ import annotations

import re

# ── BCP-47 别名映射 ───────────────────────────────────────────
# key: 用户偏好写法（小写）  value: YouTube/yt-dlp 实际可能使用的等价 tag 集合
BCP47_ALIASES: dict[str, set[str]] = {
    "zh-hans": {"zh-cn", "zh-sg", "zh-simplified", "zh"},
    "zh-hant": {"zh-tw", "zh-hk", "zh-mo", "zh-traditional"},
    "zh": {"zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-sg", "zh-hk"},
    "en": {"en-us", "en-gb", "en-au", "en-ca"},
}

# 匹配紧密度（越小越贴合），用于在多个候选里挑最合适的那条
TIER_EXACT = 0
TIER_PREFIX = 1
TIER_ALIAS = 2

# 合法 BCP-47 tag 的字符集；命中它就不需要正则转义（`-` 只在字符类里有特殊含义）
_SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9-]+$")


def normalize(tag: str) -> str:
    """归一化语言标签：去空白 + 转小写。用于比较，不用于输出。"""
    return tag.strip().lower()


def canonicalize(tag: str) -> str:
    """把**用户偏好** tag 整成 BCP-47 规范大小写：`zh-hans` → `zh-Hans`、`en-gb` → `en-GB`。

    规则：语种小写、文字(4 字母)首字母大写、地区(2 字母/3 数字)大写。
    **YouTube 的字幕键恰好就是这个形式**（`zh-Hans`、`en-GB`、`pt-BR`），
    所以规范化之后不必赌 yt-dlp 编译 `--sub-langs` 正则时有没有加 `re.IGNORECASE`。

    只认严格的 `语种[-文字][-地区]` 形状，其余原样返回 —— 这样 YouTube 的复合键
    （`en-en-GB`、`zh-Hans-en-GB` 这种「目标-来源」拼法）不会被误当成
    「语种-地区-地区」而改坏。

    > ⚠️ **只用于用户偏好 tag**，不要拿它处理从 yt-dlp 读回来的字幕键：复合键与
    > 合法 BCP-47 在位置语义上无法可靠区分（`zh-Hans-en` 既像「简中，来源 en」
    > 又像「简中，地区 EN」），而偏好永远是 `en` / `zh-Hans` 这类简单 tag。
    """
    stripped = tag.strip()
    parts = stripped.split("-")
    if not 1 <= len(parts) <= 3:
        return stripped

    # 语种段：2-3 个字母
    lang = parts[0]
    if not (2 <= len(lang) <= 3 and lang.isalpha()):
        return stripped
    if len(parts) == 1:
        return lang.lower()

    # 第二段：文字(4 字母) 或 地区(2 字母 / 3 数字)
    second = parts[1]
    if len(second) == 4 and second.isalpha():
        out = [lang.lower(), second.capitalize()]
    elif len(parts) == 2 and _is_region(second):
        return f"{lang.lower()}-{second.upper()}"
    else:
        return stripped

    if len(parts) == 2:
        return "-".join(out)

    # 第三段只允许是地区，且只在第二段是「文字」时成立
    if not _is_region(parts[2]):
        return stripped
    out.append(parts[2].upper())
    return "-".join(out)


def _is_region(part: str) -> bool:
    """BCP-47 地区段：2 个字母（`GB`）或 3 个数字（`419`）。"""
    return (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit())


def _is_lang_subtag(part: str) -> bool:
    """语种段：2-3 个**小写**字母。

    小写是这里唯一能分开「地区」和「来源语种」的信号 —— `en-GB` 和 `en-en` 形状
    完全一样，只有大小写不同。YouTube 的字幕键始终守这个约定（`en-GB`、`pt-BR`
    是地区，`en-en-GB`、`zh-Hans-en-GB` 里第二/第三段的小写 `en` 是来源语种）。
    """
    return 2 <= len(part) <= 3 and part.isalpha() and part.islower()


def split_translated_key(key: str) -> tuple[str, str | None]:
    """把 YouTube 的复合字幕键拆成 `(目标语种, 来源语种)`。

    自动生成/自动翻译的键是「目标-来源」拼接，而人工字幕键就是普通 BCP-47 tag：

    | 输入 | 输出 | 说明 |
    | --- | --- | --- |
    | `en-GB` | `("en-GB", None)` | 人工字幕，`GB` 是地区不是来源 |
    | `en-en-GB` | `("en", "en-GB")` | 由 `en-GB` 自动生成的英语 |
    | `zh-Hans-en-GB` | `("zh-Hans", "en-GB")` | 由 `en-GB` 自动翻译的简体中文 |
    | `en-orig` | `("en-orig", None)` | yt-dlp 的原声标记，不是复合键 |
    | `es-419` | `("es-419", None)` | `419` 是数字地区码 |

    拆不开时原样返回 `(key, None)` —— 调用方拿到的永远是能直接查表/展示的东西，
    不需要自己判断有没有拆成功。
    """
    stripped = key.strip()
    parts = stripped.split("-")
    if len(parts) < 2 or not (2 <= len(parts[0]) <= 3 and parts[0].isalpha()):
        return stripped, None

    # 从左往右找第一个「小写语种段」：它就是来源语种的开头，它左边全是目标语种
    for i in range(1, len(parts)):
        if _is_lang_subtag(parts[i]):
            return "-".join(parts[:i]), "-".join(parts[i:])
    return stripped, None


def match_tier(pref: str, lang: str) -> int | None:
    """返回 lang 相对偏好 pref 的匹配紧密度，不匹配返回 None。

    这是 `matches()` 的底层实现，单独暴露是为了在多个候选里排序 ——
    偏好 `en` 同时命中 `en`、`en-GB`、`en-en-GB` 时要能挑出最贴合的那条。
    """
    if not pref or not lang:
        return None
    pref_lower = normalize(pref)
    lang_lower = normalize(lang)
    if pref_lower == lang_lower:
        return TIER_EXACT
    if lang_lower.startswith(pref_lower + "-"):
        return TIER_PREFIX
    if lang_lower in BCP47_ALIASES.get(pref_lower, set()):
        return TIER_ALIAS
    return None


def matches(pref: str, lang: str) -> bool:
    """判断语言标注 lang 是否符合用户偏好 pref。

    匹配规则（优先级从高到低）：
    1. 完全相等（大小写不敏感）
    2. lang 以 pref+"-" 开头（前缀匹配：`en` 命中 `en-GB`、`zh-Hans` 命中 `zh-Hans-en-GB`）
    3. 查别名表（`zh-hans` 命中 `zh-cn`、`zh-sg` 等）

    **单向是刻意的**：`en` 命中 `en-GB`，但 `en-GB` 不命中 `en` —— 用户点名要英国英语时
    不该拿到一条不知道是哪个地区的 `en`。
    """
    return match_tier(pref, lang) is not None


def preference_rank(prefs: list[str], lang: str) -> tuple[int, int]:
    """给 lang 按用户偏好列表打分，返回可直接用于 `sorted(key=...)` 的元组。

    `(命中的偏好下标, 紧密度)` —— 一条都不命中时下标是 `len(prefs)`，
    于是未命中的语言必然排在所有命中的之后。

    这是给 UI 排序/预勾选用的：老代码那种 `priority.index(t.lang_code)` 对真实
    YouTube 字幕键**整体失效**（`en-GB`、`zh-Hans-en-GB` 全部落进 `ValueError`
    兜底），排出来的顺序和用户的偏好毫无关系。

    判断「命中了没有」用 `rank[0] < len(prefs)`，不要另写一遍匹配。
    """
    for idx, pref in enumerate(prefs):
        tier = match_tier(pref, lang)
        if tier is not None:
            return idx, tier
    return len(prefs), TIER_EXACT


def expand_for_sort(lang: str) -> list[str]:
    """将单个语言偏好展开为 yt-dlp format_sort 的 `lang:` 条目列表（含别名）。

    示例：
      expand_for_sort("zh-Hans") → ["lang:zh-hans","lang:zh-cn","lang:zh-sg",...]
      expand_for_sort("orig")    → ["lang:orig"]
    """
    norm = normalize(lang)
    result = [f"lang:{norm}"]
    for alias in BCP47_ALIASES.get(norm, set()):
        result.append(f"lang:{alias}")
    return result


def to_sub_langs_pattern(pref: str) -> str:
    """把偏好转成 `--sub-langs` 能用的锚定正则，覆盖同语种的所有地区/翻译变体。

    `en` → `en(-.+)?`，同时命中 `en`、`en-GB`、`en-en-GB`。

    **不要**写成 `en.*`：那会连带命中 `eng`、`enm` 这些完全不同的语种。

    大小写走 `canonicalize()` 而**不是** `normalize()`：yt-dlp 有没有给
    `--sub-langs` 的正则加 `re.IGNORECASE` 无从确认，而规范大小写恰好就是
    YouTube 字幕键的写法（`zh-Hans`、`en-GB`），两种情况下都能命中。

    仅在无法拿到真实字幕键时使用（见 `resolve_requested()` 的回落路径）；
    能精确解析时一律传真实键，这样日志里看到的就是真实下载了什么。

    **已知取舍**：一个偏好只出一条正则，所以别名表覆盖不到
    （偏好 `zh-Hans` 命中不了键 `zh-CN`）。为每个别名都补一条正则会让
    `len(subtitleslangs)` 膨胀，凭空触发 `container_compat` 的 mp4→mkv 升级。
    """
    canon = canonicalize(pref)
    safe = canon if _SAFE_TAG_RE.match(canon) else re.escape(canon)
    return f"{safe}(-.+)?"


def resolve_requested(
    prefs: list[str],
    available: list[str],
    *,
    per_pref_limit: int = 1,
) -> tuple[list[str], list[str]]:
    """把用户偏好解析成视频上真实存在的字幕键。

    Args:
        prefs: 用户偏好，按优先级排列（如 `["zh-Hans", "en"]`）
        available: 视频实际可用的字幕键，**调用方需按期望优先级预排序**
            （如按 `SubtitleTrack.quality_rank`：人工 > 自动生成 > 自动翻译）。
            同紧密度的候选按这个顺序决胜。
        per_pref_limit: 每个偏好最多取几条真实键。

            **默认 1 是硬约束**：`utils/container_compat.ensure_subtitle_compatible_container()`
            按 `len(subtitleslangs) > 1` 决定要不要把 mp4 升成 mkv，一个 `en` 偏好展开成
            `en-GB` + `en-en-GB` + `en-US` 就会凭空触发容器升级。

    Returns:
        `(matched, missed)` —— matched 按 prefs 顺序排列的真实键（已去重），
        missed 是一条都没命中的偏好。两者都可能为空。
    """
    matched: list[str] = []
    missed: list[str] = []
    taken: set[str] = set()

    for pref in prefs:
        # (紧密度, 调用方给的顺序) 双键排序：先挑最贴合的，同档按 available 原序决胜
        candidates: list[tuple[int, int, str]] = []
        for idx, lang in enumerate(available):
            if lang in taken:
                continue
            tier = match_tier(pref, lang)
            if tier is not None:
                candidates.append((tier, idx, lang))

        if not candidates:
            missed.append(pref)
            continue

        candidates.sort(key=lambda item: (item[0], item[1]))
        for _tier, _idx, lang in candidates[:per_pref_limit]:
            matched.append(lang)
            taken.add(lang)

    return matched, missed
