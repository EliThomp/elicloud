from flask import (
    Flask, render_template, request,
    redirect, url_for, send_from_directory,
    abort, session, flash
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import psutil
import time
import socket
import subprocess
import ctypes
import errno
import stat
import shutil
import unicodedata
import uuid
from contextlib import contextmanager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# The app is only reachable through `tailscale serve`, which terminates HTTPS
# and proxies to 127.0.0.1:5000. Trust exactly one proxy hop so the client IP
# (used for rate limiting) and the scheme come from the forwarded headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Storage
STORAGE_MOUNT = Path(os.environ.get("ELICLOUD_STORAGE_MOUNT", "/mnt/elicloud"))
UPLOAD_FOLDER = STORAGE_MOUNT / "uploads"
TRASH_NAME = ".trash"
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
COPY_CHUNK = 1024 * 1024


def storage_mounted():
    return STORAGE_MOUNT.is_mount()


# Authentication configuration
app.config.update(
    SECRET_KEY=os.environ["ELICLOUD_SECRET_KEY"],
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure by default. Set ELICLOUD_COOKIE_SECURE=0 only for plain-HTTP
    # local testing.
    SESSION_COOKIE_SECURE=os.environ.get("ELICLOUD_COOKIE_SECURE", "1") != "0",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    WTF_CSRF_TIME_LIMIT=None,  # CSRF token lives as long as the session
    MAX_CONTENT_LENGTH=(
        int(os.environ.get("ELICLOUD_MAX_UPLOAD_MB", "100")) * 1024 * 1024
    ),
)

ADMIN_HASH = os.environ["ELICLOUD_ADMIN_HASH"]

csrf = CSRFProtect(app)
# Rate Limiting
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=os.environ.get(
        "ELICLOUD_LIMITER_URI", "redis://127.0.0.1:6379/0"
    ),
    default_limits=[]
)


@app.errorhandler(HTTPException)
def json_errors(error):
    # Fetch/XHR callers send JSON and get a readable message back.
    if request.is_json:
        return {"error": error.description}, error.code
    return error

# Tailscale Device Connection
def get_tailscale_devices():
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True
        )

        data = json.loads(result.stdout)
        devices = []

        for device in [data.get("Self", {}), *data.get("Peer", {}).values()]:
            if not device:
                continue

            addresses = device.get("TailscaleIPs", [])
            ipv4 = next(
                (ip for ip in addresses if "." in ip),
                "N/A"
            )

            devices.append({
                "name": device.get("HostName", "Unknown"),
                "ip": ipv4,
                "os": device.get("OS", "Unknown"),
                "online": device.get("Online", False),
                "active": device.get("Active", False)
            })

        return devices

    except (subprocess.SubprocessError, OSError, ValueError):
        return []


# No Rewrite Helper
_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = _libc.renameat2
_renameat2.argtypes = [
    ctypes.c_int, ctypes.c_char_p,
    ctypes.c_int, ctypes.c_char_p,
    ctypes.c_uint
]
_renameat2.restype = ctypes.c_int

RENAME_NOREPLACE = 1


def clean_name(raw, basename_only=False):
    """Validate a user-supplied file or folder name, keeping Unicode.

    Returns the normalized name or None. Names starting with "." are
    rejected so nobody can create, reach, or overwrite the .trash folder.
    """
    if not isinstance(raw, str):
        return None

    name = unicodedata.normalize("NFC", raw).strip()

    if basename_only:
        name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    elif "/" in name or "\\" in name:
        return None

    if (
        not name
        or name.startswith(".")
        or len(name.encode("utf-8")) > 255
        or any(unicodedata.category(ch).startswith("C") for ch in name)
    ):
        return None

    return name


def split_storage_path(value):
    if not isinstance(value, str) or not value:
        abort(400, description="Invalid storage path")

    if "\\" in value or "\x00" in value:
        abort(400, description="Invalid storage path")

    parts = value.split("/")

    if any(part == "" or part.startswith(".") for part in parts):
        abort(400, description="Invalid storage path")

    return parts


