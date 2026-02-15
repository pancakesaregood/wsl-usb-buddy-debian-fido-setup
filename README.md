# Debian WSL YubiKey Sudo Setup

This folder contains the Debian/WSL-side script to require a YubiKey touch for `sudo`.

## File

- `setup_yubikey_sudo_wsl.py`: sets up PAM U2F for `sudo` on Debian WSL.

## What This Configures

- Checks that WSL can see the YubiKey FIDO interface (`/dev/hidraw*`).
- Installs required packages.
- Creates Yubico udev rule.
- Enrolls your key with `pamu2fcfg` into `~/.config/Yubico/u2f_keys` (PIN/touch prompt).
- Updates `/etc/pam.d/sudo` to require `pam_u2f`.

## Prerequisites

- Debian running in WSL.
- YubiKey attached to Debian via `usbipd` from Windows.
- Run the script with `sudo`.

Windows (Admin PowerShell) example:

```powershell
usbipd list
usbipd bind --busid <BUSID>
usbipd attach --wsl --distribution Debian --busid <BUSID>
```

## Copy To WSL Home

From Windows, copy this folder into your Debian home, for example:

- Source: `C:\wsl-buddy\debian_yubico_fido_setup`
- Destination: `\\wsl.localhost\Debian\home\<your-user>\debian_yubico_fido_setup`

## Run

In Debian WSL:

```bash
cd ~/debian_yubico_fido_setup
sudo python3 setup_yubikey_sudo_wsl.py
```

Re-enroll key:

```bash
sudo python3 setup_yubikey_sudo_wsl.py --re-enroll
```

Dry run:

```bash
sudo python3 setup_yubikey_sudo_wsl.py --dry-run
```

## Test

Open a new Debian terminal and run:

```bash
sudo -k
sudo whoami
```

Expected: touch/PIN prompt, then output `root`.

## Recovery

If you lock yourself out of sudo:

```powershell
wsl -u root
```

Then in WSL:

```bash
cp /etc/pam.d/sudo.bak /etc/pam.d/sudo
```

## Troubleshooting

- No `/dev/hidraw*`: re-attach with `usbipd`, then retry.
- `Permission denied` writing `~/.config/Yubico/u2f_keys.tmp`: ensure you are using the latest `setup_yubikey_sudo_wsl.py` (it fixes ownership before enrollment).
- If key is attached to the wrong distro, detach and re-attach explicitly to Debian.
