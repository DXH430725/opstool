# OPSTOOL

Small SSH-based operations toolkit for personal servers and API relay maintenance.

This public repository contains only generic tooling and sanitized helper scripts. Private inventories, encrypted vaults, `.env` files, production runbooks, server backups, API keys, OAuth tokens, proxy credentials, and customer data are intentionally excluded.

## Features

- Encrypted server vault using Fernet + PBKDF2.
- SSH command execution and file upload helpers.
- Docker / PM2 / system status snapshots.
- Firewall and baseline package helpers.
- Reality / NaiveProxy / Hysteria deployment helpers.
- Optional Sub2API maintenance helpers:
  - redeem code generation
  - proxy pool rapid-response script

## Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Vault

Set a master key only in your shell environment:

```bash
export OPS_MASTER_KEY="replace-with-a-strong-local-passphrase"
```

Initialize and add a server:

```bash
python -m ops_toolkit vault init
python -m ops_toolkit toolkit add-server my-server --ip 203.0.113.10 --user root --key-path ~/.ssh/id_ed25519 --check
```

The encrypted vault is stored at `data/vault.enc`. Do not commit real vault files.

## Common Commands

```bash
python -m ops_toolkit vault list
python -m ops_toolkit status my-server
python -m ops_toolkit docker-ps my-server
python -m ops_toolkit exec my-server 'uptime'
python -m ops_toolkit upload my-server ./local.txt /tmp/local.txt
python -m ops_toolkit toolkit patrol my-server --docker --services
```

## Helper Scripts

Generate Sub2API redeem codes:

```bash
python APITOOLS/sub2api_redeem_codes.py --prefix demo --count 10 --value 5 --notes "demo batch" --dry-run
```

Run Sub2API proxy pool helper on the Sub2API host:

```bash
python APITOOLS/proxykit.py status --window 5
python APITOOLS/proxykit.py test all
python APITOOLS/proxykit.py switch --scope free --to old --dry-run
```

`proxykit.py` expects to run on a host that has Docker access to the `sub2api-postgres` container.

## Security

- Never commit `.env`, `data/vault.enc`, SSH private keys, server backups, or generated credentials.
- Never print Authorization headers, API keys, OAuth tokens, cookies, or decrypted vault contents.
- Use `gitleaks` or an equivalent scanner before publishing changes.
- Treat remote operations as production changes: inspect first, prefer narrow reversible commands, and keep a rollback path.

See [docs/SECURITY.md](docs/SECURITY.md) for the repository policy.
