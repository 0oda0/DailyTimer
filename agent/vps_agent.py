#!/usr/bin/env python3
"""DailyTimer VPS-агент — маленькая служба на хосте (только стандартная библиотека Python).

Показывает DailyTimer состояние сервера, порты, Docker-контейнеры и git-проекты и по запросу
обновляет проект или перезапускает контейнер. Произвольные команды не выполняет: действия только
над найденными проектами и существующими контейнерами. Все запросы — с токеном.

Настройки (переменные окружения или /etc/dailytimer-agent.env):
  AGENT_TOKEN   — общий секрет с DailyTimer (обязателен)
  AGENT_BIND    — адрес (по умолчанию IP интерфейса docker0, иначе 127.0.0.1)
  AGENT_PORT    — порт (9137)
  AGENT_ROOTS   — где искать git-проекты, через двоеточие (/root:/home:/opt:/srv:/var/www)
  AGENT_PROJECTS — дополнительные пути к проектам, через двоеточие
  AGENT_EXCLUDE — имена папок, которые не считать проектами (nvm, pyenv, …)
"""

from __future__ import annotations

import glob
import hmac
import json
import os
import platform
import pwd
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION = "1.0"
ENV_FILE = "/etc/dailytimer-agent.env"
SKIP_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "overlay", "squashfs", "securityfs",
           "pstore", "bpf", "tracefs", "debugfs", "mqueue", "hugetlbfs", "fusectl", "configfs", "autofs", "nsfs",
           "ramfs", "rpc_pipefs", "binfmt_misc", "efivarfs", "fuse.lxcfs"}
TCP_STATES = {"0A": "LISTEN"}


def ensure_home() -> None:
    """systemd не задаёт HOME службам — без него git не видит сохранённые учётные данные GitHub."""
    if not os.environ.get("HOME"):
        try:
            os.environ["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            os.environ["HOME"] = "/root"
    os.environ.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")


def load_env(path: str = ENV_FILE) -> None:
    try:
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"'))
    except OSError:
        pass


def run(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


# ------------------------------------------------------------------ система

def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def cpu_percent(interval: float = 0.3) -> float:
    def snap() -> tuple[int, int]:
        fields = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return idle, sum(fields)
    idle1, total1 = snap()
    time.sleep(interval)
    idle2, total2 = snap()
    return round(100 * (1 - (idle2 - idle1) / max(1, total2 - total1)), 1)


def memory() -> dict[str, int]:
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0]) * 1024 if rest.split() else 0
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    return {"total": total, "used": total - avail, "available": avail,
            "swap_total": info.get("SwapTotal", 0), "swap_used": info.get("SwapTotal", 0) - info.get("SwapFree", 0)}


def disks() -> list[dict[str, Any]]:
    seen, out = set(), []
    for line in _read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] in SKIP_FS or parts[1].startswith(("/snap", "/var/lib/docker", "/run")):
            continue
        device, mount = parts[0], parts[1].replace("\\040", " ")
        if device in seen:
            continue
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        if usage.total == 0:
            continue
        seen.add(device)
        out.append({"mount": mount, "device": device, "fs": parts[2], "total": usage.total, "used": usage.used,
                    "free": usage.free, "percent": round(usage.used / usage.total * 100, 1)})
    return out


def network() -> dict[str, int]:
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, data = line.partition(":")
        if name.strip() == "lo" or not data.split():
            continue
        fields = data.split()
        rx, tx = rx + int(fields[0]), tx + int(fields[8])
    return {"rx": rx, "tx": tx}


def system_status() -> dict[str, Any]:
    uptime = float((_read("/proc/uptime").split() or ["0"])[0])
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    os_name = ""
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip('"')
    return {
        "hostname": socket.gethostname(), "os": os_name or platform.platform(), "kernel": platform.release(),
        "uptime": int(uptime), "load": [round(x, 2) for x in load], "cpus": os.cpu_count() or 1,
        "cpu_percent": cpu_percent(), "memory": memory(), "disks": disks(), "network": network(),
        "agent_version": VERSION, "time": int(time.time()),
    }


# ------------------------------------------------------------------ порты

def _hex_addr(value: str) -> tuple[str, int]:
    host, port = value.split(":")
    raw = bytes.fromhex(host)
    if len(raw) == 4:
        ip = socket.inet_ntop(socket.AF_INET, raw[::-1])
    else:
        ip = socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
    return ip, int(port, 16)


def _inode_owners() -> dict[str, tuple[int, str]]:
    owners: dict[str, tuple[int, str]] = {}
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        pid = int(fd_dir.split("/")[2])
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        name = None
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                if name is None:
                    name = _read(f"/proc/{pid}/comm").strip() or "?"
                owners[target[8:-1]] = (pid, name)
    return owners


