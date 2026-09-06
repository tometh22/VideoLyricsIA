"""Loopback-only, read-only artifact server with byte ranges for reliable seeking."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # The terminal that launched this local viewer may close while the
        # user keeps reviewing. Default stderr logging can then raise a
        # BrokenPipeError before response headers, producing empty replies.
        # This read-only private viewer needs no per-request/access logging.
        pass

    def send_head(self):
        self.byte_range = None
        path = self.translate_path(self.path)
        if not Path(path).resolve().is_relative_to(Path(self.directory).resolve()):
            self.send_error(403)
            return None
        range_header = self.headers.get('Range')
        if range_header and os.path.isfile(path):
            size = os.path.getsize(path)
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
            if not match or not size:
                self.send_error(416)
                return None
            a, b = match.groups()
            start = int(a) if a else max(0, size - int(b or '0'))
            end = min(size - 1, int(b)) if a and b else size - 1
            if not 0 <= start <= end < size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.end_headers()
                return None
            handle = open(path, 'rb')
            handle.seek(start)
            self.byte_range = (start, end)
            self.send_response(206)
            self.send_header('Content-Type', self.guess_type(path))
            self.send_header('Content-Length', str(end-start+1))
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            return handle
        return super().send_head()

    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()

    def copyfile(self, source, outputfile):
        if not self.byte_range:
            return super().copyfile(source, outputfile)
        remaining = self.byte_range[1]-self.byte_range[0]+1
        while remaining:
            block = source.read(min(65536, remaining))
            if not block:
                break
            outputfile.write(block)
            remaining -= len(block)


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--port', type=int, default=8767)
    a=p.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',a.port), partial(Handler,directory=str(a.directory.resolve())))
    print(f'Private preview: http://127.0.0.1:{a.port}/',flush=True)
    server.serve_forever()
