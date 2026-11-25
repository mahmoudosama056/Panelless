# Security Policy

Panelless connects to servers over SSH, so security reports are taken seriously.

## Supported versions
Only the latest release receives fixes.

## Reporting a vulnerability
Please **do not open a public issue** for security problems.
Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository.
Include the steps to reproduce and the version you used.

## How Panelless handles secrets
- Passwords are never written to disk. Only host, port, username, key file path, theme and font size are stored
  in `~/.panelless.json`.
- If you enter MySQL credentials, they are written to a temporary `chmod 600` file in `/tmp` on the server and
  deleted when you close Panelless.
- Backups of edited files go to `~/panelless_backups` on the server, outside any web root.
- New host keys are accepted automatically on first connect. On untrusted networks, verify the fingerprint yourself.
- Only use Panelless on servers you own or are authorized to administer.
