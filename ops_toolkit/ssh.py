"""SSH connection manager — credentials never leave this module."""

import json
import re
import sys
from pathlib import Path

import paramiko

from .vault import Vault


class ServerSession:
    def __init__(self, server_name: str, vault: Vault):
        self.name = server_name
        creds = vault.get(server_name)
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": creds["ip"],
            "port": creds["port"],
            "username": creds["user"],
            "timeout": 30,
            "banner_timeout": 45,
            "auth_timeout": 45,
            "look_for_keys": False,
            "allow_agent": False,
        }
        key_path = str(creds.get("key_path") or "").strip()
        if key_path:
            connect_kwargs["key_filename"] = str(Path(key_path).expanduser())
        else:
            connect_kwargs["password"] = creds.get("password", "")
        self._client.connect(**connect_kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._client.close()

    # ── Core exec ───────────────────────────────────────────────
    def exec(self, cmd: str, timeout: int = 30) -> dict:
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        return {
            "server": self.name,
            "exit_code": stdout.channel.recv_exit_status(),
            "stdout": stdout.read().decode("utf-8", errors="replace").strip(),
            "stderr": stderr.read().decode("utf-8", errors="replace").strip(),
        }

    # ── System status ───────────────────────────────────────────
    def system_status(self) -> dict:
        script = (
            "echo '@@UPTIME@@'; uptime -p 2>/dev/null || uptime; "
            "echo '@@LOAD@@'; cat /proc/loadavg; "
            "echo '@@MEM@@'; free -m | awk '/Mem:/{print $2,$3,$4}'; "
            "echo '@@DISK@@'; df -h / | awk 'NR==2{print $2,$3,$4,$5}'"
        )
        r = self.exec(script)
        out = r["stdout"]
        sections = {}
        for tag in ("UPTIME", "LOAD", "MEM", "DISK"):
            m = re.search(f"@@{tag}@@\n(.*?)(?=@@|$)", out, re.S)
            sections[tag] = m.group(1).strip() if m else ""

        mem_parts = sections["MEM"].split()
        disk_parts = sections["DISK"].split()
        return {
            "server": self.name,
            "uptime": sections["UPTIME"],
            "load_avg": sections["LOAD"],
            "memory": {"total_mb": mem_parts[0], "used_mb": mem_parts[1], "free_mb": mem_parts[2]} if len(mem_parts) >= 3 else {},
            "disk": {"size": disk_parts[0], "used": disk_parts[1], "avail": disk_parts[2], "use_pct": disk_parts[3]} if len(disk_parts) >= 4 else {},
        }

    # ── Docker ──────────────────────────────────────────────────
    def docker_ps(self) -> list[dict]:
        r = self.exec("docker ps --format '{{json .}}' 2>/dev/null")
        if r["exit_code"] != 0 or not r["stdout"]:
            return []
        containers = []
        for line in r["stdout"].splitlines():
            try:
                c = json.loads(line)
                containers.append({
                    "name": c.get("Names", ""),
                    "image": c.get("Image", ""),
                    "status": c.get("Status", ""),
                    "ports": c.get("Ports", ""),
                })
            except json.JSONDecodeError:
                pass
        return containers

    # ── PM2 ─────────────────────────────────────────────────────
    def pm2_list(self) -> list[dict]:
        r = self.exec("pm2 jlist 2>/dev/null")
        if r["exit_code"] != 0 or not r["stdout"]:
            return []
        try:
            procs = json.loads(r["stdout"])
            return [{"name": p["name"], "status": p.get("pm2_env", {}).get("status", ""),
                      "cpu": p.get("monit", {}).get("cpu", 0),
                      "memory_mb": round(p.get("monit", {}).get("memory", 0) / 1024 / 1024, 1)}
                     for p in procs]
        except (json.JSONDecodeError, KeyError):
            return []

    # ── APT maintenance ─────────────────────────────────────────
    def apt_maintenance(self) -> dict:
        r = self.exec(
            "export DEBIAN_FRONTEND=noninteractive && "
            "apt-get update -qq && "
            "apt-get upgrade -y -qq && "
            "apt-get autoremove -y -qq",
            timeout=300,
        )
        return {"server": self.name, "exit_code": r["exit_code"], "output": r["stdout"][-2000:]}

    # ── SFTP ────────────────────────────────────────────────────
    def upload(self, local_path: str, remote_path: str):
        sftp = self._client.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        return {"server": self.name, "action": "upload", "local": local_path, "remote": remote_path}

    def download(self, remote_path: str, local_path: str):
        sftp = self._client.open_sftp()
        sftp.get(remote_path, local_path)
        sftp.close()
        return {"server": self.name, "action": "download", "remote": remote_path, "local": local_path}
