#!/usr/bin/env python3
"""
bootstrap_ansible_wsl.py
Bootstrap an Ansible control node on Debian WSL from scratch.

What it does:
- Installs system prerequisites via apt
- Creates a project directory structure
- Creates a Python venv
- Installs Ansible + common network automation libs into the venv
- Installs Ansible Galaxy collections (cisco.ios, ansible.netcommon)
- Writes ansible.cfg + sample inventory + sample test playbook

Run:
  python3 tools/bootstrap_ansible_wsl.py
  python3 tools/bootstrap_ansible_wsl.py --path ~/ansible-control-node
  python3 tools/bootstrap_ansible_wsl.py --skip-apt
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path


APT_PACKAGES = [
    "python3",
    "python3-pip",
    "python3-venv",
    "git",
    "sshpass",
    "build-essential",
    "libffi-dev",
    "libssl-dev",
    "ca-certificates",
]

PIP_PACKAGES = [
    "pip",
    "setuptools",
    "wheel",
    "ansible",
    # network + parsing helpers commonly used for Cisco automation
    "paramiko",
    "netmiko",
    "ncclient",
    "jmespath",
    "textfsm",
    "ttp",
    "ttp-templates",
]

GALAXY_COLLECTIONS = [
    "ansible.netcommon",
    "cisco.ios",
]


ANSIBLE_CFG = """[defaults]
inventory = ./inventory/hosts.yml
host_key_checking = False
retry_files_enabled = False
timeout = 60
interpreter_python = auto_silent
stdout_callback = yaml
bin_ansible_callbacks = True

[persistent_connection]
connect_timeout = 60
command_timeout = 60
"""

HOSTS_YML = """all:
  children:
    network_ios:
      hosts:
        switch1:
          ansible_host: 10.10.10.10
          ansible_network_os: cisco.ios.ios
          ansible_connection: network_cli
"""

TEST_PLAYBOOK = """---
- name: Test Cisco IOS reachability (facts)
  hosts: network_ios
  gather_facts: no
  tasks:
    - name: Gather facts from IOS device
      cisco.ios.ios_facts:
      register: facts_out

    - name: Print hostname and version
      ansible.builtin.debug:
        msg:
          - "Hostname: {{ facts_out.ansible_facts.ansible_net_hostname | default('unknown') }}"
          - "Version: {{ facts_out.ansible_facts.ansible_net_version | default('unknown') }}"
