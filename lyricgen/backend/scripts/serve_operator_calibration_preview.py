"""Loopback-only blind reviewer server with private on-demand audio clips."""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import re
from threading import Lock
from urllib.parse import urlsplit

from render_operator_calibration_clips import load_manifest, render_one
from serve_reviewer_shadow_preview import Handler as RangeHandler


class Handler(RangeHandler):
    def send_head(self):
        match = re.fullmatch(r"/clips/([0-9a-f]{64})\.mp3", urlsplit(self.path).path)
        if match:
            task = self.server.tasks.get(match.group(1))
            if task is None:
                self.send_error(404)
                return None
            clip = self.server.clip_dir / f"{task['task_id']}.mp3"
            if not clip.is_file() or clip.stat().st_size == 0:
                with self.server.render_lock:
                    if not clip.is_file() or clip.stat().st_size == 0:
                        try:
                            render_one(task, self.server.urls[task["job_id"]], self.server.clip_dir)
                        except Exception:
                            self.send_error(503, "Recorte no disponible")
                            return None
        return super().send_head()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--audio-urls", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if ".context" not in directory.parts:
        raise ValueError("preview_must_remain_in_context")
    tasks, urls, _ = load_manifest(args.queue, args.audio_urls)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), lambda *a, **kw: Handler(*a, directory=str(directory), **kw))
    server.tasks = {task["task_id"]: task for task in tasks}
    server.urls = urls
    server.clip_dir = directory / "clips"
    server.render_lock = Lock()
    print(f"Private calibration preview: http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()
