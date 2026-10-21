"""D-212: Resimlerim, the pictures a child made, on their own launcher.

The launcher serves files from the child's home to a browser, so most of what
is pinned here is what it must *not* serve: anything outside the child's
picture folders, Tux Paint's thumbnails, a hidden file, a link to somewhere
else, a file that is not a picture whatever it is called, an SVG, and any
picture at all to another website. And then that a child sees their own
pictures, newest first, big, and can page through them without a script.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from biokids_core.catalog import BUNDLED_CATALOG, load_catalog
from biokids_core.config import Config
from biokids_core.types import Reason
from biokids_shell import pictures
from biokids_shell.app import TILE_ICONS, create_app
from test_shell import LAUNCHER, FakeShellDaemon, _RecordingLauncher, a_state

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
DRAWINGS = {
    "slug": "drawings",
    "name": "Resimlerim",
    "icon": "gallery",
    "kind": "builtin",
    "url": "",
    "exec": "",
    "category": "free",
}


def write(path: Path, data: bytes, mtime: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def link(target: Path, at: Path) -> None:
    try:
        at.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot make symbolic links")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A child's home: Tux Paint's pictures and thumbnails, and a Pictures folder."""
    root = tmp_path / "home" / "elif"
    saved = root / ".tuxpaint" / "saved"
    write(saved / "20261020101500.png", PNG, 1_760_000_000)
    write(saved / "20261021093000.png", PNG, 1_760_100_000)
    write(saved / ".thumbs" / "20261021093000-t.png", PNG, 1_760_100_001)
    write(saved / "20261021093000.dat", b"label data", 1_760_100_002)
    write(root / "Resimler" / "kedim.jpg", JPEG, 1_760_050_000)
    write(root / "Resimler" / ".gizli.png", PNG, 1_760_200_000)
    write(root / "Resimler" / "logo.svg", SVG, 1_760_200_001)
    write(root / "Resimler" / "sahte.png", b"<html>not a picture</html>", 1_760_200_002)
    (root / "Resimler" / "klasör.png").mkdir()
    write(root / "Belgeler" / "ödev.png", PNG, 1_760_300_000)
    return root


def names(found: list[pictures.Picture]) -> list[str]:
    return [p.name for p in found]


# ------------------------------------------------------------ the folders


def test_the_childs_pictures_newest_first_and_nothing_else(home: Path) -> None:
    found = pictures.pictures(home, environ={})
    assert names(found) == ["20261021093000.png", "kedim.jpg", "20261020101500.png"], (
        "no thumbnail, no hidden file, no SVG, no fake, no folder, nothing from Belgeler"
    )
    assert [p.folder for p in found] == ["tuxpaint", "resimler", "tuxpaint"]
    assert [p.media_type for p in found] == ["image/png", "image/jpeg", "image/png"]


def test_the_pictures_folder_is_the_one_xdg_names(home: Path, tmp_path: Path) -> None:
    (home / ".config").mkdir()
    (home / ".config" / "user-dirs.dirs").write_text(
        '# written by xdg-user-dirs-update\nXDG_PICTURES_DIR="$HOME/Belgeler"\n',
        encoding="utf-8",
    )
    keys = [f.key for f in pictures.folders(home, environ={})]
    assert keys == ["tuxpaint", "xdg", "resimler"]
    assert "ödev.png" in names(pictures.pictures(home, environ={}))

    elsewhere = tmp_path / "config"
    elsewhere.mkdir()
    (elsewhere / "user-dirs.dirs").write_text('XDG_PICTURES_DIR="$HOME/"\n', encoding="utf-8")
    assert [f.key for f in pictures.folders(home, {"XDG_CONFIG_HOME": str(elsewhere)})] == [
        "tuxpaint",
        "resimler",
    ], "a Pictures folder set to the home itself would list the whole home"


def test_a_folder_named_twice_is_listed_once(home: Path) -> None:
    (home / ".config").mkdir()
    (home / ".config" / "user-dirs.dirs").write_text(
        'XDG_PICTURES_DIR="$HOME/Resimler"\n', encoding="utf-8"
    )
    assert [f.key for f in pictures.folders(home, environ={})] == ["tuxpaint", "xdg"]
    assert names(pictures.pictures(home, environ={})).count("kedim.jpg") == 1


def test_a_home_without_pictures_is_an_empty_grid(tmp_path: Path) -> None:
    assert pictures.folders(tmp_path / "nobody", environ={}) == []
    assert pictures.pictures(tmp_path, environ={}) == []


