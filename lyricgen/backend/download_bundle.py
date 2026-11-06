"""Disk-backed bundle delivery with cleanup after completion or interruption."""

import shutil

from starlette.responses import FileResponse


class TemporaryZipResponse(FileResponse):
    def __init__(self, path: str, *, temp_dir: str, filename: str):
        super().__init__(path, media_type="application/zip", filename=filename)
        self.temp_dir = temp_dir

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
