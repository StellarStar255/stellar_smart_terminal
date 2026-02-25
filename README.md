# Stellar Smart Terminal

A PyQt6-based terminal emulator with built-in file explorer, Git integration, LLM bridge, and VS Code extension management.

## Features

- Multi-tab terminal with session management
- File explorer and built-in code editor
- Git GUI (stage, commit, push, pull, diff, branch)
- OpenAI-compatible LLM proxy server
- VS Code extension browser
- i18n (English / Chinese)

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

## Usage

```bash
python app.py                # launch GUI
python app.py -c bash        # run a specific command
python app.py --list         # list saved sessions
python app.py --history      # browse session history
python app.py --export <id> --format html  # export a session
```

## Requirements

- Python 3.10+
- PyQt6 >= 6.5.0
- pyte >= 0.8.0

## License

[MIT](LICENSE)
