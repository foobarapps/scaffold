import asyncio
import hashlib
import time
from pathlib import Path

import aiofiles
from quart import url_for

# How often, at most, the static folder is checked for changes when auto refresh is on.
REFRESH_INTERVAL = 1.0


class Assets:
    def __init__(self, static_dir: Path, *, auto_refresh: bool = False) -> None:
        """
        `auto_refresh` re-reads the static folder when its files change, so an edit shows
        up without a restart. It is meant for development: the check runs on the event loop.
        """
        self.static_dir = static_dir
        self.auto_refresh = auto_refresh
        self.file_map: dict[str, str] = {}
        self.reverse_map: dict[str, str] = {}
        self._signature: tuple[int, int] | None = None
        self._last_refresh_check = 0.0
        self._loaded = False

    def _collect_files(self, static_dir: Path) -> list[Path]:
        return [file for file in static_dir.rglob("*") if file.is_file()]

    async def generate_hash(self, content: bytes) -> str:
        return self._hash(content)

    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.md5(content).hexdigest()  # noqa: S324

    def _hashed_path(self, file: Path, content: bytes) -> tuple[str, str]:
        """The path of a file relative to the static folder, plain and with the content hash in its name."""
        relative_path = file.relative_to(self.static_dir)
        # The directory structure is kept, only the file name changes
        hashed_path = relative_path.with_name(f"{file.stem}.{self._hash(content)}{file.suffix}")
        return str(relative_path), str(hashed_path)

    async def hash_file(self, file: Path) -> tuple[str, str]:
        async with aiofiles.open(file, "rb") as f:
            content = await f.read()
        return self._hashed_path(file, content)

    async def create_file_map(
        self,
        static_dir: Path,
    ) -> tuple[dict[str, str], dict[str, str]]:
        file_map: dict[str, str] = {}
        reverse_map: dict[str, str] = {}
        files = await asyncio.to_thread(self._collect_files, static_dir)
        hashed_files = await asyncio.gather(*(self.hash_file(file) for file in files))
        for original, hashed in hashed_files:
            file_map[original] = hashed
            reverse_map[hashed] = original
        return file_map, reverse_map

    async def update_file_maps(self) -> None:
        """Update the file mapping dictionaries."""
        self._signature = await asyncio.to_thread(self._read_signature)
        self.file_map, self.reverse_map = await self.create_file_map(self.static_dir)
        self._loaded = True

    def get_original_filename(self, hashed_filename: str) -> str:
        """Get the original filename from a hashed filename."""
        self._refresh_if_changed()
        return self.reverse_map.get(hashed_filename, hashed_filename)

    def get_hashed_filename(self, original_filename: str) -> str:
        """Get the hashed filename from an original filename."""
        self._refresh_if_changed()
        return self.file_map.get(original_filename, original_filename)

    def is_hashed_filename(self, filename: str) -> bool:
        """Tell whether the filename carries a content hash, so its content can never change."""
        self._refresh_if_changed()
        return filename in self.reverse_map

    def get_assets_by_pattern(self, pattern: str) -> list[str]:
        """Retrieve asset paths based on a pathlib glob pattern, sorted by path."""
        self._refresh_if_changed()
        return sorted(file for file in self.file_map if Path(file).match(pattern))

    def url_for(self, path: str) -> str:
        """The URL of an asset, with the content hash in it. Needs a request or app context."""
        return url_for("assets.asset", path=self[path])

    def read_manifest(self, path: str) -> list[str]:
        """
        The asset paths listed in a manifest file, in the order they are listed.

        A manifest is a text file in the static folder with one asset path per line, for
        assets that have to load in a given order (stylesheets, mostly). Blank lines and
        lines starting with # are skipped. A manifest that does not exist lists nothing.
        """
        manifest_path = self.static_dir / path
        if not manifest_path.is_file():
            return []

        lines = (line.strip() for line in manifest_path.read_text(encoding="utf-8").splitlines())
        return [line for line in lines if line and not line.startswith("#")]

    def _read_signature(self) -> tuple[int, int]:
        files = self._collect_files(self.static_dir)
        return len(files), max((file.stat().st_mtime_ns for file in files), default=0)

    def _refresh_if_changed(self) -> None:
        if not self._loaded:
            # Nothing has read the folder yet, e.g. in a test that never starts the server
            self._load()
            return

        if not self.auto_refresh:
            return

        now = time.monotonic()
        if now - self._last_refresh_check < REFRESH_INTERVAL:
            return
        self._last_refresh_check = now

        if self._read_signature() != self._signature:
            self._load()

    def _load(self) -> None:
        signature = self._read_signature()
        file_map: dict[str, str] = {}
        reverse_map: dict[str, str] = {}
        for file in self._collect_files(self.static_dir):
            original, hashed = self._hashed_path(file, file.read_bytes())
            file_map[original] = hashed
            reverse_map[hashed] = original

        self._signature = signature
        self.file_map, self.reverse_map = file_map, reverse_map
        self._last_refresh_check = time.monotonic()
        self._loaded = True

    def __getitem__(self, path: str) -> str:
        """Retrieve a hashed filename by the relative path."""
        self._refresh_if_changed()
        if path not in self.file_map:
            message = f"Asset not found for path: {path}"
            raise KeyError(message)
        return self.file_map[path]

    def __contains__(self, path: str) -> bool:
        """Check if an asset path exists."""
        self._refresh_if_changed()
        return path in self.file_map
