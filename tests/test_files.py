import errno
import io
import json

import app as app_module


def upload(client, files, folder=""):
    data = {
        "folder": folder,
        "file": [(io.BytesIO(content), name) for name, content in files],
    }
    return client.post(
        "/files/upload", data=data, content_type="multipart/form-data"
    )


def trash_entries(storage):
    trash = storage / ".trash"
    return sorted(trash.iterdir()) if trash.exists() else []


# --- upload / download -----------------------------------------------------

def test_multi_upload_round_trips_bytes(client, storage):
    payloads = {"a.bin": bytes(range(256)) * 50, "b.txt": b"hello"}

    response = upload(client, list(payloads.items()))
    assert response.status_code == 302

    for name, content in payloads.items():
        assert (storage / name).read_bytes() == content
        downloaded = client.get(f"/files/download/{name}")
        assert downloaded.status_code == 200
        assert downloaded.data == content


def test_unicode_names_are_preserved(client, storage):
    names = ["照片.jpg", "ʻōlelo.txt", "résumé final.pdf"]

    upload(client, [(name, b"x") for name in names])

    for name in names:
        assert (storage / name).is_file()
        assert client.get(f"/files/download/{name}").status_code == 200


def test_upload_never_overwrites(client, storage):
    (storage / "keep.txt").write_bytes(b"original")

    upload(client, [("keep.txt", b"replacement"), ("new.txt", b"new")])

    assert (storage / "keep.txt").read_bytes() == b"original"
    assert (storage / "new.txt").read_bytes() == b"new"


def test_upload_rejects_dotfile_and_trash_names(client, storage):
    upload(client, [(".trash", b"x"), (".hidden", b"x")])

    assert not (storage / ".trash").exists()
    assert not (storage / ".hidden").exists()