def parse_proc_net(text: str, proto: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        state = fields[3]
        if proto.startswith("tcp") and state != "0A":  # только LISTEN
            continue
        if proto.startswith("udp") and state != "07":
            continue
        ip, port = _hex_addr(fields[1])
        out.append({"proto": proto[:3], "ip": ip, "port": port, "inode": fields[9]})
    return out


def listening_ports(containers: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    entries = []
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        entries += parse_proc_net(_read(f"/proc/net/{proto}"), proto)
    owners = _inode_owners()
    published: dict[int, str] = {}
    for c in containers or []:
        for port in c.get("published", []):
            published[port] = c["name"]
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for e in entries:
        key = (e["proto"], e["port"])
        public = e["ip"] not in {"127.0.0.1", "::1"} and not e["ip"].startswith("127.")
        pid, name = owners.get(e["inode"], (None, ""))
        item = result.setdefault(key, {"proto": e["proto"], "port": e["port"], "addresses": [], "public": False,
                                       "process": name, "pid": pid, "container": published.get(e["port"], "")})
        if e["ip"] not in item["addresses"]:
            item["addresses"].append(e["ip"])
        item["public"] = item["public"] or public
        if name and not item["process"]:
            item["process"], item["pid"] = name, pid
    for c in containers or []:
        for item in c.get("internal", []):
            key = (item["proto"], item["port"])
            if key not in result:
                result[key] = {"proto": item["proto"], "port": item["port"], "addresses": ["в сети Docker"],
                               "public": False, "process": "", "pid": None, "container": c["name"], "internal": True}
    return sorted(result.values(), key=lambda x: (x.get("internal", False), x["proto"], x["port"]))


# ------------------------------------------------------------------ docker

def docker_containers() -> list[dict[str, Any]]:
    if not shutil.which("docker"):
        return []
    code, out = run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=20)
    if code != 0:
        return []
    stats: dict[str, dict[str, str]] = {}
    code_s, out_s = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=30)
    if code_s == 0:
        for line in out_s.splitlines():
            try:
                row = json.loads(line)
                stats[row.get("Name", "")] = row
            except ValueError:
                pass
    containers = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ports_text = row.get("Ports", "")
        published = sorted({int(p) for p in re.findall(r":(\d+)->", ports_text)})
        # Порты внутри контейнера без проброса наружу: «11434/tcp» (не часть «…->11434/tcp»).
        internal = sorted({(int(p), proto) for p, proto in re.findall(r"(?:^|,\s*)(\d+)/(tcp|udp)", ports_text)})
        st = stats.get(row.get("Names", ""), {})
        containers.append({
            "name": row.get("Names", ""), "image": row.get("Image", ""), "state": row.get("State", ""),
            "status": row.get("Status", ""), "ports": ports_text, "published": published,
            "internal": [{"port": p, "proto": proto} for p, proto in internal],
            "project": row.get("Labels", "") and dict(
                kv.split("=", 1) for kv in row["Labels"].split(",") if "=" in kv).get("com.docker.compose.project", ""),
            "cpu": st.get("CPUPerc", ""), "mem": st.get("MemUsage", ""),
        })
    return containers


# ------------------------------------------------------------------ git-проекты

def discover_projects() -> list[str]:
    roots = os.environ.get("AGENT_ROOTS", "/root:/home:/opt:/srv:/var/www").split(":")
    found: set[str] = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for depth in ("*", "*/*", "*/*/*"):
            for git_dir in glob.glob(os.path.join(root, depth, ".git")):
                found.add(os.path.dirname(git_dir))
        if os.path.isdir(os.path.join(root, ".git")):
            found.add(root)
    for extra in os.environ.get("AGENT_PROJECTS", "").split(":"):
        if extra and os.path.isdir(os.path.join(extra, ".git")):
            found.add(os.path.abspath(extra))
    skip = set(os.environ.get(
        "AGENT_EXCLUDE", "nvm:.nvm:rbenv:.rbenv:pyenv:.pyenv:.oh-my-zsh:.cargo:.rustup:.local:.cache:node_modules:go"
    ).split(":"))
    result = []
    for path in sorted(found):
        parts = set(Path(path).parts)
        if parts & skip:
            continue
        if any(path.startswith(parent + os.sep) for parent in result):  # вложенный репозиторий
            continue
        result.append(path)
    return result


def github_slug(url: str) -> str:
    match = re.search(r"github\.com[:/]+([^/]+/[^/.\s]+?)(?:\.git)?/?$", url or "")
    return match.group(1) if match else ""


