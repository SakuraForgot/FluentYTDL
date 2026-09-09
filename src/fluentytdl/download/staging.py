"""下载产物事务层 —— 每任务一份清单（Manifest）+ 单点事务式提交。

## 为什么这一层非得存在

「多字幕独立保存 / 多字幕嵌入 / 独立封面」三件事一直容易**少删、误删、错误移动**，
根因不是某一处 bug，而是**「这个任务产出了哪些文件、每个文件是什么角色、该留还是该
删、该落到哪」这件事从来没有一个单一表示**。角色被三处独立地重新猜一遍（按后缀猜 /
按 stem 拼接猜 / 扫目录猜），删除决策散在 5 个地方各自 `os.remove`，搬运是 `os.walk`
\\+ 逐文件 `shutil.move` + 逐文件去重（于是视频变 `Title (1).mp4` 而字幕留在
`Title.en-GB.vtt`，播放器不再自动挂载、语言级断言失效、「彻底删除」也删不干净）。

## 四条不可让的性质

1. **角色只在进入 Manifest 时判定一次** —— 优先由报告者（`add_reported`）或生产者
   （`register_generated`）显式声明；只有 `add_reconciled()` 允许做一次集中式兜底
   分类。入库之后任何下游模块不得再次推断角色。
2. **删除只表达为清单标记**（`keep=False` / `disposition="internal"` / `presence`
   变更）。物理删除只发生在沙盒 `rmtree`，以及 commit 自己写出的占位符与 `.tmp`
   （且删除前必须过 ownership fingerprint 核对）。
3. **落地只有一处代码** —— `commit()`。
4. **失败与取消也只有一处裁决** —— `finalize_failure()` / `finalize_cancel()`，
   判据是 phase，不是调用点各自的判断。

## 生命周期（唯一权威顺序）

```
create               建 txn_<staging_id>/{payload,.parts,.work,.internal,.fytdl}
prepare_attempt      .fytdl/final.<n>.txt（每个子进程一个，Append 不跨 attempt 污染）
apply_to_opts        home=payload、temp=.parts、每个类型子键都改写进 payload
yt-dlp               每个输出行 → add_reported()          ← 角色由报告者声明
reconcile            唯一物理扫描，只扫 payload/
seal_discovery       禁止再「发现」新 artifact（≠ 禁止 Feature 生产）
Feature mutations    走本类的原子 API（register_generated / rename / supersede / replace）
verify               事务安全门：re-stat kept() → media 缺失/过小 ⇒ 阻断。**不做集合减法**
build_plan           读 kept()：group_stem + member_tail + 类型化目的地
commit               唯一 cancel gate → phase=committing → 预留 → 发布 → phase=committed
─── point of no return ───   事务成功不可逆，outcome 锁定为 completed/degraded
cleanup              rmtree 沙盒（唯一的物理删除动作）
```

`reserve` **不是**独立步骤，而是 `commit()` 的第一个动作：`os.open(dst, O_CREAT|O_EXCL)`
会直接在用户目录里造 0 字节占位符，那已经是沙盒外副作用。**第一次 external side effect
有可能发生的那一刻起，事务就是 non-cancellable critical section。**

## 三层事实彻底分离

```
observed()   创建事实      ← origin / producer     消费者：emit_actual
present()    当前物理事实  ← presence              消费者：VerifyBlocked 的诊断负载
kept()       要交付的现实                          消费者：verify / build_plan
```

`presence` 与 `disposition`/`keep` **正交**：`reconcile()` 发现清单里的项在盘上不存在
时**不删记录**，而是标 `missing`（意外消失）或 `consumed`（被后处理正常吃掉：DASH 分片
被 merge、字幕被 embed、`.webp` 被转成 `.jpg`）。历史事实一旦进 manifest 就不再消失 ——
于是 `emit_actual` 不必从别处补历史（`observability/artifacts.py` 的契约是「yt-dlp 报告
过创建」而非「此刻磁盘上还在」），`build_plan` 也不会去搬一个幽灵。

## 沙盒分区：control plane 与 data plane 物理分离

```
<download_dir>/.fluent_temp/task_<db_id>/txn_<staging_id>/
    payload/     ← yt-dlp 的 home；reconcile 只扫这里；提交只从这里取
    .parts/      ← yt-dlp 的 temp（.part/.ytdl）；永不扫、永不提交
    .work/       ← 加工落点（未被采纳的候选）；永不扫、永不提交
    .internal/   ← 被 supersede / replace 掉的旧内容；永不扫、永不提交
    .fytdl/      ← journal.json + final.<attempt>.txt；永不扫、永不提交
```

分区本身就是排除，所以不需要维护一张「排除自己的控制文件」黑名单。

**`txn_` 这一级不能省。** `task_<db_id>` 在重下时是同一个目录（`controller.py` 的
`restore_db_id` 明文复用同一个 `tasks.id`），旧 run 的残骸会被新 run 的 `reconcile()`
当成自己的产物交付出去。**但目录身份也不能用 `trace.run_id`** —— 它是
`uuid4().hex[:6]`，只有 24 bit，是给人看的展示标识；拿它当 filesystem ownership
identity 就得为碰撞准备 `.2` fallback，而两个目录的 journal 里 `run_id` 相同，GC 反而
无法区分谁活着。所以 `staging_id` 是全长 `uuid4().hex`，`trace_run_id` 只进 journal。

## 提交语义：说到做到，不夸大

| 情形 | 保证 |
|---|---|
| 捕获到提交失败，且全部补偿成功 | 目标目录干净（含占位符已清）、成员回到 payload、phase 退回 `prepared`、保留沙盒、`raise CommitFailed` |
| 捕获到提交失败，但补偿本身也失败 | phase=`rollback_failed`、沙盒保留、GC 永不自动动它。**明确不声称目标目录干净** |
| 进程被硬杀 / 断电 | 只承诺**崩溃可检出**（journal 存在且 phase ∈ {committing, rollback_failed}）与**状态可对账**。不承诺自动恢复 |

无论哪种，「搬了一半 + 上报 completed」都不允许出现：只有 phase 走到 `committed`
才 emit 成功。反过来，**`committed` 之后的异常也不得把 outcome 改成 `failed`** ——
那是同一种谎言的镜像版（文件明明下好了，任务说失败）。

## 沙盒外的东西，只删能证明是自己刚写出来的

占位符、跨卷 `.tmp`、以及**本事务刚 publish 出去的成品**，删除/移回之前都先重新
`stat` 核对 journal 里记下的 `(st_dev, st_ino, st_ctime_ns, st_size)`。核不上就不动，
只落 `kind=signal`。尤其占位符的 `st_size != 0` 意味着那已经不是占位符而是某个成品，
此时删除就是数据丢失。
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

# 私有名的跨模块引用只有这一处，且是刻意的：`_subtitle_lang_from_name()` 的规则
# （取紧贴后缀的那个点分段）必须与观测侧**逐字一致**，否则 manifest 里的 qualifier
# 和 `actual` 里的 `subtitle:<lang>` 会各算一遍、慢慢漂开。抄一份 4 行实现才是错的。
from ..models.subtitle_config import SUBTITLE_RESOLUTION_KEY
from ..observability.artifacts import MEDIA, _subtitle_lang_from_name, expected_artifacts
from ..observability.events import emit_event
from ..utils.aux_files import IMAGE_EXTS, SUBTITLE_EXTS

# 刻意**不** import `utils.logger`：事务层的每一条诊断都该走 `kind=signal`（可被 JSONL
# 检索、带 trace 上下文），`logger.warning` 那种只给人看的行在这里没有位置。
#
# 注意这不构成 import 期解耦：`observability/events.py` 自己就 import 了 `utils.logger`，
# 而后者在 import 期 `user_data_dir() / "logs"` 并 `makedirs`。所以本模块的单测**仍然
# 必须**在 import fluentytdl 之前设 `FLUENTYTDL_DATA_DIR_OVERRIDE`，否则会往真实数据
# 目录里写日志。

# ── 封闭集合 ────────────────────────────────────────────────

Kind = Literal["media", "subtitle", "thumbnail", "metadata", "intermediate"]
Origin = Literal["reported", "reconciled", "generated"]
Disposition = Literal["deliver", "internal"]
Presence = Literal["present", "consumed", "missing"]

#: 事务整体的状态。`prepared` 的精确含义是「plan 已生成，但沙盒外一片干净」——
#: 这正是 `finalize_failure()` 允许在该 phase 下 `discard()` 的依据。
Phase = Literal["downloading", "prepared", "committing", "committed", "rollback_failed"]

#: 单个成员的提交状态。
ItemState = Literal["reserved", "publishing", "published", "rolled_back", "rollback_failed"]

#: `finalize_failure()` 的裁决。`already_succeeded` 表示 phase 已过 point of no
#: return —— 调用方**不得**把 outcome 改成 `failed`。
Verdict = Literal["failed", "already_succeeded"]

SANDBOX_ROOT_NAME = ".fluent_temp"
PAYLOAD_NAME = "payload"
PARTS_NAME = ".parts"
WORK_NAME = ".work"
INTERNAL_NAME = ".internal"
CONTROL_NAME = ".fytdl"
JOURNAL_NAME = "journal.json"
JOURNAL_VERSION = 1

#: 有效媒体文件的最小字节数。与 `executor._MIN_VALID_MEDIA_BYTES` 同值 —— 那边是
#: 下载结束时的门槛，这里是提交前的门槛，同一个判据不该有两个数。
MIN_VALID_MEDIA_BYTES = 10 * 1024

#: 整组避让的上限。到这个数还撞名说明目录里已经有上千个同名组，那是异常而不是常态。
MAX_SUFFIX = 999

#: `.f399.mp4` / `.F251.webm` —— DASH 分片，永远是中间产物。规则搬自
#: `features.find_final_merged_file()`，该函数已随 Step 5 退休，这里是它唯一的遗产。
_DASH_FRAGMENT = re.compile(r"\.[fF]\d+\.\w+$")

#: 元数据与中间产物的后缀分工。两者的并集必须等于
#: `observability/artifacts.py` 的 `_IGNORED_EXTS`，由 `tests/test_download_staging.py`
#: 机械保证 —— 那边加一种后缀而这边漏掉，只表现成「有时把 .description 当成 media」。
#: 必须分成两份是因为 disposition 不同：`intermediate` 恒为 `internal`，
#: 而 `metadata` 由用户是否勾了 `writeinfojson` / `writedescription` 决定。
_METADATA_EXTS: frozenset[str] = frozenset({".json", ".txt", ".xml", ".description"})
_INTERMEDIATE_EXTS: frozenset[str] = frozenset(
    {".part", ".ytdl", ".temp", ".tmp", ".m3u8", ".mpd", ".url", ".lnk"}
)


# ── 异常族 ──────────────────────────────────────────────────


class StagingError(Exception):
    """事务层异常基类。

    `retain_staging` 表达的是**保留意愿**，不是最终判据 —— 真正的判据是 phase
    （见 `finalize_failure()`）。未知异常也不能在 `committing` 之后毁掉现场。
    """

    retain_staging = False


class StagingEscape(StagingError):
    """有路径逃出了沙盒。是 bug，现场比磁盘空间值钱。"""

    retain_staging = True


class VerifyBlocked(StagingError):
    """事务安全门拦下了提交（主媒体缺失或过小）。要能事后看 payload 里到底有什么。"""

    retain_staging = True


class CommitFailed(StagingError):
    """提交失败。`commit()` 自己已经把状态收敛完了，外层不要再动。"""

    retain_staging = True


class StagingCancelled(StagingError):
    """`commit()` 开头的 gate 判定本次已被取消。沙盒外一片干净，可以清。

    刻意**不**复用 `workers.DownloadCancelled` —— 那会让 `download/staging.py`
    反向依赖 `download/workers.py`（后者要 import 前者）。调用点一行翻译即可。
    """

    retain_staging = False


# ── 进程内预留 ──────────────────────────────────────────────

#: 真实场景就是同进程多个 `DownloadWorker` 线程同时上岸。跨进程那一半由
#: `os.open(O_CREAT|O_EXCL)` 兜住；这里挡的是同进程内两个线程都 `stat` 到空闲
#: 然后都去写的那一刻（旧代码用裸 `os.path.exists()` 判据，正是这个洞）。
_RESERVE_LOCK = threading.RLock()
_RESERVED_DESTS: set[str] = set()


# ── 数据模型 ────────────────────────────────────────────────


@dataclass
class StagedArtifact:
    """一个暂存产物。同时回答五个问题，缺一个就会有模块去别处重新猜。

    - `kind` / `qualifier` —— 是什么
    - `origin` / `producer` —— 从哪来
    - `presence` —— 曾存在吗、现在还在吗
    - `disposition` / `keep` —— 要不要交付
    - `final_path` —— 交到哪
    """

    id: str
    path: str
    kind: Kind
    qualifier: str = ""
    primary: bool = False

    disposition: Disposition = "deliver"
    keep: bool = True
    presence: Presence = "present"

    reason: str = ""
    degraded: bool = False

    origin: Origin = "reported"
    producer: str = ""
    parent_ids: tuple[str, ...] = ()
    final_path: str = ""

    @property
    def deliverable(self) -> bool:
        return self.presence == "present" and self.disposition == "deliver" and self.keep

    def describe(self) -> dict[str, Any]:
        """给观测用的扁平摘要。刻意不含绝对路径 —— 那由 `sanitize_path` 那侧处理。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "qualifier": self.qualifier or None,
            "origin": self.origin,
            "producer": self.producer or None,
            "presence": self.presence,
            "disposition": self.disposition,
            "keep": self.keep,
            "primary": self.primary,
            "degraded": self.degraded,
            "reason": self.reason or None,
        }


