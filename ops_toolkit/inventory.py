"""Server + service inventory backed by YAML."""

from pathlib import Path

import yaml

INVENTORY_FILE = Path(__file__).resolve().parent.parent / "data" / "inventory.yaml"


def load() -> dict:
    if not INVENTORY_FILE.exists():
        return {"servers": {}}
    return yaml.safe_load(INVENTORY_FILE.read_text(encoding="utf-8")) or {"servers": {}}


def save(data: dict):
    INVENTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_FILE.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8")


def get_services(server_name: str) -> list[dict]:
    inv = load()
    server = inv.get("servers", {}).get(server_name, {})
    return server.get("services", [])


def refresh(server_name: str, session) -> list[dict]:
    services = []
    for c in session.docker_ps():
        services.append({"name": c["name"], "type": "docker", "status": c["status"], "image": c["image"]})
    for p in session.pm2_list():
        services.append({"name": p["name"], "type": "pm2", "status": p["status"]})

    inv = load()
    if server_name not in inv.get("servers", {}):
        inv.setdefault("servers", {})[server_name] = {}
    inv["servers"][server_name]["services"] = services
    save(inv)
    return services
