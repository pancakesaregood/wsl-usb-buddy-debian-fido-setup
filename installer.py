#!/usr/bin/env python3
"""
setup_yubikey_sudo_wsl.py
Hardware-gate sudo with YubiKey touch (PAM U2F) on Debian WSL.

What it does (Debian/WSL side):
- Installs required packages (usbutils, libfido2, udev, libpam-u2f, pamu2fcfg)
- Writes udev rule for Yubico FIDO hidraw access (/etc/udev/rules.d/70-u2f.rules)
- Enrolls YubiKey with pamu2fcfg -> ~/.config/Yubico/u2f_keys (interactive: PIN/touch)
- Backs up /etc/pam.d/sudo and inserts pam_u2f line at top

What it DOES NOT do:
- Windows usbipd bind/attach (script prints the commands you should run)

Usage:
  sudo python3 tools/setup_yubikey_sudo_wsl.py

Re-enroll / overwrite u2f_keys:
  sudo python3 tools/setup_yubikey_sudo_wsl.py --re-enroll

Dry run (show what would change):
  sudo python3 tools/setup_yubikey_sudo_wsl.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

UDEV_RULE_PATH = Path("/etc/udev/rules.d/70-u2f.rules")
UDEV_RULE_CONTENT = 'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1050", MODE="0666"\n'
PAM_SUDO_PATH = Path("/etc/pam.d/sudo")
ENROLL_PREREQ_PACKAGES = [
    "usbutils",
    "libfido2-1",
    "udev",
    "pamu2fcfg",
]
POST_ENROLL_PACKAGES = [
    "libpam-u2f",
    "libfido2-dev",
]


def run(cmd: list[str], *, check: bool = True, capture: bool = False, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=text)


def is_wsl() -> bool:
    try:
        data = Path("/proc/version").read_text(errors="ignore").lower()
        return "microsoft" in data or "wsl" in data
    except Exception:
        return False


def require_root():
    if os.geteuid() != 0:
        print("ERROR: This script must be run with sudo/root (it edits /etc and installs packages).")
        print("Run: sudo python3 tools/setup_yubikey_sudo_wsl.py")
        sys.exit(1)


def get_target_user(explicit_user: str | None) -> tuple[str, Path]:
    """
    Prefer explicit --user; else prefer SUDO_USER; else fall back to current user.
    Returns (username, home_path).
    """
    if explicit_user:
        u = explicit_user
    else:
        u = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name

    try:
        pw = pwd.getpwnam(u)
    except KeyError:
        print(f"ERROR: Could not find user '{u}' on this system.")
        sys.exit(1)

    return u, Path(pw.pw_dir)


def apt_install(packages: list[str], dry_run: bool, title: str = "Installing required packages (Debian)"):
    print(f"\n[Package Step] {title}...")
    print("Packages:", " ".join(packages))
    if dry_run:
        print("DRY RUN: skipping apt operations.")
        return

    run(["apt-get", "update"])
    run(["apt-get", "install", "-y"] + packages)


def write_udev_rule(dry_run: bool):
    print("\n[Setup Step] Ensuring udev rule exists for Yubico hidraw access...")
    if UDEV_RULE_PATH.exists():
        existing = UDEV_RULE_PATH.read_text(errors="ignore")
        if existing.strip() == UDEV_RULE_CONTENT.strip():
            print(f"OK: udev rule already present at {UDEV_RULE_PATH}")
            return
        else:
            print(f"NOTICE: {UDEV_RULE_PATH} exists but differs; will replace with standard content.")

    print(f"Writing udev rule to: {UDEV_RULE_PATH}")
    print("Rule content:", UDEV_RULE_CONTENT.strip())
    if dry_run:
        print("DRY RUN: not writing udev rule.")
        return

    UDEV_RULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UDEV_RULE_PATH.write_text(UDEV_RULE_CONTENT)
    # udev in WSL can be weird; reload best-effort
    try:
        run(["udevadm", "control", "--reload-rules"], check=False)
        run(["udevadm", "trigger"], check=False)
    except FileNotFoundError:
        # If udevadm isn't there for some reason, just continue
        pass


def check_hidraw_presence():
    print("\n[Check Step] Checking for /dev/hidraw* (FIDO interface)...")
    hidraw = sorted(Path("/dev").glob("hidraw*"))
    if not hidraw:
        print("ERROR: No /dev/hidraw* devices found.")
        print("This usually means WSL doesn't currently see the YubiKey FIDO HID interface.")
        print("\nFix checklist:")
        print("  A) On Windows (Admin PowerShell):")
        print("     usbipd list")
        print("     usbipd detach --busid <BUSID>")
        print("     usbipd attach --wsl --distribution Debian --busid <BUSID>")
        print("  B) Then: wsl --shutdown (Windows) and re-attach again")
        print("  C) Re-run this script")
        sys.exit(2)

    print("OK: Found hidraw devices:", ", ".join(str(p) for p in hidraw))


def enroll_u2f(username: str, home: Path, re_enroll: bool, dry_run: bool):
    print("\n[Enrollment Step] Enrolling YubiKey with pamu2fcfg (interactive: may ask for FIDO2 PIN + touch)...")
    pw = pwd.getpwnam(username)
    yubico_dir = home / ".config" / "Yubico"
    config_dir = home / ".config"
    u2f_keys = yubico_dir / "u2f_keys"

    if u2f_keys.exists() and not re_enroll:
        print(f"OK: {u2f_keys} already exists (use --re-enroll to overwrite).")
        return str(u2f_keys)

    print(f"Will write mapping file: {u2f_keys}")
    if dry_run:
        print("DRY RUN: skipping pamu2fcfg enrollment.")
        return str(u2f_keys)

    # Ensure path exists and is writable by target user (fixes root-owned dir issues).
    config_dir.mkdir(parents=True, exist_ok=True)
    yubico_dir.mkdir(parents=True, exist_ok=True)
    os.chown(config_dir, pw.pw_uid, pw.pw_gid)
    os.chown(yubico_dir, pw.pw_uid, pw.pw_gid)

    # Run pamu2fcfg as the target user so file ownership is correct.
    # Use a temp file then move into place.
    tmp_path = u2f_keys.with_suffix(".tmp")

    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)

    cmd = ["sudo", "-u", username, "bash", "-lc", f"pamu2fcfg > {shlex_quote(str(tmp_path))}"]
    print("Running:", " ".join(cmd))
    print("If prompted for a PIN: set/enter your YubiKey FIDO2 PIN, then touch the key.")
    try:
        run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print("ERROR: pamu2fcfg failed. Common causes:")
        print("- YubiKey not attached to Debian distro")
        print("- /dev/hidraw* not present/accessible")
        print("- FIDO2 app disabled on the key")
        print("- PIN/touch not provided in time")
        print("- ~/.config/Yubico is not writable by the target user")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise e

    # Move into place (as root), then set perms/owner
    shutil.move(str(tmp_path), str(u2f_keys))
    os.chmod(u2f_keys, 0o600)

    # Ensure ownership to user
    os.chown(u2f_keys, pw.pw_uid, pw.pw_gid)

    # Validate content begins with username:
    content = u2f_keys.read_text(errors="ignore")
    if not content.startswith(username + ":"):
        print("WARNING: u2f_keys does not start with expected username prefix.")
        print("File content:", content[:200], "...")
        print("This can cause PAM to skip matching. You may need to re-enroll with the correct user.")
    else:
        print("OK: Enrollment file created and matches username prefix.")

    return str(u2f_keys)


def backup_file(path: Path, backup_suffix: str = ".bak", dry_run: bool = False):
    backup = path.with_name(path.name + backup_suffix)
    if backup.exists():
        print(f"OK: Backup already exists at {backup}")
        return backup
    print(f"Creating backup: {backup}")
    if dry_run:
        print("DRY RUN: not creating backup.")
        return backup
    shutil.copy2(path, backup)
    return backup


def ensure_pam_sudo_line(authfile_path: str, dry_run: bool):
    print("\n[PAM Step] Updating /etc/pam.d/sudo to require YubiKey touch for sudo...")
    if not PAM_SUDO_PATH.exists():
        print(f"ERROR: {PAM_SUDO_PATH} not found.")
        sys.exit(3)

    backup_file(PAM_SUDO_PATH, dry_run=dry_run)

    pam_line = f"auth required pam_u2f.so authfile={authfile_path} cue"

    content = PAM_SUDO_PATH.read_text(errors="ignore").splitlines()

    # Remove any existing pam_u2f lines for sudo (to avoid duplicates / wrong paths)
    new_lines = []
    removed = 0
    for line in content:
        if "pam_u2f.so" in line and line.strip().startswith("auth"):
            removed += 1
            continue
        new_lines.append(line)

    if removed:
        print(f"NOTICE: Removed {removed} existing pam_u2f auth line(s) from sudo PAM config to avoid duplicates.")

    # Insert at top (after PAM header comments if present)
    insert_idx = 0
    while insert_idx < len(new_lines) and new_lines[insert_idx].startswith("#"):
        insert_idx += 1

    # Insert our line
    new_lines.insert(insert_idx, pam_line)

    # Ensure it appears before @include common-auth if that exists
    # (Insert at top already satisfies that.)
    updated_text = "\n".join(new_lines).rstrip() + "\n"

    if dry_run:
        print("DRY RUN: would write the following first ~12 lines of /etc/pam.d/sudo:")
        preview = "\n".join(updated_text.splitlines()[:12])
        print(preview)
        return

    PAM_SUDO_PATH.write_text(updated_text)
    print("OK: pam_u2f line added to /etc/pam.d/sudo")
    print("Inserted line:", pam_line)


def print_windows_steps():
    print("\n[Windows-side reminder]")
    print("You must attach the YubiKey to Debian WSL with usbipd (Admin PowerShell). Example:")
    print("  usbipd list")
    print("  usbipd bind --busid <BUSID>")
    print("  usbipd attach --wsl --distribution Debian --busid <BUSID>")
    print("After 'wsl --shutdown' you must re-attach again.")
    print("If the key attaches to the wrong distro, explicitly detach then attach to Debian:")
    print("  usbipd detach --busid <BUSID>")
    print("  usbipd attach --wsl --distribution Debian --busid <BUSID>")


def final_test_instructions():
    print("\n[Final Step] Test instructions (run in a NEW Debian WSL terminal):")
    print("  sudo -k")
    print("  sudo whoami")
    print("\nExpected: It should prompt you to touch the YubiKey, then print 'root'.")
    print("Note: sudo caches auth; 'sudo -k' forces re-auth so you see the prompt.")
    print("\nRecovery (if you lock yourself out):")
    print("  Windows PowerShell: wsl -u root")
    print("  Then in WSL: cp /etc/pam.d/sudo.bak /etc/pam.d/sudo")


def shlex_quote(s: str) -> str:
    # Simple safe quoting for bash -lc
    return "'" + s.replace("'", "'\"'\"'") + "'"


def main():
    parser = argparse.ArgumentParser(description="Set up YubiKey touch requirement for sudo on Debian WSL.")
    parser.add_argument("--user", help="Target username to enroll (defaults to SUDO_USER).")
    parser.add_argument("--re-enroll", action="store_true", help="Overwrite ~/.config/Yubico/u2f_keys by re-running pamu2fcfg.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing the system.")
    args = parser.parse_args()

    if not is_wsl():
        print("WARNING: This does not look like WSL. Proceeding anyway, but this guide is intended for Debian WSL.")
    require_root()

    username, home = get_target_user(args.user)
    print(f"Target user: {username}")
    print(f"Target home: {home}")

    print_windows_steps()

    # Fail early before package/system changes if the token isn't visible in WSL.
    check_hidraw_presence()

    # Install only what is needed for enrollment first, then enroll as early as possible.
    apt_install(ENROLL_PREREQ_PACKAGES, args.dry_run, title="Installing enrollment prerequisites")
    write_udev_rule(args.dry_run)

    # Re-check after udev rule changes, then enroll before PAM changes.
    check_hidraw_presence()
    authfile = enroll_u2f(username, home, args.re_enroll, args.dry_run)

    # Install remaining packages needed for PAM integration after successful enrollment.
    apt_install(POST_ENROLL_PACKAGES, args.dry_run, title="Installing post-enrollment PAM integration packages")
    ensure_pam_sudo_line(authfile, args.dry_run)

    final_test_instructions()


if __name__ == "__main__":
    main()