@dataclass(frozen=True)
class PlannedMember:
    """提交计划里的一个成员。`dst` 不在这里 —— 它由整组后缀 `n` 决定。"""

    artifact_id: str
    src: str
    dest_dir: str
    member_tail: str
    kind: Kind


@dataclass(frozen=True)
class CommitPlan:
    """一次提交的完整计划。`destination()` 是唯一的目标路径构造点。"""

    group_stem: str
    members: tuple[PlannedMember, ...]

    def destination(self, member: PlannedMember, n: int) -> str:
        return _destination(self.group_stem, member, n)

    def destinations(self, n: int) -> list[str]:
        return [self.destination(m, n) for m in self.members]


def _destination(group_stem: str, member: PlannedMember, n: int) -> str:
    """整组同后缀。命名遵循 `utils/paths.py::_preserve_loser` 的确定性规则：
    只用 ` (n)`，**不引入时间戳 / uuid** —— 那会让重跑每次多一份副本。

    做成模块级函数是为了让 `build_plan()` 的撞名自检也能用同一份实现，而不必为了算
    一个目标路径先造一个半成品 `CommitPlan`。
    """
    stem = group_stem if n <= 0 else f"{group_stem} ({n})"
    return os.path.join(member.dest_dir, stem + member.member_tail)


# ── Manifest ────────────────────────────────────────────────


