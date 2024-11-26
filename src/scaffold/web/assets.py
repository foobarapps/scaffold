import asyncio
import hashlib
from pathlib import Path

import aiofiles
from quart import Blueprint, Quart, Response, send_from_directory, url_for


class Assets:
    def __init__(self, app: Quart | None = None) -> None:
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Quart) -> None:
        self.app = app

        setattr(app, "assets", self)
        app.before_serving(self.setup)
        app.context_processor(self.inject_assets)

        # Set up the blueprint for serving static files under '/assets'
        blueprint = Blueprint("assets", __name__)
        blueprint.add_url_rule(
            "/assets/<path:filename>",
            "serve_assets",
            self.serve_assets,
        )
        app.register_blueprint(blueprint)

    async def generate_hash(self, content: bytes) -> str:
        return hashlib.md5(content).hexdigest()  # noqa: S324

    async def hash_file(self, file: Path) -> tuple:
        async with aiofiles.open(file, "rb") as f:
            content = await f.read()
        hashed_content = await self.generate_hash(content)
        hashed_file_name = file.with_stem(f"{file.stem}.{hashed_content}")
        # Maintain the directory structure in the hashed filename
        relative_path = file.relative_to(self.static_dir)
        hashed_path = relative_path.with_name(hashed_file_name.name)
        return str(relative_path), str(hashed_path)

    async def create_file_map(self, static_dir: Path) -> tuple:
        file_map = {}
        reverse_map = {}
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

    async def setup(self) -> None:
        if self.app.static_folder is None:
            error_message = "A static folder has to be set"
            raise RuntimeError(error_message)

        self.static_dir = Path(self.app.static_folder)
        await self.update_file_maps()
        if self.app.debug:
            self.app.before_request(self.update_file_maps)

    async def update_file_maps(self) -> None:
        self.file_map, self.reverse_map = await self.create_file_map(self.static_dir)

    async def serve_assets(self, filename: str) -> Response:
        if self.app.static_folder is None:
            error_message = "A static folder has to be set"
            raise RuntimeError(error_message)

        original_filename = self.reverse_map.get(filename, filename)
        return await send_from_directory(self.app.static_folder, original_filename)

    def inject_assets(self) -> dict:
        return {"assets": self}

    def _url_for(self, path: str) -> str:
        return url_for("assets.serve_assets", filename=self.file_map[path])

    def get_assets_by_pattern(self, pattern: str) -> list:
        """Retrieve asset URLs based on a pathlib glob pattern."""
        return [self._url_for(file) for file in self.file_map if Path(file).match(pattern)]

    def __getitem__(self, path: str) -> str:
        """Retrieve a single asset URL by the relative path."""
        if path in self.file_map:
            return self._url_for(path)
        message = f"Asset not found for path: {path}"
        raise KeyError(message)
