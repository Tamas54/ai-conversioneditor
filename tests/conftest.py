"""Közös tesztkörnyezet: ideiglenes adatkönyvtár, kikapcsolt előmelegítés, igény szerint ideiglenes Postgres.

A környezeti változókat az app importja ELŐTT kell beállítani (az app/config.py import-időben olvas).
A mintafájlokat a tests/samples.py generálja (nincs bináris fixture a repóban).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_DATA = tempfile.mkdtemp(prefix="aice-test-data-")
# helyi futtatás: ha a Playwright saját böngészője más verziójú, a gépen lévő headless shellt használjuk
if not os.getenv("CHROMIUM_EXECUTABLE"):
    import glob
    _shell = sorted(glob.glob("/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell"))
    if _shell:
        os.environ["CHROMIUM_EXECUTABLE"] = _shell[-1]
os.environ.update({
    "DATA_DIR": _DATA,
    "LO_WARM": "0",
    "CHROMIUM_WARM": "0",
    "DATABASE_URL": "",
    "DASHBOARD_PASSWORD": "",
    "MCP_AUTH_TOKEN": "",
    "INDEXING_OPEN": "",
    "PUBLIC_BASE_URL": "http://testserver",
    "SITE_LANGS": "en,hu",          # a nyelvi mechanizmus tesztjéhez a magyar is be (élesben alapból csak angol)
    "SILICONFLOW_API_KEY": "",
    "FILE_SIGNING_KEY": "test-signing-key",
    "VIDEO_PREVIEW_LIVE": "0",      # a régi előnézet-tesztek alakja; az élőé a saját tesztjében (test_live_wire.py)
})
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return Path(_DATA)


@pytest.fixture(scope="session")
def pg_url():
    """Ideiglenes Postgres a teszt-munkamenetre (TEST_DATABASE_URL felülírja)."""
    if os.getenv("TEST_DATABASE_URL"):
        yield os.environ["TEST_DATABASE_URL"]
        return
    from pgtemp import TempPostgres
    try:
        pg = TempPostgres()
        url = pg.start()
    except Exception as e:  # pragma: no cover - nincs Postgres a gépen
        pytest.skip(f"no PostgreSQL available: {e}")
    yield url
    pg.stop()


@pytest.fixture(scope="session")
def _app_client():
    """EGY TestClient a munkamenetre: az MCP session managere példányonként csak egyszer indítható."""
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    settings.DATABASE_URL = ""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client(_app_client):
    """Az app Postgres nélkül (lifespan: MCP session manager, worker pool előmelegítés nélkül)."""
    _app_client.cookies.clear()
    yield _app_client
    _app_client.cookies.clear()


@pytest.fixture()
def db_client(_app_client, pg_url):
    """Az app ideiglenes Postgres-szel. Az async DB-hívások az app saját loopjában: db_client.portal.call(fn, …)."""
    from app import db
    from app.config import settings
    settings.DATABASE_URL = pg_url
    _app_client.portal.call(db.start)
    _app_client.cookies.clear()
    try:
        yield _app_client
    finally:
        _app_client.portal.call(db.stop)
        settings.DATABASE_URL = ""
        _app_client.cookies.clear()


@pytest.fixture()
def samples(tmp_path):
    import samples as s
    return s.Samples(tmp_path)


@pytest.fixture()
def put_file():
    """Fájl a munkaterületre (admin: DATA_DIR/inputs) → file_id."""
    from app.storage import input_path, new_file_id

    def put(src: Path, ext: str | None = None) -> str:
        from app.storage import write_meta
        fid = new_file_id(ext or src.suffix.lstrip("."))
        input_path(fid).write_bytes(Path(src).read_bytes())
        write_meta(fid, original_filename=Path(src).name)
        return fid
    return put
