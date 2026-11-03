"""Profile-scoped runtime data; never write beside installed Python modules."""
import os
from pathlib import Path


def antigravity_home() -> Path:
    configured = (
        os.environ.get('ANTIGRAVITY_HOME', '').strip()
        or os.environ.get('CODEX_HOME', '').strip()
        or os.environ.get('ANTIGRAVITY_APP_DATA_DIR', '').strip()
    )
    return (
        Path(os.path.expandvars(configured)).expanduser()
        if configured
        else Path.home() / '.gemini' / 'antigravity'
    ).resolve()


codex_home = antigravity_home


def antigravity_config_dir() -> Path:
    configured = os.environ.get('ANTIGRAVITY_CONFIG_DIR', '').strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve()
    home = antigravity_home()
    if home.name.lower() == 'antigravity' and home.parent.name.lower() == '.gemini':
        return (home.parent / 'config').resolve()
    return home


def hooks_json_path() -> Path:
    return antigravity_config_dir() / 'hooks.json'


def data_dir() -> Path:
    explicit = (
        os.environ.get('ANTIGRAVITY_TELEGRAM_DATA_DIR', '').strip()
        or os.environ.get('CODEX_TELEGRAM_DATA_DIR', '').strip()
    )
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser().resolve()
    profile = antigravity_home()
    # Reuse a legacy installation in place: credentials and all reply routes
    # remain valid. Ambiguity requires an explicit choice, never newest-wins.
    candidates = [
        p
        for p in (
            profile / 'telegram-bridge',
            profile / 'hooks',
            profile / 'hooks/codex-telegram-bot',
        )
        if (p / 'telegram_notify.credentials.json').is_file()
    ]
    if len(candidates) > 1:
        raise RuntimeError('Multiple Robot data directories; set ANTIGRAVITY_TELEGRAM_DATA_DIR explicitly.')
    return candidates[0] if candidates else profile / 'telegram-bridge'


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def resources_dir() -> Path:
    return Path(__file__).parent / 'resources'
