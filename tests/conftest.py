import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# The app reads its configuration at import time, so set it first.
_STORAGE = Path(tempfile.mkdtemp(prefix="elicloud-test-"))

os.environ.update({
    "ELICLOUD_SECRET_KEY": "test-secret-key",
    "ELICLOUD_ADMIN_HASH": "unused-in-tests",
    "ELICLOUD_STORAGE_MOUNT": str(_STORAGE),
    "ELICLOUD_LIMITER_URI": "memory://",
    "ELICLOUD_COOKIE_SECURE": "0",
})

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import app as app_module  # noqa: E402


@pytest.fixture
def storage(monkeypatch):
    uploads = _STORAGE / "uploads"
    shutil.rmtree(uploads, ignore_errors=True)
    uploads.mkdir(parents=True)
    # The temp directory is not a real mount point.
    monkeypatch.setattr(app_module, "storage_mounted", lambda: True)
    return uploads


@pytest.fixture
def client(storage):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with app_module.app.test_client() as test_client:
        with test_client.session_transaction() as session:
            session["admin_authenticated"] = True
        yield test_client


@pytest.fixture
def anon_client(storage):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with app_module.app.test_client() as test_client:
        yield test_client
