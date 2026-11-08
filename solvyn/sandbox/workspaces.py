"""Managed temporary workspaces with path and size confinement."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class WorkspaceError(ValueError):
    """Workspace identifier, path, or size violates the manager policy."""


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

_SEARCH_MAX_QUERY_CHARS = 200
_SEARCH_MAX_ENTRIES = 2_000
_SEARCH_MAX_FILES = 200
_SEARCH_MAX_FILE_BYTES = 128 * 1024
_SEARCH_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_SEARCH_MAX_RESULTS = 50
_SEARCH_MAX_SNIPPET_CHARS = 240
_SEARCH_MAX_OUTPUT_BYTES = 16 * 1024


def safe_relative_path(value: str) -> PurePosixPath:
    if not value or "\x00" in value or "\\" in value:
        raise WorkspaceError("Path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceError("Path must stay inside the workspace")
    for part in path.parts:
        if any(ord(char) < 32 or char in '<>:"|?*' for char in part) or part.endswith((" ", ".")):
            raise WorkspaceError("Path contains a platform-specific or reserved name")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise WorkspaceError("Path contains a reserved device name")
    return path


@dataclass(frozen=True)
class ManagedWorkspace:
    id: uuid.UUID
    path: Path


class WorkspaceManager:
    """Create and operate on directories below one dedicated workspace root."""

    def __init__(self, root: Path, *, max_bytes: int = 256 * 1024 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if root.is_symlink():
            raise WorkspaceError("Workspace root may not be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise WorkspaceError("Workspace root may not be a symlink")
        self.root = root.resolve(strict=True)
        self.max_bytes = max_bytes
        try:
            self.root.chmod(0o700)
        except OSError:
            # Windows ACLs, rather than POSIX mode bits, control access there.
            pass

    def create(self) -> ManagedWorkspace:
        workspace_id = uuid.uuid4()
        path = self.root / str(workspace_id)
        path.mkdir(mode=0o700)
        return ManagedWorkspace(workspace_id, path)

    def get(self, workspace_id: uuid.UUID) -> ManagedWorkspace:
        path = self.root / str(workspace_id)
        if path.is_symlink() or not path.is_dir():
            raise WorkspaceError("Workspace not found")
        resolved = path.resolve(strict=True)
        if resolved.parent != self.root:
            raise WorkspaceError("Workspace path escaped its managed root")
        return ManagedWorkspace(workspace_id, resolved)

    def write_file(self, workspace: ManagedWorkspace, relative_path: str, content: bytes) -> None:
        if len(content) > self.max_bytes:
            raise WorkspaceError("File exceeds the workspace size limit")
        target = self._resolve_path(workspace, relative_path)
        if target.is_symlink():
            raise WorkspaceError("Workspace paths may not target symlinks")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._check_tree(workspace)
        existing_size = target.stat().st_size if target.is_file() else 0
        if target.exists() and not target.is_file():
            raise WorkspaceError("A directory already exists at the file path")
        if self._tree_size(workspace) - existing_size + len(content) > self.max_bytes:
            raise WorkspaceError("Workspace exceeds the size limit")
        target.write_bytes(content)

    def read_file(self, workspace: ManagedWorkspace, relative_path: str, *, max_bytes: int = 1_000_000) -> bytes:
        target = self._resolve_path(workspace, relative_path)
        if target.is_symlink() or not target.is_file():
            raise WorkspaceError("File not found")
        if target.stat().st_size > max_bytes:
            raise WorkspaceError("File exceeds the read limit")
        return target.read_bytes()

    def delete_file(self, workspace: ManagedWorkspace, relative_path: str) -> None:
        """Delete one regular file without following links or removing directories."""
        target = self._resolve_path(workspace, relative_path)
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError as exc:
            raise WorkspaceError("File not found") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise WorkspaceError("Only regular workspace files can be deleted")
        target.unlink()

    def list_files(
        self,
        workspace: ManagedWorkspace,
        *,
        limit: int = 1000,
        exclude_git: bool = False,
    ) -> list[str]:
        self._check_tree(workspace)
        files = []
        for path in workspace.path.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace.path)
            if exclude_git and any(part.casefold() == ".git" for part in relative.parts):
                continue
            files.append(relative.as_posix())
        files.sort()
        return files[:limit]

    def search_text(self, workspace: ManagedWorkspace, query: str) -> dict[str, object]:
        """Search a bounded set of regular UTF-8 files using literal case-insensitive matching."""
        if not isinstance(query, str) or not query.strip() or len(query) > _SEARCH_MAX_QUERY_CHARS or "\x00" in query:
            raise WorkspaceError("Search query must be non-empty and at most 200 characters")
        current = self.get(workspace.id)
        if current.path != workspace.path:
            raise WorkspaceError("Workspace handle is invalid")
        files: list[str] = []
        pending_directories = [workspace.path]
        scanned_entries = 0
        truncated = False
        while pending_directories:
            directory = pending_directories.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if scanned_entries >= _SEARCH_MAX_ENTRIES:
                            truncated = True
                            break
                        scanned_entries += 1
                        if entry.name.casefold() == ".git":
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending_directories.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                files.append(Path(entry.path).relative_to(workspace.path).as_posix())
                        except OSError:
                            truncated = True
                            continue
                        if len(files) > _SEARCH_MAX_FILES:
                            truncated = True
                            break
            except OSError:
                truncated = True
                continue
            if truncated:
                break
        if len(files) > _SEARCH_MAX_FILES:
            truncated = True
        matches: list[dict[str, object]] = []
        scanned_files = 0
        scanned_bytes = 0
        skipped_files = 0
        needle = query.casefold()
        output_bytes = 0
        stop_search = False

        for relative_path in files[:_SEARCH_MAX_FILES]:
            scanned_files += 1
            try:
                path = self._resolve_path(workspace, relative_path)
            except WorkspaceError:
                skipped_files += 1
                truncated = True
                continue
            try:
                file_descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                skipped_files += 1
                truncated = True
                continue
            with os.fdopen(file_descriptor, "rb") as stream:
                file_mode = os.fstat(stream.fileno()).st_mode
                size = os.fstat(stream.fileno()).st_size
                if not stat.S_ISREG(file_mode):
                    skipped_files += 1
                    continue
                if size > _SEARCH_MAX_FILE_BYTES:
                    skipped_files += 1
                    continue
                if scanned_bytes + size > _SEARCH_MAX_TOTAL_BYTES:
                    truncated = True
                    break
                try:
                    content = stream.read(_SEARCH_MAX_FILE_BYTES + 1)
                except OSError:
                    skipped_files += 1
                    continue
            if len(content) > _SEARCH_MAX_FILE_BYTES:
                skipped_files += 1
                continue
            if scanned_bytes + len(content) > _SEARCH_MAX_TOTAL_BYTES:
                truncated = True
                break
            scanned_bytes += len(content)
            if b"\x00" in content:
                skipped_files += 1
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                skipped_files += 1
                continue

            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                if len(matches) >= _SEARCH_MAX_RESULTS:
                    truncated = True
                    stop_search = True
                    break
                start = max(0, line.casefold().find(needle) - (_SEARCH_MAX_SNIPPET_CHARS // 3))
                snippet = line[start : start + _SEARCH_MAX_SNIPPET_CHARS]
                if start > 0:
                    snippet = "…" + snippet
                if start + _SEARCH_MAX_SNIPPET_CHARS < len(line):
                    snippet += "…"
                match = {"path": relative_path, "line": line_number, "snippet": snippet}
                encoded_size = len(json.dumps(match, ensure_ascii=False).encode("utf-8"))
                # Reserve room for the response envelope, query, commas, and metadata.
                if output_bytes + encoded_size + 1 > _SEARCH_MAX_OUTPUT_BYTES - 1024:
                    truncated = True
                    stop_search = True
                    break
                matches.append(match)
                output_bytes += encoded_size
            if stop_search:
                break

        response: dict[str, object] = {
            "query": query,
            "matches": matches,
            "count": len(matches),
            "scanned_entries": scanned_entries,
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "skipped_files": skipped_files,
            "truncated": truncated,
            "content_trust": "untrusted_repository_data",
        }
        while len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > _SEARCH_MAX_OUTPUT_BYTES:
            matches.pop()
            response["count"] = len(matches)
            response["truncated"] = True
        return response

    def delete(self, workspace_id: uuid.UUID) -> None:
        workspace = self.get(workspace_id)
        shutil.rmtree(workspace.path)

    def validate_tree(self, workspace: ManagedWorkspace) -> int:
        """Reject links and return the regular-file bytes staged for execution."""
        self._check_tree(workspace)
        return self._tree_size(workspace)

    def fingerprint(self, workspace: ManagedWorkspace) -> str:
        """Return a stable SHA-256 digest of relative paths and file contents."""
        self._check_tree(workspace)
        digest = hashlib.sha256()
        for path in sorted(item for item in workspace.path.rglob("*") if item.is_file()):
            relative_path = path.relative_to(workspace.path).as_posix().encode("utf-8")
            digest.update(len(relative_path).to_bytes(8, "big"))
            digest.update(relative_path)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as content:
                for chunk in iter(lambda: content.read(64 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    def _resolve_path(self, workspace: ManagedWorkspace, relative_path: str) -> Path:
        current = self.get(workspace.id)
        if current.path != workspace.path:
            raise WorkspaceError("Workspace handle is invalid")
        posix_path = safe_relative_path(relative_path)
        target = workspace.path.joinpath(*posix_path.parts)
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(workspace.path):
            raise WorkspaceError("Path must stay inside the workspace")
        cursor = workspace.path
        for part in posix_path.parts[:-1]:
            cursor = cursor / part
            if cursor.is_symlink():
                raise WorkspaceError("Workspace paths may not traverse symlinks")
        return target

    def _check_tree(self, workspace: ManagedWorkspace) -> None:
        current = self.get(workspace.id)
        if current.path != workspace.path:
            raise WorkspaceError("Workspace handle is invalid")
        for base, directories, files in os.walk(workspace.path, followlinks=False):
            for name in [*directories, *files]:
                item = Path(base) / name
                mode = item.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise WorkspaceError("Workspace contains a symlink")
                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise WorkspaceError("Workspace contains an unsupported filesystem entry")

    def _tree_size(self, workspace: ManagedWorkspace) -> int:
        total = 0
        for path in workspace.path.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
                if total > self.max_bytes:
                    raise WorkspaceError("Workspace exceeds the size limit")
        return total
