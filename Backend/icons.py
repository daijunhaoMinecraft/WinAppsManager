from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from hashlib import sha256
from mimetypes import guess_type
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


ICON_CACHE_CONTROL = "public, max-age=604800"


class IconService:
    def __init__(self) -> None:
        self._etags: dict[Path, tuple[int, int, str]] = {}

    def response(self, request: Request, path: Path) -> Response:
        stat = path.stat()
        cached = self._etags.get(path)
        if cached is None or cached[:2] != (stat.st_mtime_ns, stat.st_size):
            etag = '"' + sha256(path.read_bytes()).hexdigest() + '"'
            self._etags[path] = (stat.st_mtime_ns, stat.st_size, etag)
        else:
            etag = cached[2]

        headers = {
            "Cache-Control": ICON_CACHE_CONTROL,
            "ETag": etag,
            "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
        }
        if_none_match = request.headers.get("if-none-match")
        if if_none_match is not None:
            candidates = [candidate.strip().removeprefix("W/") for candidate in if_none_match.split(",")]
            if "*" in candidates or etag in candidates:
                return Response(status_code=304, headers=headers)
        else:
            if_modified_since = request.headers.get("if-modified-since")
            if if_modified_since:
                try:
                    modified_since = parsedate_to_datetime(if_modified_since)
                    if modified_since.tzinfo is None:
                        modified_since = modified_since.replace(tzinfo=timezone.utc)
                    if int(stat.st_mtime) <= int(modified_since.timestamp()):
                        return Response(status_code=304, headers=headers)
                except (ValueError, TypeError, OverflowError):
                    pass

        return FileResponse(path, media_type=guess_type(path.name)[0] or "application/octet-stream", headers=headers)


class CachedIconStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = ICON_CACHE_CONTROL
        return response
