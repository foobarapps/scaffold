import asyncio
import hashlib
from pathlib import Path

import aiofiles


class Assets:
    def __init__(self, static_dir: Path) -> None:
        self.static_dir = static_dir
        self.file_map: dict[str, str] = {}
        self.reverse_map: dict[str, str] = {}

    async def generate_hash(self, content: bytes) -> str:
        return hashlib.md5(content).hexdigest()  # noqa: S324

    async def hash_file(self, file: Path) -> tuple[str, str]:
        async with aiofiles.open(file, "rb") as f:
            content = await f.read()
        hashed_content = await self.generate_hash(content)
        hashed_file_name = file.with_stem(f"{file.stem}.{hashed_content}")
        # Maintain the directory structure in the hashed filename
        relative_path = file.relative_to(self.static_dir)
        hashed_path = relative_path.with_name(hashed_file_name.name)
        return str(relative_path), str(hashed_path)

    async def create_file_map(
        self,
        static_dir: Path,
    ) -> tuple[dict[str, str], dict[str, str]]:
        file_map: dict[str, str] = {}
        reverse_map: dict[str, str] = {}
        # Recursively gather all files in subdirectories
        files = list(
            static_dir.rglob("*"),
        )  # rglob('*') matches all files and folders recursively
        files = [file for file in files if file.is_file()]  # Filter out directories
        hashed_files = await asyncio.gather(*(self.hash_file(file) for file in files))
        for original, hashed in hashed_files:
            file_map[original] = hashed
            reverse_map[hashed] = original
        return file_map, reverse_map

    async def update_file_maps(self) -> None:
        """Update the file mapping dictionaries."""
        self.file_map, self.reverse_map = await self.create_file_map(self.static_dir)

    def get_original_filename(self, hashed_filename: str) -> str:
        """Get the original filename from a hashed filename."""
        return self.reverse_map.get(hashed_filename, hashed_filename)

    def get_hashed_filename(self, original_filename: str) -> str:
        """Get the hashed filename from an original filename."""
        return self.file_map.get(original_filename, original_filename)

    def get_assets_by_pattern(self, pattern: str) -> list[str]:
        """Retrieve asset paths based on a pathlib glob pattern."""
        return [file for file in self.file_map if Path(file).match(pattern)]

    def __getitem__(self, path: str) -> str:
        """Retrieve a hashed filename by the relative path."""
        if path not in self.file_map:
            message = f"Asset not found for path: {path}"
            raise KeyError(message)
        return self.file_map[path]

    def __contains__(self, path: str) -> bool:
        """Check if an asset path exists."""
        return path in self.file_map
