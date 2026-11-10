from flask import (
    Flask, render_template, request,
    redirect, url_for, send_from_directory,
    abort, session, flash
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from datetime import timedelta
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
from contextlib import contextmanager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# Secure session cookies
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

#Folder Upload
UPLOAD_FOLDER = Path("/mnt/elicloud/uploads")

# Authentication configuration
app.config.update(
    SECRET_KEY=os.environ["ELICLOUD_SECRET_KEY"],
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # Local HTTP testing only
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    WTF_CSRF_TIME_LIMIT=3600,
)

ADMIN_HASH = os.environ["ELICLOUD_ADMIN_HASH"]

csrf = CSRFProtect(app)
# Rate Limiting
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="redis://127.0.0.1:6379/0",
    default_limits=[]
)

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


def split_storage_path(value):
    if not isinstance(value, str) or not value:
        abort(400, description="Invalid storage path")

    if "\\" in value or "\x00" in value:
        abort(400, description="Invalid storage path")

    parts = value.split("/")

    if any(part in ("", ".", "..") for part in parts):
        abort(400, description="Invalid storage path")

    return parts


@contextmanager
def open_storage_directory(parts):
    if not Path("/mnt/elicloud").is_mount():
        abort(503, description="NVMe storage unavailable")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    fd = os.open(UPLOAD_FOLDER, flags)

    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=fd)
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


app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

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
        nvme_path = "/mnt/elicloud"

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
    if not Path("/mnt/elicloud").is_mount():
        abort(503, description="NVMe storage is not mounted")

    root = UPLOAD_FOLDER.resolve()

    if not root.is_dir():
        abort(503, description="EliCloud storage unavailable")

    if not isinstance(folder, str):
        abort(400, description="Invalid folder path")

    relative = Path(folder)

    if relative.is_absolute() or any(
        part == ".." for part in relative.parts
    ):
        abort(400, description="Invalid folder path")

    destination = (root / relative).resolve()

    if not destination.is_relative_to(root):
        abort(403, description="Access outside storage is forbidden")

    return destination

#FILES
@app.route("/files")
@login_required
def files():
    current_folder = request.args.get("folder", "")
    current_path = get_storage_path(current_folder)

    if not current_path.is_dir():
        abort(404, description="Folder not found")

    folders = []
    stored_files = []

    for item in sorted(
        current_path.iterdir(),
        key=lambda p: p.name.lower()
    ):
        if item.is_symlink():
            continue

        if item.is_dir():
            folders.append({
                "name": item.name,
                "path": item.relative_to(UPLOAD_FOLDER).as_posix()
            })

        elif item.is_file():
            stored_files.append({
                "name": item.name,
                "size": round(
                    item.stat().st_size / (1024 * 1024), 2
                ),
                "path": item.relative_to(UPLOAD_FOLDER).as_posix()
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
        parent_folder=parent_folder
    )

@app.route("/files/folder/create", methods=["POST"])
@login_required
def create_folder():
    current_folder = request.form.get("folder", "")
    folder_name = secure_filename(
        request.form.get("folder_name", "")
    )

    if not folder_name or folder_name in {".", ".."}:
        abort(400, description="Invalid folder name")

    parent = get_storage_path(current_folder)

    if not parent.is_dir():
        abort(404, description="Parent folder not found")

    destination = parent / folder_name

    try:
        destination.mkdir(mode=0o700)
    except FileExistsError:
        abort(409, description="Folder already exists")

    return redirect(url_for("files", folder=current_folder))


@app.route("/files/upload", methods=["POST"])
@login_required
def upload_file():
    if not UPLOAD_FOLDER.is_mount() and not Path("/mnt/elicloud").is_mount():
        abort(503, description="NVMe storage is not mounted")

    if "file" not in request.files:
        abort(400, description="No file provided")

    uploaded_file = request.files["file"]
    filename = secure_filename(uploaded_file.filename or "")

    if not filename or filename in {".", ".."}:
        abort(400, description="Invalid filename")

    current_folder = request.form.get("folder", "")
    current_path = get_storage_path(current_folder)

    if not current_path.is_dir():
        abort(404, description="Destination folder not found")

    destination = current_path / filename

    try:
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            uploaded_file.save(output)

    except FileExistsError:
        abort(409, description="A file with this name already exists")

    return redirect(url_for("files", folder=current_folder))

@app.route("/files/download/<path:filename>")
@login_required
def download_file(filename):
    requested_path = get_storage_path(filename)

    if not requested_path.is_file():
        abort(404, description="File not found")

    return send_from_directory(
        UPLOAD_FOLDER,
        requested_path.relative_to(UPLOAD_FOLDER).as_posix(),
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
            source_name = source_parts[-1]

            try:
                info = os.stat(
                    source_name,
                    dir_fd=source_fd,
                    follow_symlinks=False
                )
            except FileNotFoundError:
                abort(404, description="Source not found")

            if not (
                stat.S_ISREG(info.st_mode)
                or stat.S_ISDIR(info.st_mode)
            ):
                abort(400, description="Unsupported item type")

            encoded_name = os.fsencode(source_name)

            result = _renameat2(
                source_fd,
                encoded_name,
                dest_fd,
                encoded_name,
                RENAME_NOREPLACE
            )

            if result != 0:
                error = ctypes.get_errno()

                if error == errno.EEXIST:
                    abort(409, description="Destination already exists")

                if error == errno.ENOENT:
                    abort(404, description="File or folder not found")

                if error in (errno.EACCES, errno.EPERM):
                    abort(403, description="Permission denied")

                if error == errno.EINVAL:
                    abort(400, description="Invalid folder move")

                raise OSError(error, os.strerror(error))

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