def test_no_link_leads_out_of_the_home(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "home" / "kerem" / "Pictures"
    write(other / "kerem.png", PNG, 1_760_400_000)
    link(other / "kerem.png", home / "Resimler" / "kardesim.png")
    link(other, home / "Pictures")
    found = pictures.pictures(home, environ={})
    assert "kardesim.png" not in names(found) and "kerem.png" not in names(found)
    assert pictures.find(home, "resimler", "kardesim.png", environ={}) is None
    assert pictures.find(home, "pictures", "kerem.png", environ={}) is None


def test_a_link_inside_the_home_is_followed_for_a_folder(home: Path) -> None:
    link(home / "Belgeler", home / "Pictures")
    assert "ödev.png" in names(pictures.pictures(home, environ={}))


@pytest.mark.parametrize(
    ("folder", "name"),
    [
        ("tuxpaint", "../../Belgeler/ödev.png"),
        ("tuxpaint", "..\\..\\Belgeler\\ödev.png"),
        ("tuxpaint", ".thumbs/20261021093000-t.png"),
        ("tuxpaint", "20261021093000.dat"),
        ("resimler", ".gizli.png"),
        ("resimler", "logo.svg"),
        ("resimler", "klasör.png"),
        ("belgeler", "ödev.png"),
        ("../Belgeler", "ödev.png"),
        ("tuxpaint", ""),
        ("tuxpaint", "a\x00.png"),
    ],
)
def test_only_a_plain_name_in_a_known_folder_resolves(
    home: Path, folder: str, name: str
) -> None:
    assert pictures.find(home, folder, name, environ={}) is None


def test_a_file_is_read_only_when_it_is_the_picture_it_says(home: Path) -> None:
    found = pictures.find(home, "tuxpaint", "20261021093000.png", environ={})
    assert found is not None and pictures.read(found) == PNG
    fake = pictures.find(home, "resimler", "sahte.png", environ={})
    assert fake is not None, "named like a picture"
    assert pictures.read(fake) is None, "but its bytes are not one"


def test_the_kinds_a_browser_shows_as_pictures() -> None:
    assert pictures.looks_like(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp")
    assert pictures.looks_like(b"GIF89a....", "image/gif")
    assert not pictures.looks_like(PNG, "image/jpeg")
    assert pictures.media_type("x.SVG") is None and pictures.media_type("x.PNG") == "image/png"


# ------------------------------------------------------------- the pages


def launcher(config: Config, home: Path, state: dict[str, Any] | None = None) -> TestClient:
    if state is None:
        state = a_state()
        state["tiles"] = [*state["tiles"], dict(DRAWINGS)]
    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(state)  # type: ignore[assignment]
    app.state.shell.launcher = _RecordingLauncher(config)
    app.state.shell.home = home
    return TestClient(app, base_url=LAUNCHER)


SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def test_the_tile_is_a_page_of_the_launcher(config: Config, home: Path) -> None:
    client = launcher(config, home)
    page = client.get("/").text
    tile = page.split('href="/pictures"')[1].split("</a>")[0]
    assert "Resimlerim" in tile and "🏞️" in tile
    assert "/launch/drawings" not in page, "no process to open, so no opening ring"

    answer = client.post("/launch/drawings")
    assert answer.json() == {"ok": True, "builtin": "drawings", "page": "/pictures"}
    form = client.post("/launch/drawings", data={"x": "1"}, follow_redirects=False)
    assert form.status_code == 303 and form.headers["location"] == "/pictures"
    screen = client.get("/launch/drawings", follow_redirects=False)
    assert screen.status_code == 303 and screen.headers["location"] == "/pictures"


def test_the_grid_shows_the_pictures_without_a_script(config: Config, home: Path) -> None:
    page = launcher(config, home).get("/pictures").text
    grid = page.split('class="pic-grid"')[1].split("</ul>")[0]
    assert grid.count('class="pic"') == 3
    assert grid.index("20261021093000.png") < grid.index("kedim.jpg")
    assert 'href="/pictures/view/tuxpaint/20261021093000.png"' in grid
    assert 'src="/pictures/file/resimler/kedim.jpg"' in grid
    assert 'loading="lazy"' in grid
    assert "Resimlerin bu bilgisayarda kalır, kimseye gönderilmez." in page
    assert 'href="/"' in page


def test_a_name_with_spaces_and_turkish_letters_survives_the_trip(
    config: Config, home: Path
) -> None:
    write(home / "Resimler" / "benim güzel evim #1.png", PNG, 1_760_500_000)
    client = launcher(config, home)
    grid = client.get("/pictures").text
    url = "/pictures/file/resimler/benim%20g%C3%BCzel%20evim%20%231.png"
    assert f'src="{url}"' in grid
    assert client.get(url, headers=SAME_ORIGIN).content == PNG


def test_an_empty_grid_says_where_pictures_come_from(config: Config, tmp_path: Path) -> None:
    page = launcher(config, tmp_path).get("/pictures").text
    assert "Henüz resim yok" in page
    assert str(escape("Boya'da bir resim yapınca burada görünecek.")) in page
    assert 'href="/launch/tuxpaint"' in page, "and a way to Boya, which this child has"
    assert "data-speak-on-load" in page, "said aloud for 4-7 (D-175)"


def test_the_viewer_pages_through_with_links(config: Config, home: Path) -> None:
    client = launcher(config, home)
    middle = client.get("/pictures/view/resimler/kedim.jpg").text
    assert 'src="/pictures/file/resimler/kedim.jpg"' in middle
    assert (
        'id="viewer-newer"\n       href="/pictures/view/tuxpaint/20261021093000.png"' in middle
    )
    assert (
        'id="viewer-older"\n       href="/pictures/view/tuxpaint/20261020101500.png"' in middle
    )
    assert 'id="viewer-close" href="/pictures"' in middle
    assert "2 / 3" in middle
    assert '<script src="/static/pictures.js' in middle

    newest = client.get("/pictures/view/tuxpaint/20261021093000.png").text
    assert 'id="viewer-newer"' not in newest and 'id="viewer-older"' in newest
    gone = client.get("/pictures/view/resimler/sahte.png", follow_redirects=False)
    assert gone.status_code == 303 and gone.headers["location"] == "/pictures"


def test_a_picture_is_served_with_its_type_and_for_the_launcher_alone(
    config: Config, home: Path
) -> None:
    response = launcher(config, home).get(
        "/pictures/file/tuxpaint/20261021093000.png", headers=SAME_ORIGIN
    )
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_another_website_gets_no_picture(config: Config, home: Path, site: str) -> None:
    response = launcher(config, home).get(
        "/pictures/file/tuxpaint/20261021093000.png", headers={"sec-fetch-site": site}
    )
    assert response.status_code == 403 and response.content == b""


@pytest.mark.parametrize(
    "path",
    [
        "/pictures/file/tuxpaint/..%2F..%2FBelgeler%2F%C3%B6dev.png",
        "/pictures/file/tuxpaint/%2E%2E/%2E%2E/Belgeler/%C3%B6dev.png",
        "/pictures/file/tuxpaint/.thumbs%2F20261021093000-t.png",
        "/pictures/file/resimler/logo.svg",
        "/pictures/file/resimler/sahte.png",
        "/pictures/file/belgeler/%C3%B6dev.png",
    ],
)
def test_nothing_else_is_served(config: Config, home: Path, path: str) -> None:
    response = launcher(config, home).get(path, headers=SAME_ORIGIN)
    assert response.status_code == 404
    assert b"PNG" not in response.content and b"svg" not in response.content


def test_without_the_tile_there_is_no_page_and_no_picture(config: Config, home: Path) -> None:
    client = launcher(config, home, a_state())
    for path in ("/pictures", "/pictures/view/resimler/kedim.jpg"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
    assert (
        client.get("/pictures/file/resimler/kedim.jpg", headers=SAME_ORIGIN).status_code == 404
    )
    assert 'href="/pictures"' not in client.get("/").text


def test_when_the_day_is_over_the_pictures_wait(config: Config, home: Path) -> None:
    state = a_state(allow=False, reason_code=Reason.QUOTA_EXHAUSTED)
    state["tiles"] = [*state["tiles"], dict(DRAWINGS)]
    response = launcher(config, home, state).get("/pictures", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


# ------------------------------------------------------------ the catalog


def test_resimlerim_is_a_built_in_tile_for_every_age() -> None:
    catalog = load_catalog(BUNDLED_CATALOG)
    entry = catalog.get("drawings")
    assert entry is not None and entry.kind == "builtin" and not entry.exec_argv
    assert (entry.name_tr, entry.name_en) == ("Resimlerim", "My pictures")
    assert entry.icon in TILE_ICONS and TILE_ICONS[entry.icon] == "🏞️"
    assert entry.default_category == "free"
    for age in range(4, 11):
        assert entry in catalog.suggested_for_age(age)
    assert "panel" in entry.description_en, "the parent is told the pictures stay put"
