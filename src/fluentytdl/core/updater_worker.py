import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False


def print_message(msg_type: str, **kwargs):
    # msg_type: "progress", "done", "error", "status"
    msg = {"type": msg_type}
    msg.update(kwargs)
    print(json.dumps(msg), flush=True)


def build_opener(
    proxy_url: str | None = None, proxy_mode: str | None = None
) -> urllib.request.OpenerDirector:
    handlers = []
    if proxy_mode in ("http", "socks5") and proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
        handlers.append(urllib.request.ProxyHandler(proxies))

    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def kill_locking_processes(file_path: Path):
    if not HAS_PSUTIL:
        name = file_path.name.lower()
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception:
            pass
        return

    file_path_str = str(file_path.resolve()).lower()
    for proc in psutil.process_iter(["pid", "name", "open_files"]):
        try:
            open_files = proc.info.get("open_files")
            if open_files:
                for f in open_files:
                    if f.path and str(Path(f.path).resolve()).lower() == file_path_str:
                        proc.kill()
                        break

            try:
                exe = proc.exe()
                if exe and str(Path(exe).resolve()).lower() == file_path_str:
                    proc.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def safe_install(source_path: str | Path, target_path: Path):
    source_path = Path(source_path)
    if not target_path.exists():
        shutil.move(source_path, target_path)
        return

    kill_locking_processes(target_path)
    old_file = target_path.with_suffix(".exe.old")

    if old_file.exists():
        try:
            os.remove(old_file)
        except Exception:
            pass

    max_retries = 3
    for i in range(max_retries):
        try:
            target_path.replace(old_file)
            break
        except OSError:
            if i == max_retries - 1:
                pass
            else:
                time.sleep(1)
                kill_locking_processes(target_path)

    try:
        shutil.move(source_path, target_path)
    except OSError as e:
        if old_file.exists() and not target_path.exists():
            try:
                old_file.replace(target_path)
            except Exception:
                pass
        raise OSError(f"Failed to install new file to {target_path}. Error: {e}") from e

    if old_file.exists():
        try:
            os.remove(old_file)
        except Exception:
            pass


def handle_zip(zip_path, target_exe, dest_dir, extra_exes):
    with zipfile.ZipFile(zip_path, "r") as z:
        targets = [(target_exe.name, target_exe)]
        for extra in extra_exes:
            targets.append((extra, dest_dir / extra))

        for target_name, target_path in targets:
            found_member = None
            for name in z.namelist():
                if name.endswith(f"/{target_name}") or name == target_name:
                    found_member = name
                    break
            if not found_member:
                for name in z.namelist():
                    if name.lower().endswith(target_name.lower()):
                        found_member = name
                        break
            if not found_member:
                if target_name == target_exe.name:
                    raise FileNotFoundError(
                        f"Could not find {target_name} inside the downloaded archive."
                    )
                else:
                    continue

            with z.open(found_member) as source:
                fd, extracted_tmp_path = tempfile.mkstemp()
                os.close(fd)
                try:
                    with open(extracted_tmp_path, "wb") as target:
                        shutil.copyfileobj(source, target)
                    safe_install(extracted_tmp_path, target_path)
                finally:
                    if os.path.exists(extracted_tmp_path):
                        try:
                            os.remove(extracted_tmp_path)
                        except Exception:
                            pass


def run_worker():
    # Read config from stdin
    try:
        raw_input = sys.stdin.read()
        config = json.loads(raw_input)
    except Exception as e:
        print_message("error", msg=f"Invalid JSON input: {e}")
        return 1

    key = config.get("key")
    url = config.get("url")
    target_exe_str = config.get("target_exe")
    expected_version = config.get("expected_version", "")
    expected_channel = config.get("expected_channel", "")
    extra_exes = config.get("extra_exes", [])
    proxy_url = config.get("proxy_url")
    proxy_mode = config.get("proxy_mode")

    if not all([key, url, target_exe_str]):
        print_message("error", msg="Missing required parameters")
        return 1

    target_exe = Path(target_exe_str)
    tmp_path = None

    try:
        opener = build_opener(proxy_url, proxy_mode)
        req = urllib.request.Request(
            url, headers={"User-Agent": "FluentYTDL/DependencyManagerWorker"}
        )

        with opener.open(req, timeout=30) as r:
            total_length_str = r.headers.get("content-length")
            total_length = int(total_length_str) if total_length_str else 0

            fd, tmp_path = tempfile.mkstemp()
            os.close(fd)

            last_emit_time = 0
            last_emit_percent = -1

            with open(tmp_path, "wb") as f:
                downloaded = 0
                while True:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_length > 0:
                        percent = int(downloaded * 100 / total_length)
                        current_time = time.time()
                        if percent != last_emit_percent:
                            if (
                                (percent - last_emit_percent >= 1)
                                or (current_time - last_emit_time > 0.1)
                                or percent == 100
                            ):
                                print_message("progress", percent=percent)
                                last_emit_percent = percent
                                last_emit_time = current_time

        dest_dir = target_exe.parent
        dest_dir.mkdir(parents=True, exist_ok=True)

        print_message("status", msg="extracting")

        if url.endswith(".zip"):
            handle_zip(tmp_path, target_exe, dest_dir, extra_exes)
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        else:
            safe_install(tmp_path, target_exe)
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        if key == "yt-dlp":
            manifest_path = dest_dir / "manifest.json"
            try:
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump({"version": expected_version, "channel": expected_channel}, f)
            except Exception:
                pass

            try:
                for item in dest_dir.iterdir():
                    if item.name not in (target_exe.name, "manifest.json"):
                        if item.is_file():
                            item.unlink(missing_ok=True)
                        elif item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
            except Exception:
                pass

        print_message("done")
        return 0

    except Exception as e:
        print_message("error", msg=str(e))
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(run_worker())
