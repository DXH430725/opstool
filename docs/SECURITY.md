# Security Policy

This repository is designed to be public.

Do not commit:

- `.env`, `.env.local`, or environment backups
- `data/vault.enc` or decrypted vault JSON
- SSH private keys
- API keys, OAuth tokens, cookies, session tokens, bot tokens, or Authorization headers
- database dumps, Redis snapshots, server backups, proxy lists, or subscription URLs
- customer data, private emails, or production incident logs containing identifiers

Recommended local workflow:

```bash
export OPS_MASTER_KEY="local-passphrase"
python -m ops_toolkit vault init
python -m ops_toolkit toolkit add-server my-server --ip 203.0.113.10 --user root --key-path ~/.ssh/id_ed25519 --check
```

Before publishing:

```bash
gitleaks detect --source . --no-git --redact
git status
git diff --staged
```

If a secret enters git history, rotate the secret first, then clean history if needed.
