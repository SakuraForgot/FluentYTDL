from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

from ..core.config_manager import config_manager
from ..core.hardware_manager import hardware_manager
from ..models.subtitle_config import SUBTITLE_RESOLUTION_KEY
from ..processing.thumbnail_embed import can_embed_thumbnail, get_unsupported_formats_warning
from ..processing.thumbnail_embedder import thumbnail_embedder
from ..utils.logger import logger
from ..utils.spatialmedia import metadata_utils

if TYPE_CHECKING:
    from .staging import Manifest, StagedArtifact, StagingArea
    from .workers import DownloadWorker


class DownloadContext:
    """下载上下文，用于在 Feature 和 Worker 之间传递状态"""

    def __init__(self, worker: DownloadWorker, opts: dict[str, Any]):
        self.worker = worker
        self.opts = opts
        self.url = worker.url

    @property
    def output_path(self) -> str | None:
        return self.worker.output_path

    @output_path.setter
    def output_path(self, value: str | None):
        self.worker.output_path = value

    # ── 事务层入口 ──
    #
    # 两个都要，因为职责本来就分在两处：**动文件**的原子方法在 `StagingArea` 上
    # （`reserve_workfile` / `register_generated` / `rename_artifact` /
    # `supersede_artifact` / `replace_artifact_content`），**只动状态**的在 `Manifest` 上
    # （`kept` / `drop` / `primary_media` / `mark_degraded` / `promote` / `embed_evidence`）。
    #
    # 两个都容许为 None：单测可以不带事务构造 context，那时 Feature 退化成 no-op
    # 而不是 `AttributeError`。**但绝不回退到"自己去猜"** —— 没有清单就等于没有
    # 本任务产物的可靠答案，猜出来的答案正是这轮重构要消灭的东西。

    @property
    def staging(self) -> StagingArea | None:
        return getattr(self.worker, "staging", None)

    @property
    def manifest(self) -> Manifest | None:
        staging = self.staging
        return staging.manifest if staging is not None else None

    # 这里以前有 `dest_paths` 属性。它是 executor 记下的**混装**集合（视频、分片、字幕、
    # 封面全在里面），Feature 侧每个消费点都得自己再过一遍后缀/正则来重新猜角色 ——
    # 而那正是「同一批文件的角色被三处独立地重新猜一遍」的其中一处。
    # `worker.dest_paths` 本身还活着（`core/controller.py` 与 UI 侧仍在读，Step 8 退休），
    # 但 Feature 链从此只认清单。

    # 这里以前有 `sandbox_dir` 属性（`getattr(self.worker, "sandbox_dir", None)`）。
    # 《下载产物事务层》Step 4 之后 Worker 上不再有那个属性 —— 沙盒是
    # `StagingArea` 持有的事务，路径分成 payload / .parts / .fytdl 三个分区，
    # "沙盒目录"这个单一字符串已经表达不了任何有用的东西，而它也早就没有消费者了。

    def subtitle_artifacts(self) -> list[StagedArtifact]:
        """本任务产出的字幕 artifact。

        **这是"哪些文件是本任务的字幕"这个问题的唯一出口。** 角色在进入清单时已经
        判定过一次（`add_reported` 由 yt-dlp 的 `Writing video subtitles to:` 声明，
        `add_reconciled` 是 `reconcile()` 那一次集中兜底），这里只读不猜。

        返回 artifact 而不是路径，因为下游要的是 `id` —— 删除表达为
        `manifest.drop(id=...)`，不是 `os.remove(path)`。
        """
        manifest = self.manifest
        if manifest is None:
            return []
        return manifest.kept("subtitle")

    def subtitle_candidates(self) -> list[str]:
        """字幕路径 —— 只给 `SubtitleProcessor` 这个纯校验器用。"""
        return [art.path for art in self.subtitle_artifacts()]

    def primary_media(self) -> StagedArtifact | None:
        """主媒体 artifact。

        这里以前是 `find_final_merged_file()`：先按 `\\.f\\d+\\.` 正则判断当前
        `output_path` 是不是分片、再拼后缀探测合并结果、再翻 `dest_paths` 找第一个
        视频后缀、最后 `os.listdir` 整个目录按后缀猜一个。四层兜底每一层都在重新
        回答「哪个文件是主媒体」，而 `[Merger] Merging formats into` 那一行明明已经
        把答案写在 stdout 里了 —— 它只是从来没进过 `dest_paths`（Step 2 修好了）。

        Unicode 兜底（Windows stdout 丢 U+30FB 之类的字符）也不再需要在这里：
        `reconcile()` 扫 payload 时会把 yt-dlp 没报告成功的文件补录进清单，
        `primary` 由那一次集中判定给出。
        """
        manifest = self.manifest
        return manifest.primary_media() if manifest is not None else None

    # 这里以前有 `is_in_sandbox()`：它唯一的用途是给
    # `SubtitleProcessor.process(allow_dir_scan=...)` 开关"整目录扫描只在沙盒内安全"
    # 这条兜底。四级定位退休之后（《下载产物事务层》Step 3），字幕路径由清单直接交来，
    # `processing/` 侧不再有任何扫描动作，这个判断也就没有消费者了。
    #
    # 包含性判断本身没有消失，只是搬到了唯一该做它的地方：`staging.assert_inside()`
    # —— 那里用 `os.path.realpath` 先规范化（junction / symlink / 8.3），逃逸是硬失败
    # 而不是降级成 False。

    def emit_status(self, msg: str):
        if hasattr(self.worker, "_clean_logger"):
            pct = getattr(self.worker, "progress_val", 99.0)
            self.worker._clean_logger.force_update("processing", pct, msg)
        else:
            self.worker.status_msg.emit(msg)

    def emit_warning(self, msg: str):
        logger.warning(msg)
        self.emit_status(f"⚠️ {msg}")

    def emit_thumbnail_warning(self, msg: str):
        self.worker.thumbnail_embed_warning.emit(msg)
        self.emit_status(f"⚠️ {msg}")

    # 这里以前有 `find_final_merged_file()` 与 `find_thumbnail_file()`。
    #
    # 前者见 `primary_media()` 的说明。后者是「拿视频 stem 拼 `.jpg/.jpeg/.webp/.png`
    # 探测」——**同名不等于同一个任务的产物**：共享下载目录里别人的封面长得一模一样，
    # 而 `ThumbnailFeature` 拿到它之后是要嵌入并（旧代码里）删掉的。配对现在由
    # `ThumbnailFeature._locate_files()` 在清单内做，两边都来自同一份清单。


