"""Tiny GitHub Actions helpers shared by the Python action scripts. Stdlib only."""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Any


def _append(path_var: str, name: str, value: str) -> None:
    path = os.environ.get(path_var)
    delim = f"lsio_{secrets.token_hex(8)}"
    text = f"{name}<<{delim}\n{value}\n{delim}\n"
    if not path:
        sys.stdout.write(text)
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def set_output(name: str, value: Any) -> None:
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        value = json.dumps(value, separators=(",", ":"))
    elif value is None:
        value = ""
    _append("GITHUB_OUTPUT", name, str(value))


def set_outputs(values: dict[str, Any]) -> None:
    for k, v in values.items():
        set_output(k, v)


def set_env(name: str, value: str) -> None:
    _append("GITHUB_ENV", name, value)


def summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        sys.stdout.write(markdown + "\n")
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown + "\n")


def warning(message: str) -> None:
    print(f"::warning::{message}")


def error(message: str) -> None:
    print(f"::error::{message}")


def notice(message: str) -> None:
    print(f"::notice::{message}")
