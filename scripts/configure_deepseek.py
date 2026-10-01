"""Configure DeepSeek locally with hidden key input; this helper makes no API calls."""

from __future__ import annotations

import argparse
import getpass
import sys
import warnings

from openreview_cli.config.auth import load_auth, save_key
from openreview_cli.config.loader import load_config, set_config_value
from openreview_cli.config.paths import get_config_dir

CURRENT_MODELS = ("deepseek-flash", "deepseek-v4-pro")


def configure(model: str, *, include_grounding: bool) -> int:
    """Preserve unrelated settings and save the key outside the Git worktree."""
    if model not in CURRENT_MODELS:
        print("Choose a current DeepSeek model.", file=sys.stderr)
        return 1

    try:
        config_dir = get_config_dir()
        config_path = config_dir / "config.yml"
        auth_path = config_dir / "auth.json"
        # Check existing files before asking for a secret. Raw parse errors may
        # include sensitive file contents, so only fixed error text is emitted.
        load_config(config_path)
        load_auth(auth_path)
        with warnings.catch_warnings():
            # Never permit getpass to fall back to visibly echoed stdin.
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("DeepSeek API key (hidden; saved in user config): ").strip()
        if not key:
            print("No key saved: an API key is required.", file=sys.stderr)
            return 1

        save_key(auth_path, "deepseek", key)
        slots = ["extraction", "reasoning"]
        if include_grounding:
            slots.append("grounding")
        for slot in slots:
            set_config_value(config_path, f"gateway.models.{slot}.primary", f"deepseek/{model}")
            set_config_value(config_path, f"gateway.models.{slot}.fallback", "null")
            # Extraction/QA expect objects; grounding's prompt expects an array.
            set_config_value(config_path, f"gateway.models.{slot}.extra_params", "null")
            if slot in ("extraction", "reasoning"):
                set_config_value(
                    config_path,
                    f"gateway.models.{slot}.extra_params.response_format.type",
                    "json_object",
                )
        set_config_value(config_path, "privacy.tier", "balanced")
        set_config_value(config_path, "privacy.strip_pii", "true")
    except (KeyboardInterrupt, EOFError):
        print("Setup cancelled.", file=sys.stderr)
        return 1
    except Exception:
        print(
            "Setup could not finish. Check local configuration file access and "
            "run from an interactive terminal. No error details or key values are displayed.",
            file=sys.stderr,
        )
        return 1

    print("DeepSeek configuration saved in the user configuration directory.")
    print("No API request was made; connectivity and real model output are still unverified.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=CURRENT_MODELS, default="deepseek-flash")
    parser.add_argument(
        "--no-grounding",
        action="store_true",
        help="Keep the existing grounding slot unchanged.",
    )
    args = parser.parse_args()
    return configure(args.model, include_grounding=not args.no_grounding)


if __name__ == "__main__":
    raise SystemExit(main())