def rename_noreplace(src_fd, src_name, dst_fd, dst_name):
    result = _renameat2(
        src_fd, os.fsencode(src_name),
        dst_fd, os.fsencode(dst_name),
        RENAME_NOREPLACE
    )

    if result == 0:
        return

    error = ctypes.get_errno()

    if error == errno.EEXIST:
        abort(409, description="Destination already exists")

    if error == errno.ENOENT:
        abort(404, description="File or folder not found")

    if error in (errno.EACCES, errno.EPERM):
        abort(403, description="Permission denied")

    if error == errno.EINVAL:
        abort(400, description="Invalid folder move")

    if error == errno.EXDEV:
        abort(400, description="Cannot move across storage devices")

    raise OSError(error, os.strerror(error))


def require_item(dir_fd, name):
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        abort(404, description="Item not found")

    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        abort(400, description="Unsupported item type")


@contextmanager
def open_storage_directory(parts):
    if not storage_mounted():
        abort(503, description="NVMe storage unavailable")

    fd = os.open(UPLOAD_FOLDER, DIR_FLAGS)

    try:
        for part in parts:
            try:
                next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            except (FileNotFoundError, NotADirectoryError):
                abort(404, description="Folder not found")
            except OSError as error:
                if error.errno == errno.ELOOP:
                    abort(400, description="Invalid folder")
                raise

            os.close(fd)
            fd = next_fd

        yield fd

    finally:
        os.close(fd)

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if session.get("admin_authenticated"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        password = request.form.get("password", "")

        if check_password_hash(ADMIN_HASH, password):
            session.clear()
            session.permanent = True
            session["admin_authenticated"] = True
            return redirect(url_for("dashboard"))

        flash("Incorrect password.")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    return render_template("home.html")

#DASHBOARD
@app.route("/dashboard")
@login_required
def dashboard():
    tailscale_devices = get_tailscale_devices()

    # CPU Temperature
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            cpu_temp = round(int(f.read()) / 1000, 1)
    except Exception:
        cpu_temp = "N/A"

    # RAM
    memory = psutil.virtual_memory()
    ram_percent = memory.percent

    # Disk
    disk = psutil.disk_usage("/")
    disk_percent = disk.percent

    # NVMe SSD Storage
    try:
        nvme_path = str(STORAGE_MOUNT)

        if not os.path.ismount(nvme_path):
            raise OSError("NVMe SSD is not mounted")

        nvme = psutil.disk_usage(nvme_path)

        nvme_total = round(nvme.total / (1024 ** 3), 1)
        nvme_used = round(nvme.used / (1024 ** 3), 1)
        nvme_free = round(nvme.free / (1024 ** 3), 1)
        nvme_percent = nvme.percent

    except OSError:
        nvme_total = "N/A"
        nvme_used = "N/A"
        nvme_free = "N/A"
        nvme_percent = 0

    #Uptime
    uptime_seconds = time.time() - psutil.boot_time()
    uptime_hours = round(uptime_seconds / 3600, 1)

    #----Network Information

    # EliCloud LAN IP
    lan_ip = "192.168.4.1"

    #Internet Connectivity
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2)
        internet_status = "Online"
    except OSError:
        internet_status = "Offline"

    # IP Forwarding
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "r") as f:
            forwarding_status = (
                "Enabled" if f.read().strip() == "1" else "Disabled"
            )
    except Exception:
        forwarding_status = "Unknown"

    # Connected Wi-Fi Clients
    try:
        result = subprocess.run(
            ["iw", "dev", "wlan0", "station", "dump"],
            capture_output=True,
            text=True,
            timeout=2
        )

        connected_clients = result.stdout.count("Station ")
    except Exception:
        connected_clients = "N/A"

    #----Connected Device Details

    devices = []

    try:
        # Read DHCP leases
        leases = {}

        with open("/var/lib/misc/dnsmasq.leases", "r") as f:
            for line in f:
                parts = line.split()

                if len(parts) >= 4:
                    mac = parts[1].lower()
                    ip = parts[2]
                    hostname = parts[3]

                    leases[mac] = {
                        "ip": ip,
                        "hostname": hostname
                    }

        # Get actively connected Wi-Fi stations
        result = subprocess.run(
            ["iw", "dev", "wlan0", "station", "dump"],
            capture_output=True,
            text=True,
            timeout=2
        )

        current_mac = None

        for line in result.stdout.splitlines():

            line = line.strip()

            if line.startswith("Station "):
                current_mac = line.split()[1].lower()

                lease = leases.get(current_mac, {})

                devices.append({
                    "mac": current_mac,
                    "ip": lease.get("ip", "Unknown"),
                    "hostname": lease.get("hostname", "Unknown"),
                    "signal": "N/A"
                })

            elif line.startswith("signal:") and current_mac:

                signal = line.split(":", 1)[1].strip()

                devices[-1]["signal"] = signal

    except Exception as e:
        print("Device parsing error:", e)



    return render_template(
        "dashboard.html",

        # System
        cpu_temp=cpu_temp,
        ram_percent=ram_percent,
        disk_percent=disk_percent,
        uptime_hours=uptime_hours,

        # NVMe Storage
        nvme_total=nvme_total,
        nvme_used=nvme_used,
        nvme_free=nvme_free,
        nvme_percent=nvme_percent,

        # Network
        lan_ip=lan_ip,
        internet_status=internet_status,
        connected_clients=connected_clients,
        forwarding_status=forwarding_status,
        tailscale_devices=tailscale_devices,

        # Devices
        devices=devices
    )