class Manifest:
    """一个任务产出了什么的**唯一表示**。只记事实与意图，从不碰文件系统。

    物理操作全部在 `StagingArea` 上，且每个方法内部固定三步
    「`assert_inside` → 文件系统操作 → Manifest 状态更新」。这样不会出现
    `os.rename(...)` 紧跟 `manifest.rename(...)` 那种两步写法 —— 两步之间抛异常
    就让物理世界与清单分叉，而那正是本轮要消灭的东西。
    """

    def __init__(self, payload_dir: str) -> None:
        self._payload_dir = payload_dir
        self._items: dict[str, StagedArtifact] = {}
        self.discovery_sealed: bool = False
        self.group_stem: str | None = None
        #: `group_stem` 的来源，只为了让「谁是 authority」可裁决：字幕先到就先定下
        #: （纯字幕任务里它是唯一 authority），但普通任务里主媒体一旦确定就该盖过它 ——
        #: 否则 DASH 场景下先写出的 `Title.en.vtt` 会把 stem 锁死在字幕那一侧，
        #: 而主媒体此刻还叫 `Title.f399.mp4`。封板之后此字段不再变化。
        self.stem_authority: Literal["", "subtitle", "media", "explicit"] = ""
        #: 嵌入成功的**结构化**证据（`{"subtitle", "thumbnail"}`）。人类日志行
        #: 只作 fallback / 显示，不作 authority，也不再把结论绑在 yt-dlp 的 rc 上。
        self.embed_evidence: set[str] = set()

    # ── 登记：三个入口，角色来源写在函数名里 ──

    def add_reported(
        self,
        path: str,
        kind: Kind,
        *,
        qualifier: str = "",
        primary: bool = False,
        metadata_deliver: bool = False,
    ) -> StagedArtifact:
        """yt-dlp 报告过创建。角色由**报告者**声明，这里不推断。"""
        self._require_unsealed("add_reported")
        return self._put(
            path,
            kind,
            origin="reported",
            qualifier=qualifier,
            primary=primary,
            metadata_deliver=metadata_deliver,
        )

    def add_reconciled(
        self,
        path: str,
        kind: Kind,
        *,
        qualifier: str = "",
        metadata_deliver: bool = False,
    ) -> StagedArtifact:
        """对账补录。**全流程唯一允许做兜底角色推断的入口**（推断在 `StagingArea`
        的 `_classify()` 里，且只在这一个调用点被用到）。
        """
        self._require_unsealed("add_reconciled")
        return self._put(
            path,
            kind,
            origin="reconciled",
            qualifier=qualifier,
            metadata_deliver=metadata_deliver,
        )

    def register_generated(
        self,
        path: str,
        *,
        kind: Kind,
        producer: str,
        parent_ids: Iterable[str] = (),
        qualifier: str = "",
        primary: bool = False,
    ) -> StagedArtifact:
        """Feature 显式生产。**封板管的是「发现」，不是「创造」** —— 所以这个入口
        在 `seal_discovery()` 之后仍然可用。

        `producer` 必填：没有产生者的「生成物」等于又一次匿名推断。
        """
        if not producer:
            raise StagingError("register_generated 必须给出 producer")
        art = self._put(
            path,
            kind,
            origin="generated",
            qualifier=qualifier,
            primary=primary,
        )
        art.producer = producer
        art.parent_ids = tuple(parent_ids)
        return art

    def seal_discovery(self) -> None:
        """封住**推断**，不封**创造**。之后 `add_reported` / `add_reconciled` 抛异常，
        `register_generated` 不受影响。
        """
        self.discovery_sealed = True

    # ── 状态变更 ──

    def rename(self, artifact_id: str, new_path: str) -> StagedArtifact:
        """改物理路径，**`id` 不变** —— 否则下游拿着旧 id 就找不到人了。"""
        art = self.require(artifact_id)
        art.path = new_path
        return art

    def promote(self, artifact_id: str) -> StagedArtifact:
        """指定主媒体。同一时刻只有一个 primary。"""
        art = self.require(artifact_id)
        if art.kind != "media":
            raise StagingError(f"只有 media 能 promote，{artifact_id} 是 {art.kind}")
        for other in self._items.values():
            if other.kind == "media":
                other.primary = other.id == art.id
        return art

    def supersede(self, old_id: str, new_id: str, *, reason: str) -> None:
        """两个已登记项争同一个交付位，旧的转入内部区。

        **没有替代品就不许消失** —— 所以这里强制要求 new 已登记。
        """
        old = self.require(old_id)
        new = self.require(new_id)
        if new.presence != "present":
            raise StagingError(f"替代品 {new_id} 当前 presence={new.presence}，不能顶位")
        old.disposition = "internal"
        old.primary = False
        old.reason = reason
        if old.kind == "media" and not any(a.primary for a in self.kept("media")):
            new.primary = True

    def drop(self, *, id: str | None = None, kind: Kind | None = None, reason: str) -> int:
        """把项标成不交付。**唯一合法理由是「嵌入确实成功了，且用户没要独立文件」。**

        返回被标记的项数。注意这只改标记 —— 物理删除只发生在沙盒 `rmtree`。
        """
        if not reason:
            raise StagingError("drop 必须给出 reason")
        targets = [self.require(id)] if id else [a for a in self._items.values() if a.kind == kind]
        for art in targets:
            art.keep = False
            art.reason = reason
        return len(targets)

    def mark_presence(self, artifact_id: str, presence: Presence, *, reason: str = "") -> None:
        """改物理状态。**不删记录** —— 历史事实一旦进 manifest 就不再消失。"""
        art = self.require(artifact_id)
        art.presence = presence
        if reason:
            art.reason = reason

    def mark_degraded(self, artifact_id: str, reason: str) -> None:
        """这一项没达到预期，但**仍然保留**（嵌入失败、完整性校验不过）。"""
        art = self.require(artifact_id)
        art.degraded = True
        art.reason = reason

    # ── 查询：三个集合，三个消费者，不许混用 ──

    def observed(self) -> list[StagedArtifact]:
        """曾被登记过的事实全集（含 `consumed` / `missing`）。消费者：`emit_actual`。

        `artifacts.py` 的契约是「yt-dlp **报告过创建**，不是此刻磁盘上还在」——
        嵌进容器后被吃掉的字幕仍必须计入 `actual`，否则会把「嵌进去了」误报成
        「没拿到」。
        """
        return sorted(self._items.values(), key=lambda a: a.id)

    def present(self) -> list[StagedArtifact]:
        """此刻物理存在的（含 `internal`）。消费者：`VerifyBlocked` 的诊断负载 ——
        阻断时唯一有用的信息是「payload 里此刻究竟有什么」。
        """
        return [a for a in self.observed() if a.presence == "present"]

    def kept(self, kind: Kind | None = None) -> list[StagedArtifact]:
        """present ∧ deliver ∧ keep。消费者：`verify()` 与 `build_plan()`。"""
        return [a for a in self.observed() if a.deliverable and (kind is None or a.kind == kind)]

    def primary_media(self) -> StagedArtifact | None:
        """主媒体。**不猜** —— 没有 primary 标记且不止一个候选时返回 None，
        让 `verify()` / `build_plan()` 硬失败，而不是随手挑一个交付出去。

        `media` 是复数概念：`vr_keep_source` 默认开，`Title.mp4`（equi）与
        `Title.eac.mp4`（原片）合法共存。
        """
        medias = self.kept("media")
        for art in medias:
            if art.primary:
                return art
        return medias[0] if len(medias) == 1 else None

    def get(self, artifact_id: str) -> StagedArtifact | None:
        return self._items.get(artifact_id)

    def require(self, artifact_id: str) -> StagedArtifact:
        art = self._items.get(artifact_id)
        if art is None:
            raise StagingError(f"清单里没有 {artifact_id!r}")
        return art

    def __len__(self) -> int:
        return len(self._items)

    # ── 内部 ──

    def _require_unsealed(self, op: str) -> None:
        if self.discovery_sealed:
            raise StagingError(f"discovery 已封板，{op}() 不再可用（生产请走 register_generated）")

    def make_id(self, path: str) -> str:
        """稳定标识 = 首次登记时的 payload 内相对路径（normcase）。"""
        return os.path.normcase(os.path.relpath(path, self._payload_dir))

    def _put(
        self,
        path: str,
        kind: Kind,
        *,
        origin: Origin,
        qualifier: str = "",
        primary: bool = False,
        metadata_deliver: bool = False,
    ) -> StagedArtifact:
        if kind not in ("media", "subtitle", "thumbnail", "metadata", "intermediate"):
            raise StagingError(f"未知 kind: {kind!r}")
        artifact_id = self.make_id(path)
        existing = self._items.get(artifact_id)
        if existing is not None:
            # 同一个路径被报告两次（`Destination:` 与 `[Merger]` 都会提到主文件）。
            # 已有记录胜出，只补强 primary / qualifier，绝不降级已知信息。
            if primary:
                existing.primary = True
            if qualifier and not existing.qualifier:
                existing.qualifier = qualifier
            existing.presence = "present"
            return existing

        art = StagedArtifact(id=artifact_id, path=path, kind=kind, qualifier=qualifier)
        art.origin = origin
        art.primary = primary
        # `intermediate` 是唯一硬编码 internal 的 —— DASH 分片、ffmpeg 中间文件没有
        # 任何情况下该交付。`metadata` 按用户意图，否则等以后加「保存 info.json」
        # 开关时又要回头改 Kind/Disposition 的语义。
        if kind == "intermediate":
            art.disposition = "internal"
        elif kind == "metadata" and not metadata_deliver:
            art.disposition = "internal"
        self._items[artifact_id] = art

        if kind == "subtitle" and self.group_stem is None and self.stem_authority == "":
            # 纯字幕任务（`skip_download`）没有主媒体可当 authority，首个字幕即定 stem。
            # 普通任务里这个值只是占位：`seal_discovery()` 会用 `primary_media()` 盖掉它。
            stem = _stem_without_qualifier(os.path.basename(path), qualifier)
            if stem:
                self.group_stem = stem
                self.stem_authority = "subtitle"
        return art


# ── 工具函数 ────────────────────────────────────────────────


def _stem_without_qualifier(basename: str, qualifier: str) -> str:
    """`Title.en-GB.vtt` + `en-GB` → `Title`；拿不掉 qualifier 就退回普通 stem。"""
    stem, _ext = os.path.splitext(basename)
    if qualifier and stem.lower().endswith("." + qualifier.lower()):
        return stem[: -(len(qualifier) + 1)]
    return stem


def _member_tail(basename: str, group_stem: str, qualifier: str) -> tuple[str, bool]:
    """成员相对整组 stem 的尾巴（`.en-GB.vtt` / `.eac.mp4` / `.mp4`）。

    返回 `(tail, exact)`。`exact=False` 表示 basename 没有以 group_stem 开头 ——
    Windows 上 yt-dlp 的 stdout 可能丢掉片假名中点这类字符，于是解析出的名字与磁盘
    实际名字不一致。此时按 qualifier + 后缀重建，并让调用方落一条 signal。
    """
    if basename.startswith(group_stem):
        return basename[len(group_stem) :], True
    ext = os.path.splitext(basename)[1]
    return (f".{qualifier}{ext}" if qualifier else ext), False


def _fingerprint(path: str) -> dict[str, Any]:
    """ownership proof。四元组一起看：单看 `st_size` 认不出「刚好也是 0 字节」的
    别人的文件，单看 `st_ino` 认不出 inode 被回收后重用。
    """
    st = os.stat(path)
    return {
        "dev": st.st_dev,
        "ino": st.st_ino,
        "ctime_ns": st.st_ctime_ns,
        "size": st.st_size,
    }


def _fingerprint_matches(path: str, expected: Mapping[str, Any] | None) -> bool:
    if not expected:
        return False
    try:
        actual = _fingerprint(path)
    except OSError:
        return False
    return all(actual.get(k) == expected.get(k) for k in ("dev", "ino", "ctime_ns", "size"))


def _same_volume(src: str, dst_dir: str) -> bool:
    """同卷判定。跨卷时 `os.replace` 会抛 `OSError`，必须走两阶段复制。"""
    try:
        return os.stat(src).st_dev == os.stat(dst_dir).st_dev
    except OSError:
        return False


def _classify(name: str) -> tuple[Kind, str]:
    """兜底角色推断。**全项目只有 `add_reconciled()` 这一个调用点。**

    后缀表复用 `utils/aux_files`（不再抄一份），DASH 分片规则搬自已退休的
    `features.find_final_merged_file()`。
    """
    lowered = name.lower()
    ext = os.path.splitext(lowered)[1]
    if ext in SUBTITLE_EXTS:
        return "subtitle", _subtitle_lang_from_name(name)
    if ext in IMAGE_EXTS:
        return "thumbnail", ""
    if ext in _INTERMEDIATE_EXTS:
        return "intermediate", ""
    if _DASH_FRAGMENT.search(name):
        return "intermediate", ""
    if ext in _METADATA_EXTS:
        return "metadata", ""
    if not ext:
        # 无后缀的东西进 `media` 会让「主文件到手了」被一个残片满足。
        return "intermediate", ""
    return "media", ""