class DownloadFeature:
    """下载功能模块基类"""

    def configure(self, ydl_opts: dict[str, Any]) -> None:
        pass

    def on_download_start(self, context: DownloadContext) -> None:
        pass

    def on_post_process(self, context: DownloadContext) -> None:
        pass


class SponsorBlockFeature(DownloadFeature):
    def configure(self, ydl_opts: dict[str, Any]) -> None:
        if not config_manager.get("sponsorblock_enabled", False):
            return
        categories = config_manager.get(
            "sponsorblock_categories", ["sponsor", "selfpromo", "interaction"]
        )
        action = config_manager.get("sponsorblock_action", "remove")
        if not categories:
            return
        if action == "remove":
            ydl_opts["sponsorblock_remove"] = categories
        elif action == "mark":
            ydl_opts["sponsorblock_mark"] = categories
        logger.info(f"[SponsorBlock] Enabled: action={action}, categories={categories}")


class MetadataFeature(DownloadFeature):
    def configure(self, ydl_opts: dict[str, Any]) -> None:
        if config_manager.get("embed_metadata", True):
            pps = ydl_opts.setdefault("postprocessors", [])
            if not any(p.get("key") == "FFmpegMetadata" for p in pps):
                pps.append({"key": "FFmpegMetadata"})
            logger.info("[Metadata] Enabled")


class SubtitleFeature(DownloadFeature):
    def on_download_start(self, context: DownloadContext) -> None:
        opts = context.opts
        if opts.get("embedsubtitles"):
            fmt = (opts.get("merge_output_format") or "").lower()
            if fmt == "webm":
                opts["merge_output_format"] = "mkv"
                logger.info("[SubEmbed] WebM → MKV")
            elif not fmt:
                opts["merge_output_format"] = "mkv"
                logger.info("[SubEmbed] 未指定 → MKV")
            # `--embed-subs` 嵌入完就把外置字幕文件删了，于是 on_post_process 无从校验
            # 字幕到底下没下到。先用 `--keep-subs` 留住，校验完再清理（见下）。
            opts["keepsubtitles"] = True
        else:
            logger.warning("[SubEmbed] embedsubtitles=False")

    def on_post_process(self, context: DownloadContext) -> None:
        opts = context.opts
        # 只有在启用了字幕下载时才执行后处理
        if not opts.get("writesubtitles") and not opts.get("writeautomaticsub"):
            return

        from ..processing import subtitle_processor

        # 纠正 output_path：分片文件（如 .f136.mp4）在合并后已被删除，主媒体是哪个
        # 由清单回答（`[Merger] Merging formats into` 那一行的 `role="media"`）。
        primary = context.primary_media()
        if primary is not None:
            context.output_path = primary.path

        # 只取一次：下面的取舍要按 `id` 表达，路径只是给纯校验器看的。
        artifacts = context.subtitle_artifacts()

        try:
            result = subtitle_processor.process(
                output_path=context.output_path,
                # 本任务产出的字幕由**报告者声明**：executor 解析
                # `Writing video subtitles to:` 时就记下了精确路径和角色，
                # `reconcile()` 只对没报告到的做一次集中兜底。
                # processor 侧只做后缀过滤 + 存在性校验，不再有"找"这个动作。
                subtitle_paths=[art.path for art in artifacts],
                opts=opts,
                status_callback=context.emit_status,
            )

            if not result.success:
                # 字幕是 best-effort：任务照样算成功，但用户必须知道字幕去哪了。
                # 以前这里只有一行 logger.warning，UI 上什么都看不到。
                logger.warning(
                    "字幕后处理失败: {}（reason={}）",
                    result.message,
                    result.reason,
                )
                if result.reason == "not_found":
                    context.emit_warning(self._explain_missing(opts))
                elif result.reason == "all_invalid":
                    context.emit_warning(f"字幕文件校验失败：{result.message}")
                # `video_missing` 不再提示：视频本身没落盘时早有各自的失败提示，
                # 这里再冒一条字幕警告只会盖住真正的原因

            self._dispose_external_subtitles(context, artifacts, result)
        except Exception as e:
            logger.exception("字幕后处理异常: {}", e)

    @staticmethod
    def _dispose_external_subtitles(
        context: DownloadContext,
        artifacts: list[StagedArtifact],
        result: Any,
    ) -> None:
        """决定每条外置字幕的去向 —— **只改清单，不动文件**。

        物理删除只发生在沙盒 `rmtree`（硬约束 1），所以这里表达的是"不交付"而不是
        "删掉"。以前这个函数叫 `_cleanup_external_subtitles`，对
        `processed_files + invalid_files` 一律 `os.remove` —— 三个问题一次犯全：

        1. **校验失败的也删。** 坏文件恰恰是唯一能说明"字幕为什么不对"的证据；
        2. **不看嵌入是否真的成功。** 判据只是 `opts["embedsubtitles"]`（*请求过*
           嵌入），于是 ffmpeg 嵌入失败时外置文件也被删干净 —— 用户既没有字幕轨、
           也没有 `.srt`，两头空。现在的判据是
           `"subtitle" in manifest.embed_evidence`，那是
           `postprocessor_status == "finished"` 的结构化证据（Step 2 接线）；
        3. **表达不出"嵌入且保留外挂"。** 老模型里 `embed_type` 是 soft XOR
           external，四态只有两个能说。

        `keep` 缺省取 `True`（硬约束 2：标志缺失 ⇒ 保留）。
        `apply_subtitle_delivery()` 已处处写入该键，缺失只出现在遗留路径或测试中。
        """
        manifest = context.manifest
        if manifest is None or not artifacts:
            return

        opts = context.opts
        invalid = {os.path.normcase(p) for p, _ in getattr(result, "invalid_files", ())}
        embed_requested = bool(opts.get("embedsubtitles"))
        embed_done = "subtitle" in manifest.embed_evidence
        keep_external = bool(opts.get("__fluentytdl_keep_subtitle", True))

        dropped = 0
        for art in artifacts:
            if os.path.normcase(art.path) in invalid:
                # 留着：它是"字幕为什么不对"的唯一物证，随沙盒消失而不是被提前删掉
                manifest.mark_degraded(art.id, "integrity_failed")
                continue
            if not embed_requested:
                continue
            if not embed_done:
                manifest.mark_degraded(art.id, "embed_failed_fallback")
                continue
            if keep_external:
                continue
            manifest.drop(id=art.id, reason="embedded_successfully")
            dropped += 1

        if embed_requested and not embed_done:
            context.emit_warning("字幕嵌入未完成，已保留外置字幕文件")
        if dropped:
            logger.info("{} 条字幕已嵌入容器，不再单独交付", dropped)

    @staticmethod
    def _explain_missing(opts: dict[str, Any]) -> str:
        """一个字幕文件都没有时，说出**具体**原因。

        原因早就备在 `SUBTITLE_RESOLUTION_KEY` 里（`subtitle_service.build_resolution_meta`），
        只是从来没人读 —— 用户能看到的只有一句"未找到字幕文件"。
        """
        meta = opts.get(SUBTITLE_RESOLUTION_KEY) or {}
        mode = meta.get("mode")
        prefs = "、".join(meta.get("prefs") or []) or "默认语言"
        matched = "、".join(meta.get("matched") or [])
        available = [str(x) for x in (meta.get("available") or [])]
        avail_txt = "、".join(available[:6]) + ("…" if len(available) > 6 else "")

        if mode == "no_match":
            return f"字幕未命中任何可用语言（想要 {prefs}；该视频只有 {avail_txt or '无可用字幕'}）"
        if mode == "pattern":
            return f"未获取到字幕：按正则模式请求 {prefs}，yt-dlp 没有匹配到任何字幕轨道"
        if matched:
            return (
                f"未获取到字幕：已请求 {matched}，但一个文件都没写出（可能被限速或需要 PO Token）"
            )
        langs = "、".join(str(x) for x in (opts.get("subtitleslangs") or [])) or prefs
        return f"未获取到字幕：已请求 {langs}，但一个文件都没写出"


class ThumbnailFeature(DownloadFeature):
    def on_post_process(self, context: DownloadContext) -> None:
        primary = context.primary_media()
        if primary is not None:
            context.output_path = primary.path

        if context.opts.get("embedthumbnail") and context.opts.get("writethumbnail"):
            self._embed(context)

        # 交付取舍在最后统一做一次 —— 无论嵌入跑没跑、成没成
        self._dispose_thumbnails(context)

    def _embed(self, context: DownloadContext) -> None:
        pairs = self._locate_files(context)
        if not pairs:
            return
        if not thumbnail_embedder.is_available():
            context.emit_thumbnail_warning("⚠️ 封面嵌入工具不可用")
            return
        for video, thumb in pairs:
            self._process_single_file(context, video, thumb)

    def _process_single_file(
        self,
        context: DownloadContext,
        video: StagedArtifact,
        thumb: StagedArtifact,
    ) -> None:
        """把封面嵌进 `video` —— 嵌入器只产出候选，采纳由事务层做。

        以前是 `embed_thumbnail(video_path, thumb_path)` 直接改写主媒体：
        `thumbnail_embedder` 自己 `mkstemp(dir=video_path.parent)`（临时文件落在
        payload 里）、自己 `os.replace(temp, video)`、失败自己 `os.remove(temp)`。
        三件事都越过了事务层 —— 崩在中途就留下一个 `tmpXXXX.mp4`，而它一旦被下一个
        run 的 `reconcile()` 看到就成了清单里一个假的 media。

        现在：落点由 `reserve_workfile()` 给（在 `.work/` 里，`reconcile()` 永远看不到），
        采纳走 `replace_artifact_content()`（`id` 与计划中的 `final_path` 都不变，
        旧内容移进 `.internal/` 而不是被删）。原地改写型的后端（AtomicParsley
        `--overWrite` / mutagen `audio.save()`）拿到的 output 是先播种好的副本，
        源 artifact 全程只读。
        """
        staging = context.staging
        manifest = context.manifest
        if staging is None or manifest is None:
            return

        ext = os.path.splitext(video.path)[1].lower().lstrip(".")
        if not can_embed_thumbnail(ext):
            w = get_unsupported_formats_warning(ext)
            if w:
                context.emit_thumbnail_warning(w)
            return

        context.emit_status(f"[封面嵌入] 正在处理: {os.path.basename(video.path)}")
        tool = thumbnail_embedder.get_recommended_tool(ext)
        work = staging.reserve_workfile(
            os.path.splitext(os.path.basename(video.path))[0],
            os.path.splitext(video.path)[1],
            seed_from=video.id if tool.mutates_in_place else None,
        )
        res = thumbnail_embedder.embed_thumbnail(
            video.path,
            thumb.path,
            work,
            progress_callback=lambda msg: context.emit_status(f"[封面嵌入] {msg}"),
        )
        if res.success:
            staging.replace_artifact_content(video.id, work, producer="ThumbnailEmbedder")
            manifest.embed_evidence.add("thumbnail")
            context.emit_status("[封面嵌入] ✓ 成功")
        elif res.skipped:
            context.emit_thumbnail_warning(res.message)
        else:
            context.emit_thumbnail_warning(f"封面嵌入失败: {res.message}")
        # 失败时 workfile 从未登记，随沙盒 `rmtree` 消失；主媒体原样不动

    def _locate_files(
        self, context: DownloadContext
    ) -> list[tuple[StagedArtifact, StagedArtifact]]:
        """把媒体和封面按 stem 配对 —— **只在清单内**。

        这里以前有两层扫描：先拿 `output_path ∪ dest_paths` 的每个路径去
        `find_thumbnail_file()` 拼后缀探测，配不上再 `os.listdir` 整个目录按后缀
        分成两堆重新配一遍，配上还顺手把 `context.output_path` 改成扫出来的那个。
        那次 `listdir` 兜底存在的理由是 Windows stdout 会丢 U+30FB 之类的字符导致
        路径对不上 —— 而这件事现在由 `reconcile()` 一次性解决（它扫 payload 补录
        没报告到的文件），所以扫描没有理由再留在这里。改 `output_path` 也一并去掉：
        主媒体是谁由清单的 `primary` 回答，不是由配对的副产物指定。
        """
        manifest = context.manifest
        if manifest is None:
            return []

        media = manifest.kept("media")
        thumbs = manifest.kept("thumbnail")
        if not media or not thumbs:
            return []

        by_stem: dict[str, StagedArtifact] = {}
        for t in thumbs:
            by_stem.setdefault(os.path.splitext(os.path.basename(t.path))[0], t)

        pairs: list[tuple[StagedArtifact, StagedArtifact]] = []
        for v in media:
            stem = os.path.splitext(os.path.basename(v.path))[0]
            t = by_stem.get(stem)
            if t is None and len(media) == 1 and len(thumbs) == 1:
                # yt-dlp 会把封面写成 `<stem>.jpg`，但 VR 转码等环节可能已经换过
                # 媒体的名字。一媒体一封面时配对是无歧义的，不必因为 stem 不等就放弃。
                t = thumbs[0]
            if t is not None:
                pairs.append((v, t))
        return pairs

    @staticmethod
    def _dispose_thumbnails(context: DownloadContext) -> None:
        """决定封面是否交付 —— **只改清单**。

        这里以前是 `_cleanup_thumbnail_files()`：拿
        `output_path ∪ dest_paths` 的每个 stem 去拼 `.webp/.jpg/.jpeg/.png` 然后
        `os.remove`，**嵌入分支和不嵌入分支都跑**，挡在用户独立封面前面的只有
        `opts.get("__fluentytdl_keep_thumbnail")` 一个真值判断。那个键有 6 个设置点，
        其中一个藏在 `elif hasattr(self, "subtitle_check")` 里 —— 任何没有该控件的
        路径都不会设它，于是**键缺失 ⇒ 当作 False ⇒ 静默删掉用户要的封面**。
        更糟的是它按 stem 拼路径，删的可能压根不是本任务的文件。

        现在缺省是**保留**（硬约束 2：标志缺失 ⇒ 保留），删除表达为清单标记，
        而且只在"嵌入确实成功了"这一个理由下才不交付。
        """
        manifest = context.manifest
        if manifest is None:
            return
        opts = context.opts
        if opts.get("__fluentytdl_keep_thumbnail", True):
            return

        embed_requested = bool(opts.get("embedthumbnail"))
        if embed_requested and "thumbnail" not in manifest.embed_evidence:
            for art in manifest.kept("thumbnail"):
                manifest.mark_degraded(art.id, "embed_failed_fallback")
            return

        for art in manifest.kept("thumbnail"):
            manifest.drop(id=art.id, reason="embedded_successfully" if embed_requested else "not_requested")


class VRFeature(DownloadFeature):
    def on_post_process(self, context: DownloadContext) -> None:
        if not context.opts.get("__fluentytdl_use_android_vr"):
            return

        opts = context.opts
        proj = str(opts.get("__vr_projection") or "").lower()
        convert = bool(opts.get("__vr_convert_eac") or False)
        auto_convert = config_manager.get("vr_eac_auto_convert", False)

        if not (proj and proj != "unknown") and not ((convert or auto_convert) and proj == "eac"):
            if (convert or auto_convert) and proj == "mesh":
                context.emit_warning("Mesh 格式暂不支持转码")
            return

        staging = context.staging
        source = context.primary_media()
        if staging is None or source is None or not os.path.exists(source.path):
            logger.warning("[VR] 无法找到最终文件")
            return
        final_file = source.path
        context.output_path = final_file

        ffmpeg_exe = opts.get("ffmpeg_location") or "ffmpeg"
        if os.path.isdir(ffmpeg_exe):
            ffmpeg_exe = os.path.join(ffmpeg_exe, "ffmpeg.exe")

        needs_convert = (convert or auto_convert) and proj == "eac"
        if needs_convert:
            if not self._check_ffmpeg_v360(ffmpeg_exe):
                context.emit_warning("FFmpeg 不支持 v360")
                needs_convert = False

            h = int(opts.get("height") or 0)
            if h == 0:
                h = 2160
            if h > int(config_manager.get("vr_max_resolution", 2160)):
                context.emit_warning(f"跳过 VR 转码: 分辨率过高 ({h}p)")
                needs_convert = False

        target = source
        if needs_convert:
            context.emit_status("VR 投影转换 (EAC -> Equi)...")
            stem, ext = os.path.splitext(os.path.basename(final_file))
            work = staging.reserve_workfile(f"{stem}.equi", ext)

            cmd = self._build_cmd(ffmpeg_exe, final_file, work)
            dur = 0.0
            try:
                dur = float(opts.get("duration") or 0)
            except Exception:
                pass

            if self._run_ffmpeg(cmd, context, dur):
                target = self._adopt_transcode(staging, source, work, stem, ext)
                proj = "equirectangular"
            else:
                context.emit_warning("VR 转码失败")
                # workfile 从未登记，随沙盒 `rmtree` 消失 —— 这里不需要（也不许）动它

        self._inject_meta(context, target, proj, opts)
        context.output_path = target.path

    @staticmethod
    def _adopt_transcode(
        staging: StagingArea,
        source: StagedArtifact,
        work: str,
        stem: str,
        ext: str,
    ) -> StagedArtifact:
        """采纳 equi 产物 —— 全部经由 `StagingArea` 的原子 API。

        这里以前是三个裸 `os.replace`（更早是 `os.remove` 紧跟 `os.rename`，两行之间
        断电就等于用户的成品消失、转码结果挂在 `_equi` 这种中间名上）。问题不只是原子性：
        物理世界改了而清单不知道，于是 `build_plan()` 会去搬一个已经不在那儿的文件。

        **先用中间名登记，再腾位、再改名** —— 直接按 `{stem}{ext}` 登记会让
        `register_generated` 的 `os.replace` 当场覆盖掉还占着这个名字的源片。

        `vr_keep_source=False` 时源片也不立即删：`supersede_artifact()` 把它移进
        `.internal/`，转码产物在提交前万一被发现有问题它还在，而「物理删除只发生在
        `rmtree`」这条不变量不必为 VR 破例。
        """
        equi = staging.register_generated(
            work,
            kind="media",
            producer="VRFeature",
            parent_ids=(source.id,),
            target_name=f"{stem}.equi{ext}",
        )
        if config_manager.get("vr_keep_source", True):
            staging.rename_artifact(source.id, f"{stem}.eac{ext}")
        else:
            staging.supersede_artifact(source.id, equi.id, reason="vr_transcode")
        staging.rename_artifact(equi.id, f"{stem}{ext}")
        staging.manifest.promote(equi.id)
        return equi

    def _build_cmd(self, exe, inp, out):
        cmd = [exe, "-y", "-i", inp, "-vf", "v360=eac:e"]
        hw = config_manager.get("vr_hw_accel_mode", "auto")
        encs = hardware_manager.get_gpu_encoders()
        gpu = ""
        if hw in ("gpu", "auto") and encs:
            if "h264_nvenc" in encs:
                gpu = "h264_nvenc"
            elif "h264_qsv" in encs:
                gpu = "h264_qsv"
            elif "h264_amf" in encs:
                gpu = "h264_amf"

        if gpu:
            cmd.extend(["-c:v", gpu])
            if gpu == "h264_nvenc":
                cmd.extend(["-preset", "p4", "-cq", "20"])
            elif gpu == "h264_qsv":
                cmd.extend(["-global_quality", "20"])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
            th = hardware_manager.get_optimal_ffmpeg_threads(True)
            if config_manager.get("vr_cpu_priority", "low") == "low":
                th = max(1, th - 1)
            if th > 0:
                cmd.extend(["-threads", str(th)])

        cmd.extend(["-c:a", "copy", out])
        return cmd

    def _check_ffmpeg_v360(self, exe):
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            res = subprocess.run(
                [exe, "-filters"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=si,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return "v360" in res.stdout
        except Exception:
            return False

    def _run_ffmpeg(self, cmd, ctx, dur):
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                startupinfo=si,
                encoding="utf-8",
                errors="replace",
            )
            while True:
                if p.stdout is None:
                    break
                line = p.stdout.readline()
                if not line and p.poll() is not None:
                    break
                if line and "time=" in line:
                    t = line[line.find("time=") + 5 :].split(" ")[0]
                    ctx.emit_status(f"VR 转换... ({t})")
            return p.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _inject_meta(
        ctx: DownloadContext,
        art: StagedArtifact,
        proj: str,
        opts: dict[str, Any],
    ) -> None:
        """注入球面视频元数据 —— 换的是**同一个逻辑文件的内容**，所以走
        `replace_artifact_content()`（`id` 与计划中的 `final_path` 都不变）。

        这里以前是 `tmp = f + ".tmp.mp4"` 落在 payload 里，成功 `os.replace(tmp, f)`、
        失败 `os.remove(tmp)`。两个问题：临时文件在 payload 里，崩在中途就会被下一个
        run 的 `reconcile()` 当成一个假 media 补录进清单；而 `os.replace` 改了物理世界
        却没通知清单。现在落点在 `.work/`（`reconcile()` 看不到），采纳与回滚都在事务层。
        """
        staging = ctx.staging
        f = art.path
        if staging is None or os.path.splitext(f)[1].lower() not in (".mp4", ".mov"):
            return
        md = metadata_utils.Metadata()
        stereo = str(opts.get("__vr_stereo_mode") or "").lower()
        if stereo == "stereo_tb":
            md.stereo_mode = "top-bottom"
        elif stereo == "stereo_sbs":
            md.stereo_mode = "left-right"
        elif stereo == "mono":
            md.stereo_mode = "none"
        if proj == "equirectangular":
            md.projection = "equirectangular"

        if not md.stereo_mode and not md.projection:
            return

        ctx.emit_status("注入 VR 元数据...")
        stem, ext = os.path.splitext(os.path.basename(f))
        work = staging.reserve_workfile(f"{stem}.meta", ext)
        try:
            metadata_utils.inject_metadata(f, work, md, lambda x: None)
            if os.path.isfile(work):
                staging.replace_artifact_content(art.id, work, producer="VRFeature")
                ctx.emit_status("VR 元数据注入成功")
        except Exception:
            logger.exception("[VR] 元数据注入失败，保留原文件")