def get_storage_path(folder=""):
    if not storage_mounted():
        abort(503, description="NVMe storage is not mounted")

    root = UPLOAD_FOLDER.resolve()

    if not root.is_dir():
        abort(503, description="EliCloud storage unavailable")

    if not isinstance(folder, str):
        abort(400, description="Invalid folder path")

    relative = Path(folder)

    if relative.is_absolute() or any(
        part == ".." or part.startswith(".")
        for part in relative.parts
    ):
        abort(400, description="Invalid folder path")

    destination = (root / relative).resolve()

    if not destination.is_relative_to(root):
        abort(403, description="Access outside storage is forbidden")

    return destination


def save_upload(upload, destination):
    """Write one upload without overwriting. Returns None or a reason."""
    try:
        fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        return "already exists"
    except OSError:
        return "could not be created"

    try:
        with os.fdopen(fd, "wb") as output:
            shutil.copyfileobj(upload.stream, output, COPY_CHUNK)
    except Exception as error:
        # Never leave a partial file behind (disk full, client disconnect).
        try:
            destination.unlink()
        except OSError:
            pass

        if isinstance(error, OSError):
            if error.errno == errno.ENOSPC:
                return "disk is full"
            return "could not be written"

        raise

    return None


#FILES
@app.route("/files")
@login_required
def files():
    current_folder = request.args.get("folder", "")
    current_path = get_storage_path(current_folder)
    root = UPLOAD_FOLDER.resolve()

    if not current_path.is_dir():
        abort(404, description="Folder not found")

    folders = []
    stored_files = []

    for item in sorted(
        current_path.iterdir(),
        key=lambda p: p.name.lower()
    ):
        if item.name.startswith(".") or item.is_symlink():
            continue

        if item.is_dir():
            folders.append({
                "name": item.name,
                "path": item.relative_to(root).as_posix()
            })

        elif item.is_file():
            stored_files.append({
                "name": item.name,
                "size": round(
                    item.stat().st_size / (1024 * 1024), 2
                ),
                "path": item.relative_to(root).as_posix()
            })

    parent_folder = ""

    if current_folder:
        parent_folder = Path(current_folder).parent.as_posix()

        if parent_folder == ".":
            parent_folder = ""

    return render_template(
        "files.html",
        files=stored_files,
        folders=folders,
        current_folder=current_folder,
        parent_folder=parent_folder,
        max_upload_mb=app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    )

@app.route("/files/folder/create", methods=["POST"])
@login_required
def create_folder():
    current_folder = request.form.get("folder", "")
    folder_name = clean_name(request.form.get("folder_name", ""))

    parent = get_storage_path(current_folder)

    if not parent.is_dir():
        abort(404, description="Parent folder not found")

    if folder_name is None:
        flash("That folder name isn't allowed.")
    else:
        try:
            (parent / folder_name).mkdir(mode=0o700)
        except FileExistsError:
            flash(f"\u201c{folder_name}\u201d already exists.")

    return redirect(url_for("files", folder=current_folder))


