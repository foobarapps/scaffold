import os
from pathlib import Path

import pytest

from scaffold.web import BaseWebApp
from scaffold.web.assets import Assets

pytestmark = pytest.mark.asyncio


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "app.css").write_text("body { color: black; }")
    (tmp_path / "css" / "base.css").write_text("html { margin: 0; }")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "app.js").write_text("console.log('app');")
    return tmp_path


async def load_assets(static_dir: Path, *, auto_refresh: bool = False) -> Assets:
    assets = Assets(static_dir, auto_refresh=auto_refresh)
    await assets.update_file_maps()
    return assets


async def start(app: BaseWebApp) -> None:
    # Assets are read when the app starts serving
    await app.test_client().app.startup()  # type: ignore[attr-defined]


async def test_asset_name_carries_content_hash(static_dir: Path) -> None:
    assets = await load_assets(static_dir)

    hashed = assets["css/app.css"]

    assert hashed.startswith("css/app.")
    assert hashed.endswith(".css")
    assert hashed != "css/app.css"
    assert assets.get_original_filename(hashed) == "css/app.css"
    assert assets.is_hashed_filename(hashed)
    assert not assets.is_hashed_filename("css/app.css")


async def test_unknown_asset_raises(static_dir: Path) -> None:
    assets = await load_assets(static_dir)

    with pytest.raises(KeyError):
        assets["css/missing.css"]


async def test_assets_by_pattern_are_sorted(static_dir: Path) -> None:
    assets = await load_assets(static_dir)

    assert assets.get_assets_by_pattern("css/*.css") == ["css/app.css", "css/base.css"]


async def test_manifest_lists_assets_in_order(static_dir: Path) -> None:
    (static_dir / "css" / "manifest.txt").write_text("# base first\ncss/base.css\n\ncss/app.css\n")
    assets = await load_assets(static_dir)

    assert assets.read_manifest("css/manifest.txt") == ["css/base.css", "css/app.css"]


async def test_missing_manifest_lists_nothing(static_dir: Path) -> None:
    assets = await load_assets(static_dir)

    assert assets.read_manifest("css/manifest.txt") == []


async def test_changed_file_is_not_picked_up_by_default(static_dir: Path) -> None:
    assets = await load_assets(static_dir)
    hashed = assets["css/app.css"]

    (static_dir / "css" / "app.css").write_text("body { color: red; }")

    assert assets["css/app.css"] == hashed


async def test_changed_file_is_picked_up_with_auto_refresh(
    static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scaffold.web.assets.REFRESH_INTERVAL", 0.0)
    assets = await load_assets(static_dir, auto_refresh=True)
    hashed = assets["css/app.css"]

    file = static_dir / "css" / "app.css"
    file.write_text("body { color: red; }")
    # Some file systems keep modification times too coarse to tell the two writes apart
    os.utime(file, ns=(file.stat().st_atime_ns, file.stat().st_mtime_ns + 1_000_000_000))

    assert assets["css/app.css"] != hashed
    assert assets.get_original_filename(hashed) == hashed


async def test_new_file_is_picked_up_with_auto_refresh(static_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scaffold.web.assets.REFRESH_INTERVAL", 0.0)
    assets = await load_assets(static_dir, auto_refresh=True)

    (static_dir / "js" / "new.js").write_text("console.log('new');")

    assert "js/new.js" in assets


async def test_hashed_asset_is_served_as_immutable(static_dir: Path) -> None:
    app = BaseWebApp("__main__", static_folder=static_dir)
    await start(app)
    hashed = (await load_assets(static_dir))["css/app.css"]

    response = await app.test_client().get(f"/assets/{hashed}")

    assert response.status_code == 200
    assert await response.get_data(as_text=True) == "body { color: black; }"
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "ETag" in response.headers


async def test_plain_asset_has_to_be_revalidated(static_dir: Path) -> None:
    app = BaseWebApp("__main__", static_folder=static_dir)
    await start(app)

    response = await app.test_client().get("/assets/css/app.css")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=0, must-revalidate"


async def test_asset_is_not_sent_again_when_the_client_has_it(static_dir: Path) -> None:
    app = BaseWebApp("__main__", static_folder=static_dir)
    await start(app)
    client = app.test_client()
    etag = (await client.get("/assets/css/app.css")).headers["ETag"]

    response = await client.get("/assets/css/app.css", headers={"If-None-Match": etag})

    assert response.status_code == 304


@pytest.mark.parametrize("path", ["css/missing.css", "css", "x" * 5000 + ".css"])
async def test_missing_asset_is_not_found(static_dir: Path, path: str) -> None:
    app = BaseWebApp("__main__", static_folder=static_dir)
    await start(app)

    response = await app.test_client().get(f"/assets/{path}")

    assert response.status_code == 404


async def test_asset_url_carries_content_hash(static_dir: Path) -> None:
    app = BaseWebApp("__main__", static_folder=static_dir)
    await start(app)
    hashed = (await load_assets(static_dir))["js/app.js"]

    async with app.test_request_context("/"):
        url = (await load_assets(static_dir)).url_for("js/app.js")

    assert url == f"/assets/{hashed}"
