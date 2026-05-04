# lpptools

*Lars Pontoppidan's Python Tools*

Python utilities for the Linux and MacOS CLI.

## Contents

- `autotag`: Tag music files based on folder and filenames
- `tracksplit`: Split an audio file into tracks based on quiet gaps
- `alreadythere`: Check whether files in input folders exist in a search folder (by hash)
- `bashdown`: Show a markdown document with rich in terminal, offering to run bash code blocks

## Install

Install with:

```bash
pipx install "git+https://github.com/larspontoppidan/lpptools.git@main#subdirectory=autotag"
pipx install "git+https://github.com/larspontoppidan/lpptools.git@main#subdirectory=tracksplit"
pipx install "git+https://github.com/larspontoppidan/lpptools.git@main#subdirectory=alreadythere"
pipx install "git+https://github.com/larspontoppidan/lpptools.git@main#subdirectory=bashdown"
```

For local development, clone the repository, then:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e autotag
pip install -e tracksplit
pip install -e alreadythere
pip install -e bashdown
autotag -h
tracksplit -h
alreadythere -h
bashdown
```
