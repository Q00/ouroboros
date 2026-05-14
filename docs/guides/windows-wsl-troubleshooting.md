# Windows WSL 2 troubleshooting

Ouroboros supports Windows through **WSL 2**. Native Windows support is
experimental, and some runtimes such as Codex CLI are documented for WSL 2 rather
than native Windows.

This page is for users who cannot get WSL installed before installing Ouroboros.
It is intentionally focused on Windows setup diagnostics, not on Ouroboros runtime
configuration.

## Is Windows 11 Home supported?

Yes. Windows 11 Home can run WSL 2 when virtualization support is available and
Windows optional features can be enabled. WSL installation does not require
Windows Pro-only Hyper-V Manager. Seeing `Hyper-V: A hypervisor has been
detected` or Virtualization Based Security running in `systeminfo` is not, by
itself, a reason WSL 2 cannot be installed.

Use WSL 2 when your environment looks like this:

- Windows 11 or Windows 10 version 2004+ / build 19041+.
- x64 or Arm64 CPU with virtualization enabled in firmware.
- Ability to run an elevated PowerShell session.
- Network or Microsoft Store access to download a Linux distribution, or the
  ability to use WSL's web-download/manual distribution install path.

## First install path

Open **PowerShell as Administrator** and run:

```powershell
wsl --install
```

Restart Windows when prompted, then verify:

```powershell
wsl --status
wsl --list --verbose
```

If WSL is installed but no distribution is present, list and install one:

```powershell
wsl --list --online
wsl --install -d Ubuntu
```

If the Microsoft Store path stalls or is blocked, try the web-download path:

```powershell
wsl --install --web-download -d Ubuntu
```

Then enter the Linux shell and install Ouroboros from inside WSL, not from native
PowerShell:

```bash
python3 --version
pip install ouroboros-ai
ouroboros --version
```

## Manual feature checks

If `wsl --install` fails before any Linux distribution starts, verify the Windows
features manually from an elevated PowerShell:

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
wsl --update
wsl --set-default-version 2
```

Restart Windows after enabling features.

## Common failure patterns

| Symptom | Likely cause | Next action |
| --- | --- | --- |
| `wsl --install` prints help text only | WSL is already installed, but no distro was selected | Run `wsl --list --online`, then `wsl --install -d Ubuntu` |
| Install hangs at `0.0%` | Store/download path issue | Run `wsl --install --web-download -d Ubuntu` |
| Virtual machine/platform error | Firmware virtualization or Windows optional feature missing | Enable virtualization in BIOS/UEFI, then enable `VirtualMachinePlatform` and reboot |
| Distro launches but Ouroboros install fails | Running commands in native Windows or missing Python/pip inside WSL | Open the Ubuntu/WSL shell and install Python tooling there |
| Corporate/school device blocks install | App Control, Store, or policy restriction | Use the web-download/manual distro path, or ask the device administrator |

## What to include in an issue

If WSL still cannot be installed, open or update the GitHub issue with these
exact command outputs. Hardware inventory alone is not enough to diagnose the
failure.

Run in **PowerShell as Administrator**:

```powershell
wsl --version
wsl --status
wsl --list --verbose
wsl --list --online
wsl --install -d Ubuntu
```

If the install command fails, include the complete error text and whether this
also fails:

```powershell
wsl --install --web-download -d Ubuntu
```

Also include:

- Windows edition and build from `winver` or `systeminfo`.
- Whether virtualization is enabled in BIOS/UEFI.
- Whether Microsoft Store access is blocked.
- Whether the machine is managed by school/company security policy.
- Whether the failure happens while installing WSL itself, installing the Linux
  distribution, or installing Ouroboros inside WSL.

## References

- Microsoft Learn: [Install WSL](https://learn.microsoft.com/en-us/windows/wsl/install)
- Microsoft Learn: [Basic commands for WSL](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)
- Microsoft Learn: [Manual installation steps for older versions of WSL](https://learn.microsoft.com/en-us/windows/wsl/install-manual)
