# lpptools

*Lars Pontoppidan's Python Tools*

Python utilities for the Linux and MacOS CLI.

## Contents

- autotag: Tag music files based on folder and filenames
- tracksplit: Split an audio file into tracks based on quiet gaps.

## Install

Install a tool, for example "autotag", with:

```bash
pipx install "git+https://github.com/larspontoppidan/lpptools.git@main#subdirectory=autotag"
```

For local development, clone the repository, then:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e autotag
pip install -e tracksplit
autotag --help
tracksplit --help
```

