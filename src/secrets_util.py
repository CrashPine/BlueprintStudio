"""
Load API keys without ever committing them.
Anthropic: env ANTHROPIC_API_KEY -> models/secretapi.txt -> .env
Roboflow:  env ROBOFLOW_API_KEY  -> models/roboflow_key.txt -> .env
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_FILE = ROOT / "models" / "secretapi.txt"
ROBOFLOW_FILE = ROOT / "models" / "roboflow_key.txt"
ENV_FILE = ROOT / ".env"


def _read_first_line(path, env_name=None):
    """Return the first usable line. Accepts `KEY=value` or a bare key."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # tolerate KEY=value or bare key
        if env_name and env_name in line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
        if "=" in line and line.split("=", 1)[0].strip().isupper():
            # some other KEY=value line we don't want — skip when scanning for a specific key
            if env_name:
                continue
            return line.split("=", 1)[1].strip().strip('"').strip("'")
        return line.strip().strip('"').strip("'")
    return None


def get_anthropic_key():
    """Return the Anthropic API key or None (caller decides to fall back)."""
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env.strip()

    if SECRET_FILE.exists():
        key = _read_first_line(SECRET_FILE, "ANTHROPIC_API_KEY")
        if key:
            return key

    if ENV_FILE.exists():
        key = _read_first_line(ENV_FILE, "ANTHROPIC_API_KEY")
        if key:
            return key

    return None


def has_anthropic_key():
    return get_anthropic_key() is not None


def get_roboflow_key():
    """Return the Roboflow API key or None (caller decides to fall back)."""
    env = os.environ.get("ROBOFLOW_API_KEY")
    if env:
        return env.strip()

    if ROBOFLOW_FILE.exists():
        key = _read_first_line(ROBOFLOW_FILE, "ROBOFLOW_API_KEY")
        if key:
            return key

    if ENV_FILE.exists():
        key = _read_first_line(ENV_FILE, "ROBOFLOW_API_KEY")
        if key:
            return key

    return None


def has_roboflow_key():
    return get_roboflow_key() is not None