@app.route("/files/upload", methods=["POST"])
@login_required
def upload_file():
    if not storage_mounted():
        abort(503, description="NVMe storage is not mounted")

    uploads = [
        item for item in request.files.getlist("file")
        if item and item.filename
    ]

    if not uploads:
        abort(400, description="No file provided")

    current_folder = request.form.get("folder", "")
    current_path = get_storage_path(current_folder)

    if not current_path.is_dir():
        abort(404, description="Destination folder not found")

    saved = 0

    for upload in uploads:
        name = clean_name(upload.filename, basename_only=True)

        if name is None:
            flash(f"Skipped \u201c{upload.filename}\u201d: name not allowed.")
            continue

        problem = save_upload(upload, current_path / name)

        if problem:
            flash(f"Skipped \u201c{name}\u201d: {problem}.")
        else:
            saved += 1

    if saved:
        flash(f"Uploaded {saved} file{'s' if saved != 1 else ''}.")

    return redirect(url_for("files", folder=current_folder))

@app.route("/files/download/<path:filename>")
@login_required
def download_file(filename):
    requested_path = get_storage_path(filename)

    if not requested_path.is_file():
        abort(404, description="File not found")

    root = UPLOAD_FOLDER.resolve()

    return send_from_directory(
        root,
        requested_path.relative_to(root).as_posix(),
        as_attachment=True
    )

@app.route("/files/move", methods=["POST"])
@login_required
def move_file():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        abort(400, description="Invalid JSON")

    source_path = data.get("source")
    destination_folder = data.get("destination")

    if not isinstance(destination_folder, str):
        abort(400, description="Invalid destination")

    source_parts = split_storage_path(source_path)
    destination_parts = (
        split_storage_path(destination_folder)
        if destination_folder else []
    )

    # Prevent moving a folder into itself or a descendant.
    if destination_parts[:len(source_parts)] == source_parts:
        abort(400, description="Cannot move an item into itself")

    if source_parts[:-1] == destination_parts:
        abort(400, description="Item is already in this folder")

    with open_storage_directory(source_parts[:-1]) as source_fd:
        with open_storage_directory(destination_parts) as dest_fd:
            name = source_parts[-1]
            require_item(source_fd, name)
            rename_noreplace(source_fd, name, dest_fd, name)

    return {"success": True}


@app.route("/files/rename", methods=["POST"])
@login_required
def rename_item():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        abort(400, description="Invalid JSON")

    parts = split_storage_path(data.get("path"))
    new_name = clean_name(data.get("name"))

    if new_name is None:
        abort(400, description="That name isn't allowed")

    if new_name == parts[-1]:
        abort(400, description="The name is unchanged")

    with open_storage_directory(parts[:-1]) as parent_fd:
        require_item(parent_fd, parts[-1])
        rename_noreplace(parent_fd, parts[-1], parent_fd, new_name)

    return {"success": True}


def write_trash_meta(entry_fd, original_path, name):
    meta = {
        "original_path": original_path,
        "name": name,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }

    fd = os.open(
        "meta.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=entry_fd,
    )

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)


@app.route("/files/delete", methods=["POST"])
@login_required
def delete_item():
    """Move an item into .trash/<id>/item; nothing is erased."""
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        abort(400, description="Invalid JSON")

    parts = split_storage_path(data.get("path"))
    name = parts[-1]

    with open_storage_directory(parts[:-1]) as parent_fd:
        require_item(parent_fd, name)

        with open_storage_directory([]) as root_fd:
            try:
                os.mkdir(TRASH_NAME, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass

            trash_fd = os.open(TRASH_NAME, DIR_FLAGS, dir_fd=root_fd)

            try:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                entry = f"{stamp}-{uuid.uuid4().hex[:8]}"
                os.mkdir(entry, 0o700, dir_fd=trash_fd)
                entry_fd = os.open(entry, DIR_FLAGS, dir_fd=trash_fd)
                moved = False

                try:
                    write_trash_meta(entry_fd, "/".join(parts), name)
                    rename_noreplace(parent_fd, name, entry_fd, "item")
                    moved = True
                finally:
                    if not moved:
                        try:
                            os.unlink("meta.json", dir_fd=entry_fd)
                        except OSError:
                            pass
                        try:
                            os.rmdir(entry, dir_fd=trash_fd)
                        except OSError:
                            pass

                    os.close(entry_fd)
            finally:
                os.close(trash_fd)

    return {"success": True}

#MEDIA
@app.route("/media")
@login_required
def media():
    return render_template("media.html")

#SERVICES
@app.route("/services")
@login_required
def services():
    return render_template("services.html")

#SETTINGS
@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
