#!/usr/bin/python3
"""Install the managed shell hook without changing existing SSH settings."""

from pathlib import Path
import shutil


def main():
    directory = Path(__file__).resolve().parent
    shell = Path.home() / ".zshrc"
    start, end = "# BEGIN SSH AUDIO", "# END SSH AUDIO"
    original = shell.read_text() if shell.exists() else ""
    updated = original
    if start in updated:
        prefix, rest = updated.split(start, 1)
        _, suffix = rest.split(end, 1)
        updated = prefix.rstrip("\n") + suffix
    else:
        backup = Path(str(shell) + ".before-ssh-audio")
        if shell.exists() and not backup.exists():
            shutil.copy2(shell, backup)
    block = f'\n{start}\nsource "$HOME/.local/share/ssh-audio/shell.zsh"\n{end}\n'
    shell.write_text(updated.rstrip("\n") + block)
    executable = directory / "ssh_big_chungus.py"
    executable.chmod(0o755)
    command = Path.home() / ".local/bin/ssh-big-chungus"
    command.parent.mkdir(parents=True, exist_ok=True)
    if command.exists() and command.resolve() != executable:
        raise RuntimeError(f"Refusing to replace {command}")
    if not command.exists():
        command.symlink_to(executable)
    print("Installed automatic audio for ssh big-chungus. Open a new Mac terminal tab to use it.")


if __name__ == "__main__":
    main()
