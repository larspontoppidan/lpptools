#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile

from rich.console import Console
from rich.markdown import Markdown

usage = """
Show a markdown document with rich in terminal, offering to run bash code blocks.

For example, this bash code block will be executed if the user accepts:

```bash
echo "This will be executed, if user accepts"
```

The markdown --- horizontal rule can be used to show a continue yes/no prompt.

The environment is persisted between bash code block sessions.

bashdown can be used in the shebang in .md markdown files on Linux and MacOS platforms.
"""


def _version_str() -> str:
    try:
        return importlib.metadata.version("bashdown")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


__version__ = _version_str()


def welcome():
    print("bashdown %s" % __version__)


CONSOLE_WIDTH = 110

console = Console()


def splitMarkdown(content: str) -> list[tuple[str, list[str]]]:
    """Split markdown into markdown and commands blocks."""
    blocks: list[tuple[str, list[str]]] = []
    md = []
    cmds = []
    in_block = 0  # 0: no block, 1: other block 2: cmd block
    first_line = True
    for line in content.splitlines():
        # Always add full line to the markdown
        if first_line and line.startswith("#!"):
            pass  # skip shebang
        else:
            md.append(line)
            stripped = line.strip()
            if in_block:
                if stripped == "```":
                    if in_block == 2:
                        blocks.append(("\n".join(md), cmds))
                        md = []
                        cmds = []
                    in_block = 0
                elif in_block == 2:
                    cmds.append(stripped)
            elif stripped.startswith("```"):
                in_block = 2 if stripped == "```bash" else 1
            elif stripped == "---":
                # Add the block without any code, removing the rule
                blocks.append(("\n".join(md[:-1]), []))
                md = []
        first_line = False
    if md:
        blocks.append(("\n".join(md), []))
    return blocks


def _abort():
    print("----- Aborted -----")
    sys.exit()


def _userYesNoAbort(description) -> bool:
    try:
        r = input(f"{description} [Yna]: ").strip().lower()
        while True:
            if r == "" or r == "y":
                return True
            elif r == "n":
                return False
            elif r == "a":
                _abort()
            r = input(
                f"{description} Please choose: (y)es, (n)o or (a)bort. Press Enter for yes: "
            ).strip().lower()
    except KeyboardInterrupt:
        _abort()


def _userYesNo(description):
    try:
        r = input(f"{description} [Yn]: ").strip().lower()
        while True:
            if r == "" or r == "y":
                return True
            elif r == "n":
                return False
            r = input(
                f"{description} Please choose: (y)es, (n)o. Press Enter for yes: "
            ).strip().lower()
    except KeyboardInterrupt:
        _abort()


def _loadEnvFromFile(path: str) -> None:
    """Update os.environ from NUL-separated key=value entries (env -0)."""
    with open(path, "rb") as f:
        data = f.read()
    for entry in data.split(b"\0"):
        if not entry:
            continue
        key_b, sep, value_b = entry.partition(b"=")
        if not sep:
            continue
        key = key_b.decode("utf-8", "replace")
        # Skip exported bash functions; they're multi-line and rarely useful to round-trip.
        if key.startswith("BASH_FUNC_"):
            continue
        os.environ[key] = value_b.decode("utf-8", "replace")


def _bashExecutable() -> str:
    return shutil.which("bash") or "/bin/bash"


def _executeBlockPersistEnv(cmds: str):
    fd, env_file = tempfile.mkstemp()
    os.close(fd)
    # Run with bash, after commands dump env so we can adapt the values
    script = f'''set -e
{cmds}
env -0 > "{env_file}"
'''
    result = subprocess.run(
        script, shell=True, executable=_bashExecutable(), env=os.environ.copy()
    )
    if os.path.exists(env_file):
        # Don't load the environment from the script if it fails
        if result.returncode == 0:
            _loadEnvFromFile(env_file)
        os.unlink(env_file)
    return result.returncode


def _runBlock(md: str, cmds: list[str], last_block: bool = False):
    console.print(Markdown(md), width=CONSOLE_WIDTH)
    if len(cmds) > 0:
        plural = "s" if len(cmds) > 1 else ""
        if _userYesNoAbort(f">>>>> Execute command{plural}?"):
            err = ""
            while True:
                try:
                    returncode = _executeBlockPersistEnv("\n".join(cmds))
                    if returncode != 0:
                        err = f"<<<<< Command failed with return code {returncode}"
                    else:
                        print(f"<<<<< Command{plural} done")
                        break
                        # if _userYesNo(f"<<<<< Done, continue?"):
                        #     break
                        # else:
                        #     sys.exit()
                except KeyboardInterrupt:
                    err = f"<<<<< Command{plural} aborted"
                if err:
                    if not _userYesNoAbort(err + ", retry?"):
                        break
        else:
            print(f"<<<<< Command{plural} skipped")
    elif not last_block:
        print()
        if not _userYesNo("Continue?"):
            sys.exit()
    print()


def main() -> int:
    if "--version" in sys.argv:
        welcome()
        return 0
    if len(sys.argv) < 2:
        welcome()
        print(usage)
        return 0
    with open(sys.argv[1], encoding="utf-8") as f:
        content = f.read()
    blocks = splitMarkdown(content)
    for i, (md, cmds) in enumerate(blocks):
        _runBlock(md, cmds, last_block=(i == len(blocks) - 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
