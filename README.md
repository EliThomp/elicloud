# EliCloud — Raspberry Pi Private Cloud & Wireless LAN

A self-hosted private cloud and wireless network built by hand on a Raspberry Pi 5. The Pi runs its own Wi-Fi access point, hands out DHCP leases, routes and NATs traffic to an upstream network, and serves an authenticated web dashboard for monitoring the network and managing files on a 1 TB NVMe SSD.

No router firmware or cloud-appliance image is involved: every layer (AP, DHCP, forwarding, firewall, web app, service supervision) is configured manually.

> **Status:** core networking and dashboard are working; hardening, backups, and final validation are in progress. See [Known limitations](#known-limitations).

## Architecture

```text
 Phone / laptop
      |  Wi-Fi (WPA2-PSK)
      v
 wlan0  (AP, hostapd)  --- Raspberry Pi 5 --- wlan1 (upstream client) --> existing LAN --> Internet
                              |
        dnsmasq (DHCP)        |  nftables: forward policy drop + masquerade
        Flask + gunicorn      |
        (127.0.0.1:5000)      +-- /mnt/elicloud (1 TB NVMe) -> uploads/
```

| Layer | Implementation |
|---|---|
| Access point | `hostapd` on `wlan0`, 2.4 GHz, WPA2-PSK |
| DHCP | `dnsmasq`, 192.168.4.0/24 pool on the AP interface |
| Routing / NAT | IPv4 forwarding + `nftables` masquerade out `wlan1` |
| Web app | Flask behind gunicorn (2 workers), supervised by `systemd` |
| Storage | NVMe mounted at `/mnt/elicloud`, files under `uploads/` |
| Remote access | Tailscale (device list is shown on the dashboard) |

Interface names and addresses in `config-examples/` reflect the author's setup. Verify them against your own hardware before use.

## Features

- **Dashboard:** CPU temperature, RAM, root and NVMe usage, uptime, internet reachability, IP-forwarding state, currently associated Wi-Fi clients (MAC / IP / hostname / signal, from `iw` + dnsmasq leases), and Tailscale peers.
- **File manager:** multi-file upload (Unicode names preserved, never overwrites), download, create folders, rename, drag-and-drop move, and delete-to-trash (items move to a hidden `.trash/` with their original path recorded; nothing is erased).
- **Authentication:** single administrator account, hashed password, Secure/HttpOnly session cookie with an 8-hour lifetime, CSRF protection on all state-changing requests, login rate limiting (Redis-backed, keyed on the real client IP via one trusted proxy hop).
- **Safe storage handling:** the app refuses to touch files if `/mnt/elicloud` is not a mount point; moves are done with fd-relative `renameat2(RENAME_NOREPLACE)` and `O_NOFOLLOW`, so they cannot overwrite existing files or follow symlinks out of the storage root.

## Repository layout

```text
dashboard/
  app.py              Flask application
  templates/          Jinja templates (login, dashboard, files, ...)
  static/             CSS and logo
systemd/
  elicloud.service    gunicorn service unit
config-examples/
  hostapd.conf.example
  dnsmasq.conf.example
  nftables.rules.example
  elicloud.env.example
```

## Setup (outline)

Requirements: Raspberry Pi OS (Debian-based), Python 3, a mounted storage volume at `/mnt/elicloud`, Redis on `127.0.0.1:6379` (`sudo apt install redis-server`), and the packages in `requirements.txt`.

1. Mount the SSD at `/mnt/elicloud` (via `/etc/fstab`) and create `/mnt/elicloud/uploads`.
2. Copy and adapt the `hostapd`, `dnsmasq`, and `nftables` examples; enable IPv4 forwarding (`net.ipv4.ip_forward=1`).
3. Create the app environment:
   ```bash
   python3 -m venv ~/dashboard/.venv
   ~/dashboard/.venv/bin/pip install -r requirements.txt
   ```
4. Create `/etc/elicloud.env` (mode `600`, owned by root) from `config-examples/elicloud.env.example`. Generate the values with:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   python3 -c "import getpass; from werkzeug.security import generate_password_hash as g; print(g(getpass.getpass()))"
   ```
   Wrap the password hash in single quotes in the env file.
5. Install `systemd/elicloud.service`, then `sudo systemctl enable --now elicloud`.

Useful checks:

```bash
systemctl status hostapd dnsmasq elicloud
ip -br addr && ip route
sudo nft list ruleset
findmnt /mnt/elicloud
journalctl -u elicloud -n 100 --no-pager
```

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests
```

The tests use a temporary directory as storage and an in-memory rate limiter, so they need neither the NVMe drive nor Redis. They cover upload/download integrity, Unicode names, no-overwrite behavior, delete-to-trash, rename, move rules, path traversal, symlink escape, anonymous access, and the unmounted-storage case.

## Security notes

- Secrets live in `/etc/elicloud.env`, never in the repository. `.gitignore` excludes env files, keys, databases, uploads, and backup files.
- The gunicorn listener is bound to loopback only and is published to the tailnet with `tailscale serve` (HTTPS, tailnet-only). Do not use `tailscale funnel` or port-forward it to the public internet. The app trusts exactly one proxy hop (`ProxyFix`), so it must not be exposed directly to untrusted clients.
- Never commit a raw `nft list ruleset` dump, `hostapd.conf`, or DHCP lease files.

## Known limitations

- Single admin account; no per-user permissions yet.
- The trash has no UI yet: restoring or emptying it is done from the shell (`/mnt/elicloud/uploads/.trash/<id>/`, with `meta.json` recording the original path).
- Uploads are capped per request (default 100 MB) and held in a temporary file first; chunked/resumable uploads are planned.
- No automated backups or restore procedure yet (a single SSD is not a backup).
- Media, Services, and Settings pages are placeholders.
- Automated tests cover the file manager only (see below); network, boot and backup validation is still manual.

## Roadmap

1. Finish acceptance testing: cold reboot recovery, upload/download integrity, path-traversal and illegal-move tests, mount-failure behavior.
2. Add an inbound firewall policy for traffic to the Pi itself and isolate AP clients from the upstream LAN.
3. Add backups to a separate physical device and test a full restore.
4. Optional: lower-permission accounts for LAN users, Grafana, Pi-hole, Jellyfin, Gitea.