def resolve_subtitle_qualifier(name: str, opts: Mapping[str, Any] | None = None) -> str:
    """字幕语言键。先拿文件名尾段去匹配迟解析的真实键，匹配不上再回落纯文件名解析。

    先匹配的理由：迟解析知道 `en(-.+)?` 这个正则实际命中的是 `en-GB`，而纯文件名
    解析只看得见点分段 —— 两者一致时无所谓，不一致时应该信更有信息量的那一个。
    """
    guess = _subtitle_lang_from_name(name)
    if not guess or not opts:
        return guess
    known: list[str] = []
    resolution = opts.get(SUBTITLE_RESOLUTION_KEY)
    if isinstance(resolution, Mapping):
        known.extend(str(x) for x in (resolution.get("matched") or []))
    raw = opts.get("subtitleslangs")
    if isinstance(raw, (list, tuple)):
        known.extend(str(x) for x in raw if str(x) and not str(x).startswith("-"))
    for key in known:
        if key and key.lower() == guess.lower():
            return key
    return guess


# ── StagingArea ─────────────────────────────────────────────


class StagingArea:
    """一个下载事务的沙盒 + 清单 + 提交机。**所有动文件的动作都在这里。**"""

    def __init__(
        self,
        txn_dir: str,
        *,
        download_dir: str,
        task_key: str,
        staging_id: str,
        trace_run_id: str = "",
        trace: Any = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.txn_dir = txn_dir
        self.download_dir = download_dir
        self.task_key = task_key
        self.staging_id = staging_id
        self.trace_run_id = trace_run_id
        self.trace = trace

        self.payload_dir = os.path.join(txn_dir, PAYLOAD_NAME)
        self.parts_dir = os.path.join(txn_dir, PARTS_NAME)
        self.work_dir = os.path.join(txn_dir, WORK_NAME)
        self.internal_dir = os.path.join(txn_dir, INTERNAL_NAME)
        self.control_dir = os.path.join(txn_dir, CONTROL_NAME)

        self.manifest = Manifest(self.payload_dir)
        #: 用户想要的类型化目的地（`-P [TYPES:]PATH` 的语义）。只在这里留值，
        #: opts 里的每个键都被改写进 payload。
        self.dest_intent: dict[str, str] = {}

        self._cancel_check = cancel_check
        self._lock = threading.RLock()
        self._phase: Phase = "downloading"
        self._attempt = 0
        self._final_path_file = ""
        self._placeholders: dict[str, dict[str, Any]] = {}
        self._reserved_group: tuple[str, ...] = ()
        self._items_state: dict[str, dict[str, Any]] = {}
        self._suffix_n = 0
        self._pending_cancel = False
        self._discarded = False
        self._payload_real = os.path.realpath(self.payload_dir)
        self._parts_real = os.path.realpath(self.parts_dir)

    # ── 建立 / 拆除 ──

    @classmethod
    def create(
        cls,
        download_dir: str,
        task_key: str,
        staging_id: str | None = None,
        *,
        trace_run_id: str = "",
        trace: Any = None,
        cancel_check: Callable[[], bool] | None = None,
        group_stem: str | None = None,
    ) -> StagingArea:
        """建一个 transaction-scoped 沙盒。

        `staging_id` 是全长 `uuid4().hex`，**不是** `trace.run_id`（24 bit，会撞）。
        全长 uuid 不需要碰撞 fallback：`exist_ok=False` 撞了就是 bug，直接抛。
        """
        sid = staging_id or uuid.uuid4().hex
        download_dir = os.path.abspath(download_dir)
        txn_dir = os.path.join(
            download_dir, SANDBOX_ROOT_NAME, f"task_{task_key}", f"txn_{sid}"
        )
        os.makedirs(os.path.dirname(txn_dir), exist_ok=True)
        os.makedirs(txn_dir, exist_ok=False)

        area = cls(
            txn_dir,
            download_dir=download_dir,
            task_key=str(task_key),
            staging_id=sid,
            trace_run_id=trace_run_id,
            trace=trace,
            cancel_check=cancel_check,
        )
        for path in (
            area.payload_dir,
            area.parts_dir,
            area.work_dir,
            area.internal_dir,
            area.control_dir,
        ):
            os.makedirs(path, exist_ok=True)
        area._payload_real = os.path.realpath(area.payload_dir)
        if group_stem:
            # 封面直下模式：没有 yt-dlp 报告、也没有主媒体可当 authority，
            # stem 由 `outtmpl` 在建沙盒时就定下，`seal_discovery()` 不得改它。
            area.manifest.group_stem = group_stem
            area.manifest.stem_authority = "explicit"
        area._write_journal()
        # 日志侧的「run_id ↔ 沙盒」对照关系由这条提供，所以排查路径不依赖目录名。
        area._signal("staging_created", level="DEBUG", stage="download", staging_dir=area.txn_dir)
        return area

    def prepare_attempt(self, attempt: int) -> str:
        """给本轮子进程一个独立的 `final.<n>.txt` 并返回其绝对路径。

        必须 attempt-scoped：`--print-to-file` 是 **Append** 语义，而同一个 Worker
        会自动重试同一个 URL（`trace.py` 明文「自动重试不换 run_id」）。单文件会被
        上一次 attempt 的路径污染，读到的「主媒体」可能是失败那次的。比「启动前
        truncate」好的地方是失败 attempt 的证据还留着，事后能看出它写出了什么。
        """
        with self._lock:
            self._attempt = int(attempt)
            name = f"final.{self._attempt}.txt"
            path = os.path.join(self.control_dir, name)
            self._final_path_file = os.path.join(CONTROL_NAME, name)
            # 只建空文件，不 truncate 已有内容（同一 attempt 内 yt-dlp 可能多次写）
            with open(path, "a", encoding="utf-8"):
                pass
            self._write_journal()
            return path

    def read_attempt_paths(self, attempt: int | None = None) -> list[str]:
        """读本轮 `final.<n>.txt` 里 yt-dlp 报告的 `after_move` 路径。"""
        n = self._attempt if attempt is None else int(attempt)
        path = os.path.join(self.control_dir, f"final.{n}.txt")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return [line.strip() for line in fh if line.strip()]
        except OSError:
            return []

    def parts_bytes(self) -> int:
        """`.parts/` 里 `.part` 文件的字节总和 —— section 下载的进度兜底探针。

        `--download-sections` 时 yt-dlp 不发常规进度行，`executor` 只能自己看
        `.part` 文件长大。那段代码原先扫的是 `paths["home"]`，`home != temp` 之后
        那里永远是 0 字节，进度条会静默卡死；所以扫描收敛到这里 —— 事务层是唯一
        允许物理扫描的地方，而它读的只是字节数，不推断任何角色。
        """
        total = 0
        try:
            with os.scandir(self.parts_dir) as it:
                for entry in it:
                    if entry.name.endswith(".part"):
                        try:
                            total += entry.stat().st_size
                        except OSError:
                            continue
        except OSError:
            return 0
        return total

    def apply_to_opts(self, opts: dict[str, Any]) -> None:
        """把 `paths` 的**每一个类型子键**都改写进 payload，值只留在 `dest_intent`。

        今天全项目 5 处 `paths` 赋值都只写 `home`，所以类型子键泄漏不是活跃 bug ——
        但一旦有人加一个 `subtitle` 子键，旧代码就会让字幕绕过沙盒直写用户目录。
        这里是前向封堵。

        `home != temp` 之后 `.part`/`.ytdl` 不再与产物混层，于是上岸时的后缀跳过表
        和 `_clean_part_files()` 都可以整体退休。
        """
        src = dict(opts.get("paths") or {})
        self.dest_intent = {}
        for key, value in src.items():
            if key == "temp":
                continue
            text = str(value or "").strip()
            if text:
                self.dest_intent[key] = os.path.abspath(text)
        self.dest_intent.setdefault("home", self.download_dir)

        # `dest_intent` 里指向沙盒内部的目录降级回 home：那要么是配置错误，要么是
        # 有人试图让产物「提交」到沙盒里，两种都不该静默通过。
        for key, value in list(self.dest_intent.items()):
            if key == "home":
                continue
            if self._is_inside_txn(value):
                self._signal(
                    "dest_intent_inside_sandbox",
                    level="WARNING",
                    stage="download",
                    path_kind=key,
                )
                self.dest_intent[key] = self.dest_intent["home"]

        new_paths = {key: self.payload_dir for key in src if key != "temp"}
        new_paths.setdefault("home", self.payload_dir)
        new_paths["temp"] = self.parts_dir
        opts["paths"] = new_paths

    def cleanup(self) -> None:
        """唯一的物理删除动作。**只在 `phase=committed` 之后调用。**"""
        self._rmtree()

    def discard(self) -> None:
        """沙盒外一片干净时才允许调用（`downloading` / `prepared`）。

        Worker 侧**不得**直接调它 —— 走 `finalize_failure()` / `finalize_cancel()`，
        否则「commit 刚保住的诊断现场」会被外层通用 `except` 立刻删掉。
        """
        self._rmtree()

    def _rmtree(self) -> None:
        with self._lock:
            if self._discarded:
                return
            self._discarded = True
        shutil.rmtree(self.txn_dir, ignore_errors=False)
        # 顺手收掉自己那两级空壳父目录（`.fluent_temp/task_<key>/`），否则每个任务都
        # 会在用户的下载目录里留一个空 `task_<db_id>/`，要等下次启动 GC 才清。
        # `_prune_empty_dir` 只在目录**确实为空**时 `os.rmdir`，所以并发事务的兄弟
        # 目录不可能被它带走。
        task_dir = os.path.dirname(self.txn_dir)
        _prune_empty_dir(task_dir)
        _prune_empty_dir(os.path.dirname(task_dir))

    # ── 包含性：最底层的 invariant ──

    def assert_inside(self, path: str) -> str:
        """`path` 必须在 `payload/` 内，返回 canonical 绝对路径。

        相对 `outtmpl` **不等于**在沙盒内 —— `..\\..\\foo.mp4` 也是相对路径。所以判据
        是 `realpath` + `commonpath`，而不是字符串前缀：前者认得 junction / symlink /
        8.3 短名。任何一处逃逸都是硬失败，不降级、不警告了事。
        """
        return _assert_under(path, self._payload_real, self.payload_dir)

    def _is_inside_txn(self, path: str) -> bool:
        try:
            _assert_under(path, os.path.realpath(self.txn_dir), self.txn_dir)
            return True
        except StagingEscape:
            return False

    def landing_path(self, path: str) -> str:
        """把 yt-dlp 报告的路径换算成它**最终落在 payload 里的那个路径**。

        `home != temp` 之后报告路径几乎全在 temp 侧 —— 实测（`yt-dlp.exe` 2026.08.30，
        `-P home:<payload> -P temp:<parts>`）：

        ```
        [info] Writing video subtitles to:      <parts>\\Title.en.vtt
        [download] Destination:                 <parts>\\Title.f395.mp4
        [Merger] Merging formats into           "<parts>\\Title.webm"
        [info] Writing video metadata as JSON to: <payload>\\Title.info.json   ← 唯一直写 home 的
        [MoveFiles] Moving file "<parts>\\…" to "<payload>\\…"
        ```

        所以 `add_reported()` 要是直接 `assert_inside(payload)`，**每个文件都会
        `StagingEscape`**，整条下载路径当场硬失败。

        换算规则是「相对 `.parts/` 的路径照搬到 `payload/` 下」：两侧 basename 恒等
        （temp 名与 home 名由同一个 outtmpl 渲染，只有目录不同），所以这是等价映射，
        而且让 temp 报告与随后 `[MoveFiles]` 的 home 报告落在同一个 `id` 上，由
        `Manifest._put()` 自然去重。

        换算之后 artifact 的 `path` 指向一个**此刻还不存在**的文件 —— 这不影响任何
        消费者：`presence` 的真相由紧接着的 `reconcile()` 统一裁决（找不到就标
        `missing`/`consumed`），而 `observed()` 要的正是「报告过创建」这个历史事实。
        """
        real = os.path.realpath(path)
        try:
            return _assert_under(real, self._payload_real, self.payload_dir)
        except StagingEscape:
            pass
        # 只从 `.parts/` 换算。`.work` / `.internal` / `.fytdl` 是我们自己的控制面，
        # yt-dlp 不会报告它们；沙盒外的路径更是硬失败，不在这里降级。
        rel = os.path.relpath(
            _assert_under(real, self._parts_real, self.parts_dir), self._parts_real
        )
        return os.path.join(self.payload_dir, rel)

    # ── 登记（带包含性检查的薄封装）──

    def add_reported(
        self,
        path: str,
        kind: Kind,
        *,
        qualifier: str = "",
        primary: bool = False,
        opts: Mapping[str, Any] | None = None,
    ) -> StagedArtifact:
        resolved = self.landing_path(path)
        if kind == "subtitle" and not qualifier:
            qualifier = resolve_subtitle_qualifier(os.path.basename(resolved), opts)
        return self.manifest.add_reported(
            resolved,
            kind,
            qualifier=qualifier,
            primary=primary,
            metadata_deliver=_metadata_wanted(opts, resolved),
        )

    def reconcile(self, opts: Mapping[str, Any] | None = None) -> dict[str, int]:
        """**全流程唯一的目录扫描**，且只扫 `payload/`。

        两个方向都做：
        - 盘上有而清单没有的 → `add_reconciled()`（Windows stdout 丢字符时是常态）
        - 清单有而盘上没有的 → `mark_presence(missing|consumed)`，**不删记录**

        `consumed` 与 `missing` 的分工是「能否解释」：DASH 分片被 merge、字幕被 embed、
        `.webp` 被转成 `.jpg` 都是后处理正常吃掉，算 `consumed`；其余算 `missing`。

        封板后调用直接抛 —— 对账是发现行为。这条同时保证了 `.internal/` 里被
        supersede 的旧内容永远不会被对账重新捡回来（那些内容只在封板**之后**产生）。
        """
        if self.manifest.discovery_sealed:
            raise StagingError("discovery 已封板，reconcile() 不再可用")

        stats = {"added": 0, "missing": 0, "consumed": 0}
        seen: set[str] = set()
        for root, dirs, files in os.walk(self.payload_dir):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                artifact_id = self.manifest.make_id(full)
                seen.add(artifact_id)
                if self.manifest.get(artifact_id) is not None:
                    continue
                kind, qualifier = _classify(name)
                if kind == "subtitle":
                    qualifier = resolve_subtitle_qualifier(name, opts)
                self.manifest.add_reconciled(
                    self.assert_inside(full),
                    kind,
                    qualifier=qualifier,
                    metadata_deliver=_metadata_wanted(opts, full),
                )
                stats["added"] += 1

        for art in self.manifest.observed():
            if art.id in seen or art.presence != "present":
                continue
            if _explains_disappearance(art, self.manifest):
                self.manifest.mark_presence(art.id, "consumed", reason="postprocess_consumed")
                stats["consumed"] += 1
            else:
                self.manifest.mark_presence(art.id, "missing", reason="vanished_after_report")
                stats["missing"] += 1

        # 没有 primary 时在这里补一次 —— `reconcile()` 是唯一被允许做集中式推断的
        # 地方，所以「挑一个主媒体」也只能发生在这里，不能散给 build_plan 去猜。
        medias = self.manifest.kept("media")
        if medias and not any(a.primary for a in medias):
            chosen = max(medias, key=lambda a: _size_or_zero(a.path))
            self.manifest.promote(chosen.id)
            self._signal(
                "primary_media_inferred",
                level="DEBUG",
                stage="postprocess",
                candidates=len(medias),
            )

        if stats["added"] or stats["missing"]:
            self._signal(
                "manifest_reconciled",
                level="WARNING" if stats["missing"] else "INFO",
                stage="postprocess",
                **stats,
            )
        return stats

    def seal_discovery(self) -> None:
        """封板，并把 `group_stem` 定下来（此后 `build_plan()` 只消费不推断）。

        authority 顺序是 `explicit`（cover-direct 的 `outtmpl`）> `media` > `subtitle`。
        字幕先到时 `_put()` 已经占了个位，这里由主媒体盖掉 —— DASH 场景下字幕
        （`Title.en.vtt`）确实比合并产物先落盘，但整组该跟着主媒体的名字走。
        """
        if self.manifest.stem_authority != "explicit":
            primary = self.manifest.primary_media()
            if primary is not None:
                self.manifest.group_stem = os.path.splitext(os.path.basename(primary.path))[0]
                self.manifest.stem_authority = "media"
        self.manifest.seal_discovery()

    # ── Feature 用的原子 API：`assert_inside` → 文件系统 → 清单 ──

    def reserve_workfile(self, stem_hint: str, ext: str, *, seed_from: str | None = None) -> str:
        """给加工步骤一个落点。**在 `.work/` 里，不在 payload** —— 否则嵌入中途崩溃
        就会在 payload 里留下 `tmpXXXX.mp4`，而下一个 run 复用沙盒时它会变成新清单里
        一个 `origin="reconciled"` 的假 media。

        `seed_from` 是给**原地改写型工具**准备的：AtomicParsley 走 `--overWrite`、
        mutagen 走 `audio.save()`，它们没有「输出到另一个路径」的用法。给了
        `seed_from` 就先把源字节复制进 workfile，工具改写的始终是副本，源全程只读。
        """
        safe = re.sub(r'[<>:"/\\|?*]', "_", stem_hint or "work")[:80] or "work"
        base = f"{safe}.{uuid.uuid4().hex[:8]}{ext}"
        path = os.path.join(self.work_dir, base)
        if seed_from:
            src = self.manifest.require(seed_from)
            shutil.copy2(src.path, path)
        return path

    def register_generated(
        self,
        work_path: str,
        *,
        kind: Kind,
        producer: str,
        parent_ids: Iterable[str] = (),
        target_name: str | None = None,
        qualifier: str = "",
        primary: bool = False,
    ) -> StagedArtifact:
        """采纳一个候选：先把它从 `.work/` 移进 payload，再登记。

        **创建不是权威，采纳才是。** 所以 Feature 只管往 `reserve_workfile()` 给的
        路径里写，路径进不进清单由这里决定；转码失败时那个文件从未登记，随沙盒
        `rmtree` 消失，清单无感。
        """
        if not os.path.isfile(work_path):
            raise StagingError(f"register_generated: {work_path!r} 不存在")
        if not self._is_inside_txn(work_path):
            raise StagingEscape(work_path)
        name = target_name or os.path.basename(work_path)
        dst = os.path.join(self.payload_dir, name)
        resolved = self.assert_inside(dst)
        if os.path.normcase(os.path.realpath(work_path)) != os.path.normcase(resolved):
            os.replace(work_path, resolved)
        return self.manifest.register_generated(
            resolved,
            kind=kind,
            producer=producer,
            parent_ids=parent_ids,
            qualifier=qualifier,
            primary=primary,
        )

    def rename_artifact(self, artifact_id: str, new_name: str) -> StagedArtifact:
        """payload 内改名。`id` 不变，物理与清单一步完成。"""
        art = self.manifest.require(artifact_id)
        dst = self.assert_inside(os.path.join(self.payload_dir, new_name))
        os.replace(art.path, dst)
        return self.manifest.rename(artifact_id, dst)

    def supersede_artifact(self, old_id: str, new_id: str, *, reason: str) -> None:
        """两个已登记项争一个交付位。旧的**移进 `.internal/`，不删** ——

        好处是新产物在提交前万一被发现有问题旧源还在，而且「物理删除只发生在
        `rmtree`」这条不变量不用为 VR 破例。
        """
        old = self.manifest.require(old_id)
        self.manifest.require(new_id)
        dst = os.path.join(self.internal_dir, f"{uuid.uuid4().hex[:8]}-{os.path.basename(old.path)}")
        os.replace(old.path, dst)
        self.manifest.rename(old_id, dst)
        self.manifest.supersede(old_id, new_id, reason=reason)

    def replace_artifact_content(
        self, artifact_id: str, work_path: str, *, producer: str
    ) -> StagedArtifact:
        """同一个逻辑 artifact 换内容（嵌入类）。`id` 与 `final_path` 规划都不变。

        与 `supersede_artifact` 的分工不能混：VR 产出的是**另一个名字**的 artifact
        （`_equi`），封面/字幕嵌入产出的是**同一个逻辑文件的新内容**。
        失败时原样恢复 —— 主媒体绝不会因为一次嵌入失败而消失。
        """
        art = self.manifest.require(artifact_id)
        if not os.path.isfile(work_path):
            raise StagingError(f"replace_artifact_content: {work_path!r} 不存在")
        if not self._is_inside_txn(work_path):
            raise StagingEscape(work_path)
        if os.path.getsize(work_path) <= 0:
            raise StagingError(f"replace_artifact_content: {work_path!r} 是空文件")

        backup = os.path.join(
            self.internal_dir, f"{uuid.uuid4().hex[:8]}-{os.path.basename(art.path)}"
        )
        os.replace(art.path, backup)
        try:
            os.replace(work_path, art.path)
        except OSError:
            os.replace(backup, art.path)
            raise
        art.producer = producer
        return art

    # ── 校验门 ──

    def verify(self, final_opts: Mapping[str, Any]) -> None:
        """事务安全门：只回答「现在提交安全吗」。**不做任何集合减法。**

        `verify` 是 safety gate，`emit_actual` 是 contract evaluation ——
        别再让两者执行同一个减法。`delivered:*` 是提交后才成立的事实，让提交前的门
        去要求它是生命周期倒置（每个正常下载都会「缺」`delivered:media`）。

        三条判据：

        1. `expected_artifacts()` 里**有没有** `media` —— 这是唯一一处仍读期望集合的
           地方，问的是布尔而不是做减法。于是 `skip_download` 的纯字幕任务不需要特例
           （`artifacts.py` 本来就不给它加 `MEDIA`）。
        2. 若有，`kept("media")` 非空且 `primary_media()` 的字节数够大。
        3. 逐项 re-stat `kept()`：这一步到 `reconcile()` 之间可能被杀软隔离掉文件，
           消失的项标 `missing`（于是自动掉出 `kept()`，`build_plan()` 不会去搬一个
           幽灵），然后回到第 2 条重判。

        只有第 2 条不成立才抛。字幕/封面缺失**照常提交** —— 少一条字幕绝不能变成
        扔掉整个视频的理由，那个降级由 commit 之后的 `delivered:*` 减法产出。

        注意 `artifacts.py` 里「verify 阶段零 I/O」说的是**产物集合的计算**
        （`found_artifacts()` 是纯路径函数，这条不变），不是这个事务门。
        """
        vanished = 0
        for art in list(self.manifest.kept()):
            if not os.path.isfile(art.path):
                self.manifest.mark_presence(art.id, "missing", reason="vanished_before_commit")
                vanished += 1
        if vanished:
            self._signal(
                "staging_artifact_vanished", level="WARNING", stage="verify", count=vanished
            )

        if MEDIA not in expected_artifacts(final_opts):
            return

        primary = self.manifest.primary_media()
        if primary is None:
            raise VerifyBlocked(self._blocked_payload("primary_media_missing"))
        size = _size_or_zero(primary.path)
        if size < MIN_VALID_MEDIA_BYTES:
            raise VerifyBlocked(self._blocked_payload("primary_media_too_small", size=size))

    def _blocked_payload(self, code: str, **extra: Any) -> str:
        """阻断时唯一有用的信息是「payload 里此刻究竟有什么」—— 那正是 `present()`，
        含 `internal` 的那些（被 supersede 的源、被替换掉的旧内容）。
        """
        present = [a.describe() for a in self.manifest.present()]
        self._signal(
            "verify_blocked", level="ERROR", stage="verify", reason=code, present=present, **extra
        )
        return f"{code}: present={len(present)} extra={extra}"

    # ── 计划 ──

    def build_plan(self) -> CommitPlan:
        """读 `kept()`，产出整组计划。**只消费 `group_stem`，不推断。**"""
        stem = self.manifest.group_stem
        if not stem:
            raise StagingError("group_stem 未确定，build_plan() 拒绝猜测")

        home = self.dest_intent.get("home") or self.download_dir
        members: list[PlannedMember] = []
        inexact = 0
        for art in self.manifest.kept():
            dest_dir = self.dest_intent.get(_path_key_for(art.kind), home)
            tail, exact = _member_tail(os.path.basename(art.path), stem, art.qualifier)
            if not exact:
                inexact += 1
            members.append(
                PlannedMember(
                    artifact_id=art.id,
                    src=self.assert_inside(art.path),
                    dest_dir=dest_dir,
                    member_tail=tail,
                    kind=art.kind,
                )
            )
        if inexact:
            self._signal(
                "member_tail_rebuilt", level="WARNING", stage="finalize", count=inexact
            )
        if not members:
            raise StagingError("没有任何可交付成员，build_plan() 拒绝产出空计划")

        # 两个成员映射到同一个目标就是**静默数据丢失**：后搬的那个盖掉先搬的，用户少
        # 一个文件而日志一切正常。而且 `_reserve_group()` 会先撞上自己刚建的占位符，
        # 于是整组避让 999 次后才以「撞名」失败 —— 那个错误信息指向的是完全错误的原因。
        #
        # 触发路径只有两条，都是异常：`_member_tail` 走了 `exact=False` 回落（两条同
        # qualifier 同后缀的字幕），或 payload 的两级子目录里有同名文件。所以这里硬失败
        # 而不是自动去重 —— 命名规则不许引入 uuid/时间戳后缀，能选的只有「盖掉一个」
        # 和「拒绝提交」，后者至少沙盒还留着（`VerifyBlocked.retain_staging = True`）。
        seen_dest: dict[str, str] = {}
        for member in members:
            key = os.path.normcase(_destination(stem, member, 0))
            if key in seen_dest:
                raise VerifyBlocked(
                    self._blocked_payload(
                        "duplicate_destination",
                        first=seen_dest[key],
                        second=member.artifact_id,
                    )
                )
            seen_dest[key] = member.artifact_id

        with self._lock:
            self._phase = "prepared"
            self._write_journal()
        return CommitPlan(group_stem=stem, members=tuple(members))

    # ── 提交：唯一一处落地 ──

    def commit(self, plan: CommitPlan) -> None:
        """整组提交。**唯一的、也是最后一次 cancel gate 在这个方法的第一行。**

        `reserve` 不是独立步骤：`os.open(dst, O_CREAT|O_EXCL)` 已经在用户目录里造了
        0 字节占位符，那就是第一个 external side effect。如果 reserve 留在 commit
        之外，就存在「占位符已建、phase 还不是 committing、取消路径认为可以 discard」
        这条竞态，于是取消状态机被迫知道「预留」这个概念。**取消公开的 reserve 阶段
        就没有这个问题。** 内部因此不再有任何取消检查，`n += 1` 的重试循环也在临界
        区内。
        """
        self._check_cancelled()
        with self._lock:
            self._phase = "committing"
            self._items_state = {}
            self._placeholders = {}
            self._write_journal()  # write-ahead：先落盘再动手

        try:
            n = self._reserve_group(plan)
            self._publish_group(plan, n)
        except BaseException as exc:
            self._compensate(plan, exc)
            # 必须包成 `CommitFailed` 再抛：补偿成功后 phase 退回 `prepared`，而
            # `finalize_failure()` 在该 phase 下按 `retain_staging` 裁决 —— 一个裸
            # `OSError`（目标文件被杀软/播放器锁住，正是最常见的提交失败）的
            # `retain_staging` 是 False，于是刚刚收敛好的诊断现场会被立刻 rmtree。
            # `StagingError` 自己已经带了保留意愿，非 `Exception` 的
            # （KeyboardInterrupt / SystemExit）原样上抛，不吞进事务语义里。
            if isinstance(exc, StagingError) or not isinstance(exc, Exception):
                raise
            raise CommitFailed(f"提交失败: {exc!r}") from exc

        with self._lock:
            self._phase = "committed"
            self._write_journal()
        # 成功也必须释放进程内预留，否则 `_RESERVED_DESTS` 只增不减。释放是安全的：
        # 那个名字现在被真正的成品占着，别人的 `O_EXCL` 一样会 `EEXIST` 然后避让。
        self._release_reservations()
        if self._cancel_check is not None and self._cancel_check():
            # late cancel：事务已成功，用户手上是完整一组。记事实后忽略 ——
            # 绝不出现「已取消 + 半组成品」。
            self._pending_cancel = True
            self._signal("pending_cancel", level="WARNING", stage="finalize")

    def _reserve_group(self, plan: CommitPlan) -> int:
        """整组探一个空闲后缀。**整组成功才算预留成功**，任一 `EEXIST` 就释放该组
        全部占位符、`n += 1` 重试。胜负由 `O_EXCL` 而非 `exists()` 裁决 ——
        后者让两个 worker 可以同时看到同一个名字空闲，然后静默覆盖对方的成品。
        """
        for member in plan.members:
            os.makedirs(member.dest_dir, exist_ok=True)

        for n in range(MAX_SUFFIX + 1):
            dests = plan.destinations(n)
            with _RESERVE_LOCK:
                if any(os.path.normcase(d) in _RESERVED_DESTS for d in dests):
                    continue
                created: dict[str, dict[str, Any]] = {}
                clash = False
                for dst in dests:
                    try:
                        fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                    except OSError as exc:
                        if exc.errno not in (errno.EEXIST, errno.EACCES):
                            raise
                        clash = True
                        break
                    os.close(fd)
                    # fingerprint 必须在**创建的这一刻**取，而不是删除前才取 ——
                    # 后者等于拿现状证明现状，核对就永远通过，ownership proof 失去意义。
                    created[dst] = _fingerprint_safe(dst)
                if clash:
                    for dst in created:
                        self._remove_placeholder(dst, created[dst])
                    continue
                for dst in dests:
                    _RESERVED_DESTS.add(os.path.normcase(dst))

            with self._lock:
                self._suffix_n = n
                self._reserved_group = tuple(dests)
                self._placeholders = created
                self._items_state = {
                    m.artifact_id: {
                        "state": "reserved",
                        "src": m.src,
                        "dst": plan.destination(m, n),
                        "same_volume": _same_volume(m.src, m.dest_dir),
                    }
                    for m in plan.members
                }
                self._write_journal()
            return n

        raise CommitFailed(f"整组避让超过 {MAX_SUFFIX} 次仍撞名: {plan.group_stem!r}")

    def _publish_group(self, plan: CommitPlan, n: int) -> None:
        for index, member in enumerate(plan.members):
            dst = plan.destination(member, n)
            state = self._items_state[member.artifact_id]
            with self._lock:
                # WAL：必须在动文件**之前**落盘，否则崩在 `os.replace` 与 journal
                # 更新之间时，journal 自己都不知道搬没搬。
                state["state"] = "publishing"
                self._write_journal()

            if state["same_volume"]:
                os.replace(member.src, dst)
            else:
                self._publish_cross_volume(member, dst, index)

            with self._lock:
                state["state"] = "published"
                state["published"] = _fingerprint_safe(dst)
                self._placeholders.pop(dst, None)
                self._write_journal()
            art = self.manifest.get(member.artifact_id)
            if art is not None:
                art.final_path = dst

    def _publish_cross_volume(self, member: PlannedMember, dst: str, index: int) -> None:
        """跨卷：`copy2` → 校验 → `os.replace`（与 dst 同卷，原子）。

        源**不删** —— 它随沙盒 `rmtree` 消失，于是「物理删除只发生在 rmtree」这条
        不变量不用为跨卷破例，回滚时也还有一份完整的源。
        """
        tmp = os.path.join(member.dest_dir, f".fluentytdl-{self.staging_id[:8]}-{index}.tmp")
        try:
            shutil.copy2(member.src, tmp)
            src_size = os.path.getsize(member.src)
            tmp_size = os.path.getsize(tmp)
            if src_size != tmp_size:
                raise CommitFailed(f"跨卷复制大小不符: {src_size} != {tmp_size}")
            if member.kind == "media" and tmp_size < MIN_VALID_MEDIA_BYTES:
                raise CommitFailed(f"跨卷复制后主媒体过小: {tmp_size}")
            os.replace(tmp, dst)
        except BaseException:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)  # 自己刚写出来的 .tmp，硬约束 1 的合法例外
                except OSError:
                    pass
            raise

    def _compensate(self, plan: CommitPlan, exc: BaseException) -> None:
        """逆序补偿。**不声称一定成功** —— dst 可能已被杀软/播放器锁住。

        方向性要记牢：**进入更危险的状态先写 journal（write-ahead），退回更安全的
        状态后写 journal。** 所以这里先清占位符、先搬回成员，全部成功之后才写
        `prepared`。顺序反了的话，崩在中间会留下一个自称 `prepared`（= 沙盒外干净）
        而实际还挂着占位符的 journal，GC 就会照 `prepared` 那一行直接 `rmtree`，
        用户目录里的 0 字节文件永远没人清。
        """
        failures: list[str] = []

        for member in reversed(plan.members):
            state = self._items_state.get(member.artifact_id)
            if not state:
                continue
            if state.get("state") != "published":
                continue
            dst = state["dst"]
            if not _fingerprint_matches(dst, state.get("published")):
                # 核不上就不动：那已经不是我们刚写出去的那个文件了，删它就是数据丢失。
                state["state"] = "rollback_failed"
                failures.append(dst)
                self._signal(
                    "rollback_fingerprint_mismatch", level="ERROR", stage="finalize", dst=dst
                )
                continue
            try:
                if state.get("same_volume"):
                    os.replace(dst, member.src)
                else:
                    os.remove(dst)  # 跨卷时源还在 payload，dst 是本事务写出的副本
                state["state"] = "rolled_back"
            except OSError as err:
                state["state"] = "rollback_failed"
                failures.append(dst)
                self._signal(
                    "rollback_failed", level="ERROR", stage="finalize", dst=dst, error=repr(err)
                )

        for dst, fingerprint in list(self._placeholders.items()):
            if not self._remove_placeholder(dst, fingerprint):
                failures.append(dst)
        self._placeholders = {k: v for k, v in self._placeholders.items() if k in failures}
        self._release_reservations()

        with self._lock:
            # 明明回滚失败还把 journal 写回 `prepared` 是在撒谎：那会让 GC 以为
            # 「清掉占位符就能 rmtree」，而用户目录里其实躺着一个搬出去了又搬不回来
            # 的成品。
            self._phase = "rollback_failed" if failures else "prepared"
            self._write_journal()
        if failures:
            self._signal(
                "commit_rollback_incomplete",
                level="ERROR",
                stage="finalize",
                pending=len(failures),
                cause=repr(exc),
            )

    def _release_reservations(self) -> None:
        with _RESERVE_LOCK:
            for dst in self._reserved_group:
                _RESERVED_DESTS.discard(os.path.normcase(dst))
            self._reserved_group = ()

    def _remove_placeholder(self, dst: str, fingerprint: Mapping[str, Any] | None) -> bool:
        """删占位符前先核对 ownership。核不上就**不删**，只落 signal。

        `size != 0` 意味着那已经不是占位符而是某个成品（可能是自己刚 `os.replace`
        成功的，也可能是别人的），此时删除就是数据丢失。
        """
        if not os.path.exists(dst):
            return True
        if not _fingerprint_matches(dst, fingerprint):
            self._signal(
                "placeholder_fingerprint_mismatch", level="ERROR", stage="finalize", dst=dst
            )
            return False
        try:
            os.remove(dst)  # 自己刚 O_EXCL 建出来的 0 字节占位符，硬约束 1 的合法例外
            return True
        except OSError as err:
            self._signal(
                "placeholder_remove_failed", level="ERROR", stage="finalize", error=repr(err)
            )
            return False

    # ── 终态裁决：只有这两个入口 ──

    def finalize_failure(self, exc: BaseException | None = None) -> Verdict:
        """唯一的失败裁决点。**判据是 phase，不是异常类型** —— 未知异常也不能在
        `committing` 之后毁掉现场。

        返回 `already_succeeded` 时调用方**不得**把 outcome 改成 `failed`：
        全部成员 `published` 之后用户手上的文件已经完整，此时任何异常都只能是
        housekeeping 出错，而一个裸 `except Exception: error.emit(...)` 会制造这轮
        重构最想消灭的另一种谎言的镜像版 —— **文件明明下好了，任务说失败。**
        """
        phase = self._phase
        if phase == "committed":
            self._signal(
                "postcommit_exception",
                level="ERROR",
                stage="finalize",
                phase=phase,
                cause=repr(exc) if exc else None,
            )
            return "already_succeeded"

        if phase == "rollback_failed":
            # 这是唯一需要人看一眼的状态，但**仍然只落 signal**：`finalize_failure()`
            # 返回 `failed` 之后 Worker 的失败边界会为同一个异常产出那唯一一条
            # `kind=diagnosis`（硬规则 2 = 一次失败一处判定）。这里再发一条就是两处
            # 判定 —— 信息量并不增加，反而让"这次失败到底怎么判的"有两个答案。
            self._signal(
                "staging_rollback_failed",
                level="ERROR",
                stage="finalize",
                staging_dir=self.txn_dir,
                cause=repr(exc) if exc else None,
            )
            return "failed"

        if phase == "committing":
            # 保留，只落 signal，不 rmtree、不猜、不续做。
            self._signal(
                "staging_retained_committing",
                level="ERROR",
                stage="finalize",
                cause=repr(exc) if exc else None,
            )
            return "failed"

        if getattr(exc, "retain_staging", False):
            self._signal(
                "staging_retained",
                level="WARNING",
                stage="finalize",
                phase=phase,
                cause=repr(exc) if exc else None,
            )
            return "failed"

        self._discard_quietly("failure")
        return "failed"

    def finalize_cancel(self) -> str:
        """唯一的取消裁决点。返回 `cancelled` / `pending` / `no_op`。"""
        phase = self._phase
        if phase == "committed":
            return "no_op"
        if phase in ("committing", "rollback_failed"):
            self._pending_cancel = True
            self._signal("pending_cancel", level="WARNING", stage="cancel", phase=phase)
            return "pending"
        self._discard_quietly("cancel")
        return "cancelled"

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def pending_cancel(self) -> bool:
        return self._pending_cancel

    def published_paths(self) -> list[str]:
        """已交付的最终路径。`delivered:*` token 的唯一事实来源。"""
        return [
            str(state["dst"])
            for state in self._items_state.values()
            if state.get("state") == "published"
        ]

    def observed_paths(self) -> list[str]:
        """取得侧喂给 `emit_actual` 的是 **payload 内原名**（语言段完整、未经整组
        改名），交付侧喂 `final_path`。两边都不 stat 磁盘。
        """
        return [a.path for a in self.manifest.observed()]

    # ── journal ──

    def journal_path(self) -> str:
        return os.path.join(self.control_dir, JOURNAL_NAME)

    def journal_snapshot(self) -> dict[str, Any]:
        return {
            "version": JOURNAL_VERSION,
            "task_key": self.task_key,
            "staging_id": self.staging_id,
            "run_id": self.trace_run_id,
            "attempt": self._attempt,
            "final_path_file": self._final_path_file,
            "phase": self._phase,
            "group_stem": self.manifest.group_stem,
            "suffix_n": self._suffix_n,
            "pending_cancel": self._pending_cancel,
            "placeholders": dict(self._placeholders),
            "items": {k: dict(v) for k, v in self._items_state.items()},
            "updated_at": time.time(),
        }

    def _write_journal(self) -> None:
        """原子写 —— 沿用 `utils/update_signal.py::_write_ready_file` 的 tmp +
        `os.replace` 写法，避免 GC 读到半个 JSON。
        """
        path = self.journal_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.journal_snapshot(), fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as err:
            # journal 写不动是严重问题（崩溃将不可检出），但不该在这里改变事务走向：
            # 让真正的失败原因来决定终态。
            self._signal(
                "journal_write_failed", level="ERROR", stage="finalize", error=repr(err)
            )

    def _signal(self, code: str, *, level: str = "INFO", stage: str = "download", **fields: Any) -> None:
        emit_event(
            "signal",
            trace=self.trace,
            level=level,
            stage=stage,
            code=code,
            staging_id=self.staging_id[:8],
            **fields,
        )

    def _check_cancelled(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise StagingCancelled("commit gate: 本次已被取消")

    def _discard_quietly(self, why: str) -> None:
        try:
            self.discard()
        except OSError as err:
            # 清不掉不该盖住真正的失败原因；沙盒留给 GC。
            self._signal(
                "staging_discard_failed", level="WARNING", stage="finalize", why=why, error=repr(err)
            )


# ── 模块级工具 ──────────────────────────────────────────────


def _assert_under(path: str, root_real: str, root_display: str) -> str:
    actual = os.path.realpath(path)
    try:
        if os.path.commonpath([root_real, actual]) != root_real:
            raise StagingEscape(f"{path!r} 不在 {root_display!r} 内")
    except ValueError:
        # Windows 上不同盘 `commonpath` 直接抛 —— 那当然也是逃逸。
        raise StagingEscape(f"{path!r} 与 {root_display!r} 不同卷") from None
    return actual


def _size_or_zero(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _fingerprint_safe(path: str) -> dict[str, Any]:
    try:
        return _fingerprint(path)
    except OSError:
        return {}


def _path_key_for(kind: Kind) -> str:
    """`kind` → yt-dlp `-P [TYPES:]PATH` 的类型键。"""
    return {"subtitle": "subtitle", "thumbnail": "thumbnail", "metadata": "infojson"}.get(
        kind, "home"
    )


def _metadata_wanted(opts: Mapping[str, Any] | None, path: str) -> bool:
    """`.info.json` / `.description` 是不是用户主动要的。

    不写死成 `internal` 的理由：用户勾了「保存 info.json」它就是正式交付产物，
    否则等以后加那个开关时又要回头改 `Kind` / `Disposition` 的语义。
    """
    if not opts:
        return False
    lowered = path.lower()
    if lowered.endswith(".info.json"):
        return bool(opts.get("writeinfojson"))
    if lowered.endswith(".description"):
        return bool(opts.get("writedescription"))
    if lowered.endswith(".json"):
        return bool(opts.get("writeinfojson"))
    return False


def _explains_disappearance(art: StagedArtifact, manifest: Manifest) -> bool:
    """能解释的消失算 `consumed`，解释不了的算 `missing`。

    三种能解释：DASH 分片被 merge、字幕被 embed（有结构化证据）、封面被转格式或
    嵌入吃掉。其余（尤其主媒体不见了）必须是 `missing`，那正是要报出来的事。
    """
    if art.kind == "intermediate":
        return True
    if art.kind == "subtitle" and "subtitle" in manifest.embed_evidence:
        return True
    if art.kind == "thumbnail":
        return True
    return False


def gc_orphans(
    download_dir: str,
    live_staging_ids: Iterable[str],
    *,
    older_than_hours: float = 6.0,
    trace: Any = None,
) -> dict[str, int]:
    """回收 `.fluent_temp` 下的孤儿事务。**宁可留下可诊断状态，也不擅自毁数据。**

    存活判据是 `staging_id` 而不是 `task_key` 也不是 `run_id`：按 task 判会让「同一个
    任务的旧残骸」永远算活着（`restore_db_id` 复用同一个 `tasks.id`），按 `run_id` 判
    在 24-bit 碰撞时无法区分谁活着。目录名里就是 `staging_id`，所以判活不需要读
    journal —— 读不出 journal 的沙盒也能被正确判定。

    | phase | 动作 |
    |---|---|
    | 无 journal / `downloading` / `prepared` | 直接 `rmtree`（`prepared` 的定义就是「plan 已生成但还没进临界区」，占位符要到 `committing` 才存在）|
    | `committing` | 先对账，只清可证明是垃圾的（fingerprint 对得上的 0 字节占位符），**沙盒原样保留** |
    | `rollback_failed` | **什么都不动**，只落 signal。这是唯一需要人看一眼的状态 |
    | `committed` | 直接 `rmtree`（上次 cleanup 没成功，这是它的恢复路径）|

    `older_than_hours <= 0` 表示**不设最低年龄**（「现在就全部回收」），而不是「只放过
    刚刚创建的」—— 见下面 `cutoff` 处的注释。
    """
    live = {str(x) for x in live_staging_ids}
    stats = {"scanned": 0, "removed": 0, "retained": 0, "placeholders_cleared": 0}
    root = os.path.join(os.path.abspath(download_dir), SANDBOX_ROOT_NAME)
    if not os.path.isdir(root):
        return stats

    # `<= 0` 表示「不设最低年龄，现在就全部回收」，而不是 `cutoff = now`：
    # `time.time()` 在 Windows 上的分辨率是 15.625ms（`GetSystemTimeAsFileTime`），
    # 而目录 mtime 来自更细的时钟，于是刚建好的沙盒有约 11% 的概率 `mtime > now`
    # 被判成「太新」而跳过 —— 一个要求「立即清空」的调用方会静默漏掉一部分。
    cutoff = time.time() - older_than_hours * 3600.0 if older_than_hours > 0 else None
    try:
        task_dirs = sorted(os.listdir(root))
    except OSError:
        return stats

    for task_name in task_dirs:
        task_dir = os.path.join(root, task_name)
        if not os.path.isdir(task_dir):
            continue
        try:
            txn_names = sorted(os.listdir(task_dir))
        except OSError:
            continue
        for txn_name in txn_names:
            txn_dir = os.path.join(task_dir, txn_name)
            if not os.path.isdir(txn_dir) or not txn_name.startswith("txn_"):
                continue
            staging_id = txn_name[len("txn_") :]
            if staging_id in live:
                continue
            stats["scanned"] += 1
            if cutoff is not None:
                try:
                    if os.path.getmtime(txn_dir) > cutoff:
                        continue
                except OSError:
                    continue

            journal = _read_journal(txn_dir)
            phase = str((journal or {}).get("phase") or "")

            if phase == "rollback_failed":
                stats["retained"] += 1
                emit_event(
                    "signal",
                    trace=trace,
                    level="ERROR",
                    stage="startup",
                    code="staging_orphan_rollback_failed",
                    staging_dir=txn_dir,
                )
                continue

            if phase == "committing":
                cleared = 0
                for dst, fingerprint in (journal or {}).get("placeholders", {}).items():
                    if _fingerprint_matches(dst, fingerprint) and _size_or_zero(dst) == 0:
                        try:
                            os.remove(dst)
                            cleared += 1
                        except OSError:
                            pass
                stats["placeholders_cleared"] += cleared
                stats["retained"] += 1
                emit_event(
                    "signal",
                    trace=trace,
                    level="WARNING",
                    stage="startup",
                    code="staging_orphan_interrupted",
                    staging_dir=txn_dir,
                    placeholders_cleared=cleared,
                )
                continue

            try:
                shutil.rmtree(txn_dir)
                stats["removed"] += 1
            except OSError as err:
                stats["retained"] += 1
                emit_event(
                    "signal",
                    trace=trace,
                    level="WARNING",
                    stage="startup",
                    code="staging_orphan_rmtree_failed",
                    staging_dir=txn_dir,
                    error=repr(err),
                )

        _prune_empty_dir(task_dir)
    _prune_empty_dir(root)
    return stats


def _read_journal(txn_dir: str) -> dict[str, Any] | None:
    path = os.path.join(txn_dir, CONTROL_NAME, JOURNAL_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _prune_empty_dir(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass
