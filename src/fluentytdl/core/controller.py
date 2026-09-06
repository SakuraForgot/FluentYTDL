import json
import os
import time
from typing import TYPE_CHECKING

from loguru import logger
from PySide6.QtCore import QObject, QThread, Signal

if TYPE_CHECKING:
    from ..models.quick_download_params import QuickDownloadParams

from ..download.download_manager import download_manager
from ..download.workers import DownloadWorker
from ..observability import FlowTrace, new_flow
from ..storage.task_db import task_db
from ..utils.aux_files import aux_files as _aux_files
from ..utils.quality_presets import current_preset_height, downgraded_opts, next_lower_height
from .config_manager import config_manager


class FileDeleteWorker(QThread):
    finished_signal = Signal(int, list)  # success_count, errors

    def __init__(self, paths_to_delete: list[str]):
        super().__init__()
        self.paths = paths_to_delete

    def run(self):
        import shutil

        success_count = 0
        errors = []
        for p in self.paths:
            deleted = False
            last_error = None
            for _ in range(5):
                try:
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                        except PermissionError:
                            import stat

                            os.chmod(p, stat.S_IWRITE)
                            os.remove(p)
                        deleted = True
                        break
                    elif os.path.isdir(p):
                        import stat

                        def remove_readonly(func, path, excinfo):
                            os.chmod(path, stat.S_IWRITE)
                            func(path)

                        shutil.rmtree(p, onerror=remove_readonly)
                        if not os.path.exists(p):
                            deleted = True
                            break
                        else:
                            raise Exception("文件夹删除残留")
                    else:
                        deleted = True
                        break
                except Exception as e:
                    last_error = e
                    time.sleep(0.5)

            if deleted:
                if os.path.basename(p) not in [".", ".."]:
                    success_count += 1

            elif last_error:
                errors.append(f"{os.path.basename(p)}: {last_error}")
        self.finished_signal.emit(success_count, errors)