def test_failed_upload_leaves_no_partial_file(client, storage, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(app_module.shutil, "copyfileobj", explode)

    upload(client, [("big.bin", b"data")])

    assert not (storage / "big.bin").exists()


def test_upload_into_subfolder(client, storage):
    (storage / "docs").mkdir()

    upload(client, [("note.txt", b"hi")], folder="docs")

    assert (storage / "docs" / "note.txt").read_bytes() == b"hi"


# --- delete (to trash) -----------------------------------------------------

def test_delete_moves_file_to_trash_with_metadata(client, storage):
    (storage / "docs").mkdir()
    (storage / "docs" / "report.txt").write_bytes(b"keep me")

    response = client.post("/files/delete", json={"path": "docs/report.txt"})

    assert response.status_code == 200
    assert not (storage / "docs" / "report.txt").exists()

    (entry,) = trash_entries(storage)
    assert (entry / "item").read_bytes() == b"keep me"

    meta = json.loads((entry / "meta.json").read_text())
    assert meta["original_path"] == "docs/report.txt"
    assert meta["name"] == "report.txt"


def test_delete_folder_keeps_contents(client, storage):
    (storage / "album").mkdir()
    (storage / "album" / "1.jpg").write_bytes(b"jpg")

    assert client.post("/files/delete", json={"path": "album"}).status_code == 200

    (entry,) = trash_entries(storage)
    assert (entry / "item" / "1.jpg").read_bytes() == b"jpg"


def test_delete_missing_item_is_404_and_leaves_no_trash_entry(client, storage):
    response = client.post("/files/delete", json={"path": "ghost.txt"})

    assert response.status_code == 404
    assert trash_entries(storage) == []


def test_trash_is_hidden_and_unreachable(client, storage):
    (storage / "a.txt").write_bytes(b"x")
    client.post("/files/delete", json={"path": "a.txt"})

    assert b".trash" not in client.get("/files").data
    assert client.get("/files?folder=.trash").status_code == 400
    assert client.post("/files/delete", json={"path": ".trash"}).status_code == 400
    assert client.post(
        "/files/move", json={"source": ".trash", "destination": ""}
    ).status_code == 400


# --- rename ----------------------------------------------------------------

def test_rename_file_and_folder(client, storage):
    (storage / "old.txt").write_bytes(b"x")
    (storage / "olddir").mkdir()

    assert client.post(
        "/files/rename", json={"path": "old.txt", "name": "新しい.txt"}
    ).status_code == 200
    assert client.post(
        "/files/rename", json={"path": "olddir", "name": "newdir"}
    ).status_code == 200

    assert (storage / "新しい.txt").is_file()
    assert (storage / "newdir").is_dir()
    assert not (storage / "old.txt").exists()


def test_rename_never_overwrites(client, storage):
    (storage / "a.txt").write_bytes(b"a")
    (storage / "b.txt").write_bytes(b"b")

    response = client.post("/files/rename", json={"path": "a.txt", "name": "b.txt"})

    assert response.status_code == 409
    assert (storage / "b.txt").read_bytes() == b"b"
    assert (storage / "a.txt").read_bytes() == b"a"


def test_rename_rejects_bad_names(client, storage):
    (storage / "a.txt").write_bytes(b"a")

    for bad in ["", "   ", ".hidden", "..", "x/y", "x\\y", "a\x00b", "a‮b", "n" * 300]:
        response = client.post("/files/rename", json={"path": "a.txt", "name": bad})
        assert response.status_code == 400, repr(bad)

    assert (storage / "a.txt").exists()


# --- move ------------------------------------------------------------------

def test_move_nested_and_error_cases(client, storage):
    (storage / "a").mkdir()
    (storage / "a" / "b").mkdir()
    (storage / "f.txt").write_bytes(b"f")

    ok = client.post("/files/move", json={"source": "f.txt", "destination": "a/b"})
    assert ok.status_code == 200
    assert (storage / "a" / "b" / "f.txt").is_file()

    into_self = client.post("/files/move", json={"source": "a", "destination": "a/b"})
    assert into_self.status_code == 400

    same_dir = client.post(
        "/files/move", json={"source": "a/b/f.txt", "destination": "a/b"}
    )
    assert same_dir.status_code == 400


def test_move_to_missing_destination_is_404_with_json_error(client, storage):
    (storage / "f.txt").write_bytes(b"f")

    response = client.post(
        "/files/move", json={"source": "f.txt", "destination": "nope"}
    )

    assert response.status_code == 404
    assert response.get_json()["error"]
    assert (storage / "f.txt").exists()


def test_move_never_overwrites(client, storage):
    (storage / "d").mkdir()
    (storage / "f.txt").write_bytes(b"new")
    (storage / "d" / "f.txt").write_bytes(b"old")

    response = client.post("/files/move", json={"source": "f.txt", "destination": "d"})

    assert response.status_code == 409
    assert (storage / "d" / "f.txt").read_bytes() == b"old"


# --- confinement / auth / mount -------------------------------------------

def test_traversal_attempts_are_rejected(client, storage):
    (storage.parent / "secret.txt").write_text("outside")

    assert client.get("/files?folder=..").status_code == 400
    assert client.get("/files?folder=../").status_code == 400
    assert client.get("/files?folder=/etc").status_code == 400
    assert client.get("/files/download/%2e%2e/secret.txt").status_code in (400, 404)

    for path in ["../secret.txt", "a/../../secret.txt", "/etc/passwd", "a//b"]:
        assert client.post("/files/delete", json={"path": path}).status_code == 400
        assert client.post(
            "/files/rename", json={"path": path, "name": "x"}
        ).status_code == 400


def test_symlink_pointing_outside_is_not_followed(client, storage):
    outside = storage.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    (storage / "zz_escape").symlink_to(outside)

    assert b"zz_escape" not in client.get("/files").data
    assert client.get("/files/download/zz_escape/secret.txt").status_code == 403
    assert client.post(
        "/files/delete", json={"path": "zz_escape/secret.txt"}
    ).status_code in (400, 404)
    assert (outside / "secret.txt").exists()


def test_anonymous_requests_are_redirected_to_login(anon_client, storage):
    (storage / "a.txt").write_bytes(b"a")

    for method, url, kwargs in [
        ("get", "/files", {}),
        ("get", "/files/download/a.txt", {}),
        ("post", "/files/delete", {"json": {"path": "a.txt"}}),
        ("post", "/files/rename", {"json": {"path": "a.txt", "name": "b"}}),
        ("post", "/files/move", {"json": {"source": "a.txt", "destination": ""}}),
        ("post", "/files/upload", {}),
    ]:
        response = getattr(anon_client, method)(url, **kwargs)
        assert response.status_code == 302, url
        assert "/login" in response.headers["Location"], url

    assert (storage / "a.txt").exists()


def test_everything_refuses_when_storage_is_not_mounted(client, storage, monkeypatch):
    monkeypatch.setattr(app_module, "storage_mounted", lambda: False)
    (storage / "a.txt").write_bytes(b"a")

    assert client.get("/files").status_code == 503
    assert upload(client, [("n.txt", b"x")]).status_code == 503
    assert client.post("/files/delete", json={"path": "a.txt"}).status_code == 503
    assert client.post(
        "/files/rename", json={"path": "a.txt", "name": "b.txt"}
    ).status_code == 503
    assert not (storage / "n.txt").exists()
    assert (storage / "a.txt").exists()