def update_method(path: str) -> str:
    if os.path.isfile(os.path.join(path, "update.sh")):
        return "update.sh"
    if any(os.path.isfile(os.path.join(path, f)) for f in
           ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")):
        return "git pull + docker compose"
    return "git pull"


def project_info(path: str, fetch: bool = False) -> dict[str, Any]:
    def git(*args: str, timeout: int = 20) -> str:
        code, out = run(["git", "-c", f"safe.directory={path}", *args], cwd=path, timeout=timeout)
        return out if code == 0 else ""

    fetch_error = ""
    if fetch:
        code, out = run(["git", "-c", f"safe.directory={path}", "fetch", "--quiet", "origin"], cwd=path, timeout=60)
        fetch_error = out if code else ""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    remote = git("remote", "get-url", "origin")
    behind = ahead = None
    counts = git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}") if branch else ""
    if counts:
        ahead, behind = (int(x) for x in counts.split())
    last = git("log", "-1", "--format=%h%x1f%s%x1f%cI%x1f%an")
    commit = dict(zip(("hash", "message", "date", "author"), last.split("\x1f"))) if last else {}
    upstream = git("log", "-1", "--format=%h%x1f%s%x1f%cI", f"origin/{branch}") if branch else ""
    return {
        "path": path, "name": os.path.basename(path), "branch": branch, "remote": remote,
        "github": github_slug(remote), "commit": commit,
        "upstream": dict(zip(("hash", "message", "date"), upstream.split("\x1f"))) if upstream else {},
        "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "behind": behind, "ahead": ahead, "update_method": update_method(path), "fetch_error": fetch_error[:300],
    }


# ------------------------------------------------------------------ задания (обновления, перезапуски)

JOBS: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _job(title: str, steps: list[tuple[list[str], str, int]]) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "title": title, "status": "running", "log": "", "started": int(time.time()), "finished": None}
    with _jobs_lock:
        JOBS[job_id] = job
        for old in sorted(JOBS.values(), key=lambda j: j["started"])[:-30]:
            JOBS.pop(old["id"], None)

    def worker() -> None:
        ok = True
        for cmd, cwd, timeout in steps:
            job["log"] += f"$ {' '.join(cmd)}\n"
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
                deadline = time.time() + timeout
                for line in proc.stdout:  # type: ignore[union-attr]
                    job["log"] = (job["log"] + line)[-40000:]
                    if time.time() > deadline:
                        proc.kill()
                        job["log"] += "\n[превышено время]\n"
                        break
                code = proc.wait()
            except OSError as exc:
                job["log"] += f"{exc}\n"
                code = 1
            if code != 0:
                job["log"] += f"\n[код выхода {code}]\n"
                ok = False
                break
        job["status"] = "ok" if ok else "error"
        job["finished"] = int(time.time())

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_update(path: str) -> str:
    if path not in discover_projects():
        raise ValueError("Проект не найден среди git-репозиториев сервера")
    safe = ["-c", f"safe.directory={path}"]
    method = update_method(path)
    if method == "update.sh":
        steps = [(["bash", "update.sh"], path, 1800)]
    else:
        steps = [(["git", *safe, "pull", "--ff-only"], path, 300)]
        if method.endswith("docker compose"):
            steps.append((["docker", "compose", "up", "-d", "--build", "--remove-orphans"], path, 1800))
    return _job(f"Обновление {os.path.basename(path)}", steps)


def start_restart(name: str) -> str:
    if name not in {c["name"] for c in docker_containers()}:
        raise ValueError("Контейнер не найден")
    return _job(f"Перезапуск {name}", [(["docker", "restart", name], "/", 300)])


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"DailyTimerAgent/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:  # тише в journald
        pass

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = os.environ.get("AGENT_TOKEN", "")
        given = self.headers.get("X-Agent-Token", "")
        return bool(token) and hmac.compare_digest(token, given)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        path, _, query = self.path.partition("?")
        if path == "/status":
            containers = docker_containers()
            return self._send(200, {**system_status(), "containers": containers,
                                    "ports": listening_ports(containers)})
        if path == "/projects":
            fetch = "fetch=1" in query
            return self._send(200, {"projects": [project_info(p, fetch) for p in discover_projects()]})
        if path.startswith("/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            return self._send(200 if job else 404, job or {"error": "not found"})
        if path == "/jobs":
            return self._send(200, {"jobs": sorted(JOBS.values(), key=lambda j: -j["started"])[:10]})
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        body = self._body()
        try:
            if self.path == "/projects/update":
                return self._send(200, {"job": start_update(str(body.get("path", "")))})
            if self.path == "/containers/restart":
                return self._send(200, {"job": start_restart(str(body.get("name", "")))})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(404, {"error": "not found"})


def default_bind() -> str:
    code, out = run(["ip", "-4", "-o", "addr", "show", "docker0"], timeout=5)
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out) if code == 0 else None
    return match.group(1) if match else "127.0.0.1"


def main() -> None:
    ensure_home()
    load_env()
    if not os.environ.get("AGENT_TOKEN"):
        raise SystemExit("AGENT_TOKEN не задан")
    bind = os.environ.get("AGENT_BIND") or default_bind()
    port = int(os.environ.get("AGENT_PORT", "9137"))
    server = ThreadingHTTPServer((bind, port), Handler)
    print(f"DailyTimer agent {VERSION} слушает {bind}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