class AppController(QObject):
    """
    The Global UI Controller (God-class Decoupler).
    Handles business logic bridging the View (MainWindow) and the low-level backend
    (download_manager, task_db). The View emits intent signals, and the Controller responds.
    """

    # Optional signals for async background ops the UI might want to know about
    files_deleted = Signal(int, list, str)  # success_count, errors, success_title

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._delete_workers: list[FileDeleteWorker] = []
        self._quick_workers: list[QThread] = []

    def handle_add_tasks(
        self,
        tasks: list[tuple[str, str, dict, str]],
        *,
        flow: FlowTrace | None = None,
    ) -> list[DownloadWorker]:
        """
        Process the payload from the DownloadConfigWindow and inject it into the manager and DB.
        Returns the created workers so the UI model can bind to them.
        tasks payload: [(title, url, opts, thumb), ...]

        `flow` 是发起解析的那个对话框的操作链标识。传进来，解析段（`flow=k72f task=-`）
        和这里创建的每个 task 才连成一条链；不传则每个 worker 自铸一条孤立的链，
        UI 时间线上就看不到"这个任务是从哪次解析来的"。播放列表一次产出几十个 task，
        它们**共享同一个 flow** —— 那正是 flow 这一级存在的理由。
        """
        created_workers = []
        default_dir = config_manager.get("download_dir")

        # 追踪当前已经存在的任务标题，以及在当前批次中即将添加的标题
        active_titles = set()
        for w in download_manager.active_workers:
            active_titles.add(w.cached_info.get("title", ""))
        for w in download_manager._pending_workers:
            active_titles.add(w.cached_info.get("title", ""))

        from fluentytdl.download.executor import _sanitize_filename

        def physical_exists(test_title: str, current_opts: dict) -> bool:
            sanitized = _sanitize_filename(test_title)
            target_dir = default_dir
            if (
                "paths" in current_opts
                and isinstance(current_opts["paths"], dict)
                and "home" in current_opts["paths"]
            ):
                target_dir = current_opts["paths"]["home"]
            if not target_dir or not os.path.exists(target_dir):
                return False
            try:
                for f in os.listdir(target_dir):
                    if f.startswith(sanitized):
                        return True
            except Exception:
                pass
            return False

        for _i, (t_title, t_url, t_opts, t_thumb) in enumerate(tasks):
            logger.info(f"[Controller] Creating worker for URL: {t_url}")

            if default_dir and "paths" not in t_opts:
                outtmpl = t_opts.get("outtmpl")
                if not (isinstance(outtmpl, str) and os.path.isabs(outtmpl)):
                    t_opts["paths"] = {"home": str(default_dir)}

            # === 全局防重名检查（打通物理层与视图层） ===
            original_title = t_title
            counter = 1

            # 如果在队列中已经存在，或者在物理目录中已经存在，则递增 (n)
            while t_title in active_titles or physical_exists(t_title, t_opts):
                t_title = f"{original_title} ({counter})"
                counter += 1

            active_titles.add(t_title)

            # 将重新推断出的完全唯一标题强制植入 yt-dlp 选项
            if (
                "outtmpl" in t_opts
                and isinstance(t_opts["outtmpl"], str)
                and "%(title)s" in t_opts["outtmpl"]
            ):
                # 覆盖原本的 outtmpl，将其中的 %(title)s 写死为新标题
                safe_title = _sanitize_filename(t_title)
                t_opts["outtmpl"] = t_opts["outtmpl"].replace("%(title)s", safe_title)

            worker = download_manager.create_worker(
                t_url,
                t_opts,
                cached_info={"title": t_title, "thumbnail": str(t_thumb) if t_thumb else ""},
                flow=flow,
            )
            created_workers.append((worker, t_title, t_thumb))

            # Start immediately inside the controller policy
            download_manager.start_worker(worker)

        return created_workers

    def handle_quick_add_tasks(
        self, urls: list[str], params: "QuickDownloadParams", callbacks: dict
    ) -> None:
        """
        处理快速下载添加请求，不阻塞主线程。
        callbacks 包含:
          - progress(msg: str)
          - finished(workers: list[DownloadWorker])
          - error(msg: str)
        """
        from .quick_add_worker import QuickAddWorker

        # 快速下载没有配置窗口，所以 flow 由本层铸造：一次「快速下载」点击 = 一条链，
        # 无论它展开成 1 个还是 500 个 task。
        flow = new_flow(stage="parse")
        worker = QuickAddWorker(urls, params, max_playlist_items=500, controller=self, flow=flow)

        def on_finished_tasks(tasks: list):
            # 将 tasks 交给 handle_add_tasks 处理并启动
            created = self.handle_add_tasks(tasks, flow=flow)
            if "finished" in callbacks:
                callbacks["finished"](created)
            if worker in self._quick_workers:
                self._quick_workers.remove(worker)

        def on_error(msg: str):
            if "error" in callbacks:
                callbacks["error"](msg)
            if worker in self._quick_workers:
                self._quick_workers.remove(worker)

        if "progress" in callbacks:
            worker.progress.connect(callbacks["progress"])

        worker.finished_tasks.connect(on_finished_tasks)
        worker.error.connect(on_error)

        self._quick_workers.append(worker)
        worker.start()

    def delete_files_best_effort(self, paths: list[str], success_title: str = "已删除文件") -> None:
        """Asynchronously delete files to avoid blocking UI thread."""
        if not paths:
            return

        worker = FileDeleteWorker(paths)

        def on_finished(scount: int, errs: list[str]):
            self.files_deleted.emit(scount, errs, success_title)
            if worker in self._delete_workers:
                self._delete_workers.remove(worker)

        worker.finished_signal.connect(on_finished)
        self._delete_workers.append(worker)
        worker.start()

    def handle_remove_task(
        self, worker: DownloadWorker | None, force_delete_files: bool = False
    ) -> None:
        """
        Handle all logic related to removing or cancelling a task.
        """
        if not worker:
            return

        try:
            db_id = getattr(worker, "db_id", 0)
            state = getattr(worker, "_final_state", "queued")
            if worker.isRunning():
                state = "running"

            if state in ("running", "queued", "paused", "quality_guard", "downloading", "parsing"):
                try:
                    download_manager.remove_worker(worker)
                    worker.cancel()  # Auto-cleans `.part` via globs
                except Exception as e:
                    logger.error(f"Error stopping worker: {e}")

                if force_delete_files:
                    self._do_force_delete_files(worker)

                if db_id:
                    task_db.delete_task(db_id)
                return

            if force_delete_files:
                self._do_force_delete_files(worker)

            if db_id:
                task_db.delete_task(db_id)

        except Exception as e:
            logger.exception(f"Critical error in controller handle_remove_task: {e}")

    def _do_force_delete_files(self, worker: DownloadWorker) -> None:
        final_path = getattr(worker, "output_path", getattr(worker, "_final_filepath", ""))
        import os

        # 如果任务还在沙盒里（未合并），直接删沙盒
        sandbox_dir = getattr(worker, "sandbox_dir", None)
        paths_to_delete = []

        if sandbox_dir and os.path.exists(sandbox_dir):
            paths_to_delete.append(sandbox_dir)

        # 收集最终上岸的文件
        if final_path and os.path.exists(str(final_path)):
            paths_to_delete.append(str(final_path))
            # 同时顺便删除同名的附属文件(字幕,封面等)
            paths_to_delete.extend(_aux_files(str(final_path)))

        # 针对播放列表多文件兜底
        if getattr(worker, "is_single_playlist", False) and getattr(worker, "download_dir", None):
            playlist_dir = worker.download_dir
            if playlist_dir and os.path.exists(playlist_dir):
                paths_to_delete.append(playlist_dir)

        # 对于未在最终路径的 dest_paths 进行兜底
        if hasattr(worker, "dest_paths"):
            for p in worker.dest_paths:
                if p and os.path.exists(str(p)) and str(p) not in paths_to_delete:
                    paths_to_delete.append(str(p))

        # 去重
        paths_to_delete = list(dict.fromkeys(paths_to_delete))

        if paths_to_delete:
            self.delete_files_best_effort(paths_to_delete, success_title="已删除文件残留")

    # === 无 worker 的历史行（融合后列表的另一半来源）===
    #
    # 上面所有 handle_* 都以 `DownloadWorker` 为入口，`if not worker: return`。
    # 分页从 `tasks` 表补进来的终态行没有 worker，走那条路等于「历史行删不掉、重下不了」。
    # 下面两个方法只依赖快照字段（`db_id` / `url` / `output_path`），不碰 download_manager
    # 的线程池 —— 没有线程在跑。
    #
    # 参数刻意是**朴素类型**而不是 `TaskRow`：`TaskRow` 住在 `ui/models/`，
    # 而 core/ 不许 import ui/（见 CLAUDE.md §2 分层）。

    def handle_remove_snapshots(
        self, rows: list[tuple[int, str]], force_delete_files: bool = False
    ) -> None:
        """删除若干条历史行。`rows` 是 `(db_id, output_path)` 序列。

        单行删除也走这里（传一个元素的列表）—— 文件删除是异步的，每条各起一个
        `FileDeleteWorker` 会在批量清空时开出几百个线程。
        """
        paths: list[str] = []
        db_ids: list[int] = []

        for raw_id, output_path in rows:
            db_id = int(raw_id or 0)
            if db_id > 0:
                db_ids.append(db_id)
            if not force_delete_files:
                continue
            final_path = str(output_path or "")
            if final_path and os.path.exists(final_path):
                paths.append(final_path)
                paths.extend(_aux_files(final_path))

        for db_id in dict.fromkeys(db_ids):
            try:
                task_db.delete_task(db_id)
            except Exception as e:
                logger.error(f"删除历史行 {db_id} 失败: {e}")

        paths = list(dict.fromkeys(paths))
        if paths:
            self.delete_files_best_effort(paths, success_title=f"已删除 {len(paths)} 个文件")

    def handle_start_snapshot(
        self, db_id: int, url: str, title: str = "", thumbnail: str = ""
    ) -> DownloadWorker | None:
        """把一条历史行「复活」成活任务；返回新 worker，失败返回 None。

        `TaskRow` 只保留渲染要用的字段，**没有 ydl_opts** —— 重下必须知道当初的参数
        （画质档位、输出目录、字幕设置），所以按 `db_id` 现取现读 `ydl_opts_json`。
        不预先塞进每一行：融合后列表可能上万行，而「重下」是罕见点击。

        `restore_db_id` 复用同一个 `tasks.id`，所以调用方拿到 worker 后
        `model.rebind_worker(row, worker)` 就是**就地升级**，不会多出一行。

        opts 读不出来时返回 None 而不是造一份默认参数：那会静默下成别的画质，
        调用方应该提示用户走「重新解析」。
        """
        if not url:
            return None

        opts: dict = {}
        db_row = task_db.get_task(int(db_id)) if db_id else None
        if db_row:
            try:
                parsed = json.loads(db_row.get("ydl_opts_json") or "{}")
                if isinstance(parsed, dict):
                    opts = parsed
            except (TypeError, ValueError):
                # 用户机器上的历史数据，别假设它一定是合法 JSON
                opts = {}
        if not opts:
            logger.warning(f"历史行 {db_id} 没有可用的 ydl_opts，无法直接重下")
            return None

        worker = download_manager.create_worker(
            url,
            opts,
            cached_info={"title": title, "thumbnail": thumbnail},
            restore_db_id=int(db_id or 0),
        )
        download_manager.start_worker(worker)
        return worker

    def task_opts(self, db_id: int, worker: DownloadWorker | None = None) -> dict:
        """取一个任务当初的 ydl_opts：活任务问 worker，历史行按 `db_id` 现读快照。

        和 `handle_start_snapshot` 同一个理由：`TaskRow` 不带 opts（融合后列表可能上万行），
        而需要 opts 的动作都是罕见点击。
        """
        if worker is not None:
            opts = getattr(worker, "opts", None)
            if isinstance(opts, dict) and opts:
                return dict(opts)

        db_row = task_db.get_task(int(db_id)) if db_id else None
        if not db_row:
            return {}
        try:
            parsed = json.loads(db_row.get("ydl_opts_json") or "{}")
        except (TypeError, ValueError):
            # 用户机器上的历史数据，别假设它一定是合法 JSON
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def handle_downgrade_quality(
        self,
        db_id: int,
        url: str,
        title: str = "",
        thumbnail: str = "",
        worker: DownloadWorker | None = None,
    ) -> tuple[DownloadWorker | None, str, int]:
        """把一个严格画质档位任务降一档重试。

        返回 `(新 worker, 原因码, 新档位)`。原因码给调用方做汇总提示用：

        * `""` —— 成功，`新 worker` 非 None，调用方必须 `rebind_worker`；
        * `"no_preset"` —— 不是严格档位任务（或 opts 读不出来），降档无从下手；
        * `"lowest"` —— 已经在阶梯最低档，只能手动调整格式；
        * `"failed"` —— 建 worker 失败。

        这是 `download_card._maybe_handle_format_unavailable` 的下半截（自动降档那一支）。
        上半截「弹窗问用户」留在 UI 侧：批量选中 50 行时只该弹一次，不该由这里决定。
        「手动调整」那一支对应右键菜单的「重新解析」，走
        `MainWindow.show_selection_dialog(url, smart_detect=True)`，不在这里重复实现。

        降档后的 opts 必须落库（`update_task_opts`）—— `create_worker` 走
        `restore_db_id` 时不写 `ydl_opts_json`，不落库的话重启后又会用回旧档位。
        """
        if not url:
            return None, "no_preset", 0

        opts = self.task_opts(db_id, worker)
        current_height = current_preset_height(opts)
        if not current_height:
            return None, "no_preset", 0

        next_height = next_lower_height(current_height)
        if next_height is None:
            return None, "lowest", current_height

        new_opts = downgraded_opts(opts, next_height)

        # 旧 worker 先摘掉：`error` 态的线程已经结束，但它还挂在 active_workers 里占额度
        if worker is not None:
            try:
                download_manager.remove_worker(worker)
            except Exception as e:
                logger.warning(f"降档前移除旧 worker 失败（继续）: {e}")

        try:
            new_worker = download_manager.create_worker(
                url,
                new_opts,
                cached_info={"title": title, "thumbnail": thumbnail},
                restore_db_id=int(db_id or 0),
            )
        except Exception as e:
            logger.error(f"降档重建 worker 失败: {e}")
            return None, "failed", next_height

        if db_id:
            task_db.update_task_opts(int(db_id), new_opts)

        download_manager.start_worker(new_worker)
        return new_worker, "", next_height

    def handle_pause_resume_task(self, worker: DownloadWorker | None) -> DownloadWorker | None:
        """
        Handle play/pause states. If the task is dead/errored, it recreates a new worker.
        Returns the new worker if one was created, else None.
        """
        if not worker:
            return None

        if hasattr(worker, "is_paused") and worker.is_paused:
            if worker.isFinished():
                # QThread 结束后不能重用，需要重建
                old_db_id = getattr(worker, "db_id", 0)
                cached_meta = {
                    "title": getattr(worker, "v_title", ""),
                    "thumbnail": getattr(worker, "v_thumbnail", ""),
                }
                new_worker = download_manager.create_worker(
                    worker.url, worker.opts, cached_info=cached_meta, restore_db_id=old_db_id
                )
                download_manager.start_worker(new_worker)
                return new_worker
            else:
                worker.resume()
                if not worker.isRunning():
                    download_manager.start_worker(worker)
        elif worker.isRunning():
            if hasattr(worker, "pause"):
                worker.pause()
            else:
                worker.cancel()
        elif not worker.isFinished():
            download_manager.start_worker(worker)
        else:
            # Dead/Cancel/Error state => Reconstruct worker
            old_db_id = getattr(worker, "db_id", 0)
            cached_meta = {
                "title": getattr(worker, "v_title", ""),
                "thumbnail": getattr(worker, "v_thumbnail", ""),
            }
            opts = worker.opts.copy()
            # 注：这里曾有一段 `if effective_state == "quality_warning": opts.pop(...)`，
            # 但 DownloadWorker.effective_state 只可能返回
            # running/paused/queued/completed/error/cancelled/quality_guard，
            # 从来不会是 "quality_warning" —— 该分支自始至终未执行过，已删除。
            # 「重启质量守卫任务时是否要绕过守卫（即 pop __fluentytdl_quality_intent）」
            # 是一个待定的 UX 决策，不要在这里悄悄改变行为。

            new_worker = download_manager.create_worker(
                worker.url, opts, cached_info=cached_meta, restore_db_id=old_db_id
            )
            download_manager.start_worker(new_worker)
            return new_worker

        return None

    # `handle_batch_start` 已删除：融合后「批量开始」必须同时处理活任务与历史行，
    # 而这个方法的入口是 `list[DownloadWorker]`，历史行根本传不进来。
    # 现在由 UI 侧的分块执行器逐行调 `handle_pause_resume_task` /
    # `handle_start_snapshot`，并在每块结束后 `download_manager.pump()`。

    def handle_batch_pause(self, workers: list[DownloadWorker]) -> None:
        """
        Explicitly pause or cancel only the tasks that are running or queued.
        """
        for worker in workers:
            if not worker:
                continue
            state = getattr(worker, "effective_state", "")
            if state in ("running", "queued"):
                if worker.isRunning():
                    if hasattr(worker, "pause"):
                        worker.pause()
                    else:
                        worker.cancel()
                else:
                    # Not running but queued (waiting in pool)
                    try:
                        download_manager.remove_worker(worker)
                        worker.cancel()
                    except Exception as e:
                        logger.error(f"Error cancelling queued worker in batch pause: {e}")

    def handle_batch_remove(
        self, workers: list[DownloadWorker], force_delete_files: bool = False
    ) -> None:
        """
        Handle removal of multiple tasks atomically.
        """
        paths_to_delete = []
        db_ids_to_delete = []

        for worker in workers:
            if not worker:
                continue

            try:
                db_id = getattr(worker, "db_id", 0)
                if db_id:
                    db_ids_to_delete.append(db_id)

                state = getattr(worker, "_final_state", "queued")
                if worker.isRunning():
                    state = "running"

                if state in ("running", "queued", "paused", "downloading", "parsing"):
                    try:
                        download_manager.remove_worker(worker)
                        worker.cancel()
                    except Exception as e:
                        logger.error(f"Error stopping worker in batch remove: {e}")

                if force_delete_files:
                    # Collect files to delete
                    final_path = getattr(
                        worker, "output_path", getattr(worker, "_final_filepath", "")
                    )
                    sandbox_dir = getattr(worker, "sandbox_dir", None)

                    if sandbox_dir and os.path.exists(sandbox_dir):
                        paths_to_delete.append(sandbox_dir)

                    if final_path and os.path.exists(str(final_path)):
                        paths_to_delete.append(str(final_path))
                        paths_to_delete.extend(_aux_files(str(final_path)))

                    if getattr(worker, "is_single_playlist", False) and getattr(
                        worker, "download_dir", None
                    ):
                        playlist_dir = worker.download_dir
                        if playlist_dir and os.path.exists(playlist_dir):
                            paths_to_delete.append(playlist_dir)

                    if hasattr(worker, "dest_paths"):
                        for p in worker.dest_paths:
                            if p and os.path.exists(str(p)) and str(p) not in paths_to_delete:
                                paths_to_delete.append(str(p))

            except Exception as e:
                logger.error(f"Error processing worker for batch remove: {e}")

        # Batch delete DB entries
        for db_id in set(db_ids_to_delete):
            try:
                task_db.delete_task(db_id)
            except Exception as e:
                logger.error(f"Error deleting db_id {db_id}: {e}")

        # Unique paths
        paths_to_delete = list(dict.fromkeys(paths_to_delete))
        if paths_to_delete:
            self.delete_files_best_effort(
                paths_to_delete, success_title=f"已清理 {len(paths_to_delete)} 个文件残留"
            )


app_controller = AppController()
