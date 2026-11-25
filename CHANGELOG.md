# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-11-24
### Added
- **Download** button: a file is saved as is (with progress), several files go into a chosen folder
  (existing local files are never overwritten), and folders or mixed selections are saved as one `.zip`.
### Changed
- Downloads run in the background, so the window no longer freezes on big files.

## [1.1.0] - 2026-11-24
### Added
- **Upload files** and **upload whole folders** to any site, with progress, overwrite / skip choice, and an option
  to skip `.git`, `node_modules` and `__pycache__`.
- **Zip here**: select several files / folders (Ctrl / Shift + click) and compress them into a `.zip` on the server.
- **Get ZIP**: download the selection as a single `.zip` (built on the server, temp file cleaned up).
- **Unzip**: extract a `.zip` on the server; files get the folder owner's ownership when running as root.
- Delete now works on several selected items at once.
### Changed
- The file tree supports multi-selection.

## [1.0.0] - 2026-11-24
### Added
- Dashboard with CPU, memory, disk, load and service status.
- Sites & Files: site picker, file tree, code editor with syntax colors and line numbers, upload / download /
  rename / delete, automatic server-side backup before saving.
- Users: create, delete, password, lock / unlock, shell, sudo.
- Services: start / stop / restart / reload / enable / disable and service logs.
- Databases (MySQL / MariaDB): databases, users, grants, `.sql` backup download.
- Cron Jobs editor, UFW firewall + Fail2ban unban, SSL expiry check, log viewer with live follow, terminal.
- Dark and light themes, adjustable font size.
- Works as a normal user (with or without passwordless sudo) or as root.
