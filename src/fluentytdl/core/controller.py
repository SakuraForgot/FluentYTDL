import os
import time

from loguru import logger
from PySide6.QtCore import QObject, QThread, Signal

from ..download.download_manager import download_manager
from ..download.workers import DownloadWorker
from ..storage.task_db import task_db
from .config_manager import config_manager


class FileDeleteWorker(QThread):
    finished_signal = Signal(int, list)  # success_count, errors

    def __init__(self, paths_to_delete: list[str]):
        super().__init__()
        self.paths = paths_to_delete

    def run(self):
        success_count = 0
        errors = []
        for p in self.paths:
            deleted = False
            last_error = None
            for _ in range(3):
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                        deleted = True
                        break
                    elif os.path.isdir(p):
                        pass
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

    def handle_add_tasks(self, tasks: list[tuple[str, str, dict, str]]) -> list[DownloadWorker]:
        """
        Process the payload from the DownloadConfigWindow and inject it into the manager and DB.
        Returns the created workers so the UI model can bind to them.
        tasks payload: [(title, url, opts, thumb), ...]
        """
        created_workers = []
        default_dir = config_manager.get("download_dir")

        for _i, (t_title, t_url, t_opts, t_thumb) in enumerate(tasks):
            logger.info(f"[Controller] Creating worker for URL: {t_url}")

            if default_dir and "paths" not in t_opts:
                outtmpl = t_opts.get("outtmpl")
                if not (isinstance(outtmpl, str) and os.path.isabs(outtmpl)):
                    t_opts["paths"] = {"home": str(default_dir)}

            worker = download_manager.create_worker(
                t_url,
                t_opts,
                cached_info={"title": t_title, "thumbnail": str(t_thumb) if t_thumb else ""},
            )
            created_workers.append((worker, t_title, t_thumb))

            # Start immediately inside the controller policy
            download_manager.start_worker(worker)

        return created_workers

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

            if state in ("running", "queued", "paused", "downloading"):
                try:
                    worker.cancel()  # Auto-cleans `.part` via globs
                except Exception as e:
                    logger.error(f"Error stopping worker: {e}")

                if db_id:
                    task_db.delete_task(db_id)
                return

            if force_delete_files:
                final_path = getattr(worker, "output_path", getattr(worker, "_final_filepath", ""))

                sweep_list: set[str] = set()
                if final_path:
                    sweep_list.add(str(final_path))
                if hasattr(worker, "dest_paths"):
                    sweep_list.update(str(p) for p in worker.dest_paths)

                import glob
                import os

                extended_list = set(sweep_list)
                for p in sweep_list:
                    base_p = p
                    if p.endswith(".part"):
                        base_p = p[:-5]
                    elif p.endswith(".ytdl"):
                        base_p = p[:-5]

                    name_without_ext = os.path.splitext(base_p)[0]

                    try:
                        extended_list.update(glob.glob(f"{name_without_ext}*.part"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.ytdl"))
                        extended_list.update(glob.glob(f"{name_without_ext}.f*.*"))

                        extended_list.update(glob.glob(f"{name_without_ext}*.mp4"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.mkv"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.m4a"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.webm"))

                        extended_list.update(glob.glob(f"{name_without_ext}*.jpg"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.jpeg"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.webp"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.png"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.vtt"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.srt"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.ass"))
                        extended_list.update(glob.glob(f"{name_without_ext}*.lrc"))
                    except Exception:
                        pass

                valid_files = [f for f in extended_list if os.path.exists(f)]
                if valid_files:
                    self.delete_files_best_effort(valid_files, success_title="已删除文件残留")

            if db_id:
                task_db.delete_task(db_id)

        except Exception as e:
            logger.exception(f"Critical error in controller handle_remove_task: {e}")

    def handle_pause_resume_task(self, worker: DownloadWorker | None) -> DownloadWorker | None:
        """
        Handle play/pause states. If the task is dead/errored, it recreates a new worker.
        Returns the new worker if one was created, else None.
        """
        if not worker:
            return None

        if hasattr(worker, "is_paused") and worker.is_paused:
            worker.resume()
            if not worker.isRunning() and not worker.isFinished():
                download_manager.start_worker(worker)
        elif worker.isRunning():
            if hasattr(worker, "pause"):
                worker.pause()
            else:
                if hasattr(worker, "stop"):
                    worker.stop()
                elif hasattr(worker, "cancel"):
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
            new_worker = download_manager.create_worker(
                worker.url, worker.opts, cached_info=cached_meta, restore_db_id=old_db_id
            )
            download_manager.start_worker(new_worker)
            return new_worker

        return None


app_controller = AppController()
