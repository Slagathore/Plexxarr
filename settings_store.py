# =============================================================================
# settings_store.py
# =============================================================================
# Persists user-editable settings back to the .env file used by config.py.
# Wraps python-dotenv's set_key/unset_key so existing comments and ordering in
# the .env file are preserved when individual values are edited from the UI.
#
# Public interface:
#   save_settings(updates, dotenv_path)
#       updates: mapping of {ENV_VAR_NAME: str_value}.
#       Empty-string values delete the key from the file.
#       Non-string values are coerced via str(...).
#
# Note: changes do NOT live-reload into the running config module. The desktop
# app shows a "restart required" dialog after saving so the user knows to
# relaunch to pick up the new values.
# =============================================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)


def _ensure_dotenv_exists(dotenv_path: Path) -> None:
    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    if not dotenv_path.is_file():
        dotenv_path.touch()


def save_settings(updates: Mapping[str, object], dotenv_path: Path) -> None:
    """
    Apply a batch of {key: value} updates to the .env file.

    - Values are coerced to str.
    - An empty string removes the key from the file (so config.py falls back
      to its default the next time it loads).
    - Comments and other unrelated keys in the .env file are preserved.

    Raises RuntimeError on failure with a useful message.
    """
    try:
        from dotenv import dotenv_values, set_key, unset_key
    except ImportError as exc:
        raise RuntimeError(
            "python-dotenv is required to save settings. "
            "Install it with: pip install python-dotenv"
        ) from exc

    _ensure_dotenv_exists(dotenv_path)
    env_path_str = str(dotenv_path)

    # Snapshot existing keys so we don't ask dotenv to unset a key that isn't
    # there (which logs a noisy "key not removed" warning).
    existing_keys = set(dotenv_values(env_path_str).keys())

    errors: list[str] = []
    for key, raw_value in updates.items():
        value = "" if raw_value is None else str(raw_value)
        try:
            if value == "":
                if key in existing_keys:
                    unset_key(env_path_str, key)
            else:
                # quote_mode="auto" only adds quotes when the value needs them
                # (whitespace, special chars). Leaves tokens/keys un-quoted.
                set_key(env_path_str, key, value, quote_mode="auto")
        except Exception as exc:  # noqa: BLE001 — bubble per-key failures
            logger.exception("Failed to write %s to %s", key, env_path_str)
            errors.append(f"{key}: {exc}")

    if errors:
        raise RuntimeError("Some settings failed to save:\n" + "\n".join(errors))


def load_current_settings(dotenv_path: Path) -> dict[str, str]:
    """
    Read the current .env into a flat dict. Useful for populating the Settings
    UI with the values actually persisted on disk (not the values in memory,
    which may differ if .env was edited outside the app).
    """
    if not dotenv_path.is_file():
        return {}

    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}

    raw = dotenv_values(str(dotenv_path))
    return {k: ("" if v is None else v) for k, v in raw.items()}


def reload_config_from_env(dotenv_path: Path) -> None:
    """
    Re-apply the .env file to os.environ, then reload the `config` module so
    its module-level constants pick up the new values. After this call,
    every `config.<NAME>` access in the running app sees the saved values
    without an app restart.

    Caveats:
      - Values cached at startup (e.g. the Telegram bot token, which is
        baked into the running Application) are NOT updated. Restart for
        those.
      - All other modules in this project access settings as `config.NAME`
        at call time, so they see the updated values automatically.
    """
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError("python-dotenv is required to reload settings") from exc

    if dotenv_path.is_file():
        load_dotenv(str(dotenv_path), override=True)

    import importlib

    import config as _config
    importlib.reload(_config)
