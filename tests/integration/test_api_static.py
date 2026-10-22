"""The API serves the built web UI (`web/dist`) when there is one, and leaves /api alone.

Each test builds its own bundle in a temp dir: an `index.html` and one hashed script under
`assets/`, the shape `vite build` writes. No server and no network: the app is reached
through `httpx.ASGITransport`, as in the other API tests.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from freight_agent.api.app import create_app
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit

INDEX = "<!doctype html><title>Load agent workspace</title><div id=root></div>"
SCRIPT = "console.log('the platform UI');"
SECRET = "outside the bundle"


@pytest.fixture(autouse=True)
def no_web_dist_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WEB_DIST from the developer's shell must not leak into the tests."""
    monkeypatch.delenv("WEB_DIST", raising=False)


def write_bundle(root: Path) -> Path:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-3f9a1c.js").write_text(SCRIPT, encoding="utf-8")
    return root


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")  # next to the bundle
    return write_bundle(tmp_path / "dist")


@asynccontextmanager
async def client_for(web_dist: Path | None = None) -> AsyncIterator[httpx.AsyncClient]:
    """`web_dist=None` leaves the folder to the app: WEB_DIST, else web/dist."""
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    app = create_app(settings, web_dist=web_dist)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# --- with a bundle ----------------------------------------------------------------------


async def test_the_root_serves_the_index(dist: Path) -> None:
    async with client_for(web_dist=dist) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == INDEX


async def test_assets_are_served_under_assets(dist: Path) -> None:
    async with client_for(web_dist=dist) as client:
        response = await client.get("/assets/index-3f9a1c.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.text == SCRIPT


@pytest.mark.parametrize("path", ["/loads/481207", "/hero", "/evals/core?k=3", "/index"])
async def test_an_unknown_path_falls_back_to_the_index(dist: Path, path: str) -> None:
    """A deep link or a reload of a client-side route opens the app."""
    async with client_for(web_dist=dist) as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX


async def test_a_missing_asset_is_a_404_not_the_index(dist: Path) -> None:
    """A browser asking for an old hashed script must not get HTML back."""
    async with client_for(web_dist=dist) as client:
        response = await client.get("/assets/index-old.js")

    assert response.status_code == 404
    assert INDEX not in response.text


@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"]
)
async def test_a_file_outside_the_bundle_is_never_served(dist: Path, path: str) -> None:
    async with client_for(web_dist=dist) as client:
        response = await client.get(path)

    assert SECRET not in response.text


async def test_the_page_cannot_be_posted_to(dist: Path) -> None:
    async with client_for(web_dist=dist) as client:
        response = await client.post("/loads/481207")

    assert response.status_code == 405


# --- /api stays untouched ---------------------------------------------------------------

API_PROBES = [
    ("GET", "/api/health"),
    ("GET", "/api/health/"),
    ("GET", "/api"),
    ("GET", "/api/"),
    ("GET", "/api/nothing-here"),
    ("GET", "/api/loads/"),
    ("POST", "/api/nothing-here"),
    ("POST", "/api/health"),
    ("DELETE", "/api/loads"),
]


@pytest.mark.parametrize(("method", "path"), API_PROBES)
async def test_api_paths_answer_as_they_do_without_the_ui(
    tmp_path: Path, dist: Path, method: str, path: str
) -> None:
    """Unknown paths, wrong methods and trailing slashes under /api keep their own answer
    (404, 405, a redirect): the UI never swallows an API call."""
    async with client_for(web_dist=tmp_path / "no-bundle") as client:
        without_ui = await client.request(method, path)
    async with client_for(web_dist=dist) as client:
        with_ui = await client.request(method, path)

    assert with_ui.status_code == without_ui.status_code
    assert with_ui.headers.get("content-type") == without_ui.headers.get("content-type")
    assert with_ui.headers.get("location") == without_ui.headers.get("location")
    assert INDEX not in with_ui.text


async def test_the_api_docs_still_answer(dist: Path) -> None:
    async with client_for(web_dist=dist) as client:
        schema = await client.get("/openapi.json")

    assert schema.status_code == 200
    assert "/api/health" in schema.json()["paths"]


# --- without a bundle -------------------------------------------------------------------


@pytest.mark.parametrize("make", ["absent", "empty"])
async def test_without_a_bundle_only_the_api_answers(tmp_path: Path, make: str) -> None:
    """No folder, or a folder with no index.html (a build that has not run): as before."""
    folder = tmp_path / "dist"
    if make == "empty":
        folder.mkdir()
    async with client_for(web_dist=folder) as client:
        root = await client.get("/")
        deep = await client.get("/loads/481207")
        state = await client.get("/api/health")

    assert (root.status_code, deep.status_code) == (404, 404)
    assert root.json() == {"detail": "Not Found"}
    assert state.status_code == 200


# --- where the bundle is found ----------------------------------------------------------


async def test_the_bundle_folder_comes_from_web_dist(
    dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_DIST", str(dist))

    async with client_for() as client:
        response = await client.get("/")

    assert response.text == INDEX


async def test_the_default_is_web_dist_under_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_bundle(tmp_path / "web" / "dist")
    monkeypatch.chdir(tmp_path)

    async with client_for() as client:
        response = await client.get("/hero")

    assert response.text == INDEX