"""


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def is_wsl() -> bool:
    try:
        data = Path("/proc/version").read_text(errors="ignore").lower()
        return "microsoft" in data or "wsl" in data
    except Exception:
        return False


def discover_home_users() -> list[pwd.struct_passwd]:
    users = []
    for entry in pwd.getpwall():
        if entry.pw_name == "root":
            continue
        if entry.pw_uid < 1000:
            continue
        if not entry.pw_dir.startswith("/home/"):
            continue
        if not Path(entry.pw_dir).exists():
            continue
        users.append(entry)
    return users


def resolve_target_user(explicit_user: str | None) -> tuple[str | None, Path | None]:
    if explicit_user:
        try:
            pw = pwd.getpwnam(explicit_user)
            return pw.pw_name, Path(pw.pw_dir)
        except KeyError:
            print(f"ERROR: user '{explicit_user}' does not exist.")
            sys.exit(1)

    for candidate in (os.environ.get("SUDO_USER"), os.environ.get("USER"), os.environ.get("LOGNAME")):
        if not candidate or candidate == "root":
            continue
        try:
            pw = pwd.getpwnam(candidate)
            return pw.pw_name, Path(pw.pw_dir)
        except KeyError:
            continue

    if os.geteuid() == 0:
        home_users = discover_home_users()
        if len(home_users) == 1:
            pw = home_users[0]
            print(f"NOTICE: Auto-selected target user '{pw.pw_name}' from /home.")
            return pw.pw_name, Path(pw.pw_dir)
        return None, None

    current = pwd.getpwuid(os.getuid())
    return current.pw_name, Path(current.pw_dir)


def expand_path_for_target(path_text: str, target_home: Path | None) -> Path:
    if target_home is not None and path_text.startswith("~/"):
        return (target_home / path_text[2:]).resolve()
    return Path(os.path.expanduser(path_text)).resolve()


def chown_tree(path: Path, username: str):
    pw = pwd.getpwnam(username)
    for root, dirs, files in os.walk(path):
        os.chown(root, pw.pw_uid, pw.pw_gid)
        for name in dirs:
            os.chown(os.path.join(root, name), pw.pw_uid, pw.pw_gid)
        for name in files:
            os.chown(os.path.join(root, name), pw.pw_uid, pw.pw_gid)


def ensure_python3():
    if shutil.which("python3") is None:
        print("ERROR: python3 is not available. Install Python 3 first.")
        sys.exit(1)


def apt_install(skip_apt: bool):
    if skip_apt:
        print("[SKIP] apt install step skipped (--skip-apt).")
        return
    if os.geteuid() != 0:
        print("NOTE: apt install requires sudo. You may be prompted.")
    print("\n[1/7] Installing system prerequisites via apt...")
    run(["sudo", "apt-get", "update"])
    run(["sudo", "apt-get", "install", "-y"] + APT_PACKAGES)


def create_project_structure(base: Path):
    print("\n[2/7] Creating project structure...")
    (base / "inventory").mkdir(parents=True, exist_ok=True)
    (base / "group_vars").mkdir(parents=True, exist_ok=True)
    (base / "host_vars").mkdir(parents=True, exist_ok=True)
    (base / "playbooks").mkdir(parents=True, exist_ok=True)
    (base / "roles").mkdir(parents=True, exist_ok=True)
    (base / "images").mkdir(parents=True, exist_ok=True)
    (base / "reports").mkdir(parents=True, exist_ok=True)


def write_file(path: Path, content: str):
    path.write_text(content, encoding="utf-8")


def ensure_files(base: Path):
    print("\n[3/7] Writing baseline config files...")
    cfg_path = base / "ansible.cfg"
    inv_path = base / "inventory" / "hosts.yml"
    test_path = base / "playbooks" / "test_ios_facts.yml"

    if not cfg_path.exists():
        write_file(cfg_path, ANSIBLE_CFG)
        print(f"  wrote {cfg_path}")
    else:
        print(f"  exists {cfg_path} (leaving as-is)")

    if not inv_path.exists():
        write_file(inv_path, HOSTS_YML)
        print(f"  wrote {inv_path}")
    else:
        print(f"  exists {inv_path} (leaving as-is)")

    if not test_path.exists():
        write_file(test_path, TEST_PLAYBOOK)
        print(f"  wrote {test_path}")
    else:
        print(f"  exists {test_path} (leaving as-is)")


def create_venv(base: Path) -> Path:
    print("\n[4/7] Creating Python virtual environment...")
    venv_dir = base / "venv"
    if not venv_dir.exists():
        run(["python3", "-m", "venv", str(venv_dir)])
        print(f"  created {venv_dir}")
    else:
        print(f"  exists {venv_dir} (leaving as-is)")
    return venv_dir


def venv_bin(venv_dir: Path, exe: str) -> str:
    # WSL/Linux venv bin path
    return str(venv_dir / "bin" / exe)


def pip_install(venv_dir: Path):
    print("\n[5/7] Installing Ansible + libraries into venv...")
    pip = venv_bin(venv_dir, "pip")
    run([pip, "install", "--upgrade"] + PIP_PACKAGES)
    ansible = venv_bin(venv_dir, "ansible")
    out = run([ansible, "--version"], capture=True)
    print("  ansible version:")
    print("  " + "\n  ".join(out.stdout.strip().splitlines()[:3]))


def galaxy_install(venv_dir: Path):
    print("\n[6/7] Installing Ansible Galaxy collections...")
    ag = venv_bin(venv_dir, "ansible-galaxy")
    for coll in GALAXY_COLLECTIONS:
        print(f"  installing {coll} ...")
        run([ag, "collection", "install", coll])


def print_next_steps(base: Path, venv_dir: Path):
    print("\n[7/7] Done ✅")
    print("\nNext steps:")
    print(f"  1) cd {base}")
    print("  2) Activate venv:")
    print(f"     source {venv_dir}/bin/activate")
    print("  3) Edit inventory/hosts.yml and set your real switch IP(s)")
    print("  4) Test connectivity (will prompt for TACACS/SSH password):")
    print("     ansible-playbook playbooks/test_ios_facts.yml")
    print("\nTip: open the folder in Windows Explorer from WSL:")
    print("     explorer.exe .")


def main():
    parser = argparse.ArgumentParser(description="Bootstrap Ansible on Debian WSL.")
    parser.add_argument(
        "--path",
        default=None,
        help="Project base directory (default: <target-home>/ansible-control-node)",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Target non-root user when running as root/sudo (default: auto-detect)",
    )
    parser.add_argument(
        "--skip-apt",
        action="store_true",
        help="Skip apt install step (use if you already installed prerequisites)",
    )
    args = parser.parse_args()

    ensure_python3()

    if not is_wsl():
        print("WARNING: This does not look like WSL. Continuing anyway.")

    target_user, target_home = resolve_target_user(args.user)
    if os.geteuid() == 0 and target_user is None:
        print("ERROR: Running as root, but could not determine a non-root target user.")
        print("Use: python3 ans.py --user <your-user> [--path /home/<your-user>/ansible-control-node]")
        sys.exit(1)

    default_base = (target_home / "ansible-control-node") if target_home else (Path.home() / "ansible-control-node")
    base = expand_path_for_target(args.path, target_home) if args.path else default_base.resolve()

    if target_user:
        print(f"Target user: {target_user}")
    print(f"Project base: {base}")

    base.mkdir(parents=True, exist_ok=True)

    apt_install(args.skip_apt)
    create_project_structure(base)
    ensure_files(base)

    venv_dir = create_venv(base)
    pip_install(venv_dir)
    galaxy_install(venv_dir)

    if os.geteuid() == 0 and target_user:
        chown_tree(base, target_user)

    print_next_steps(base, venv_dir)


if __name__ == "__main__":
    main()
