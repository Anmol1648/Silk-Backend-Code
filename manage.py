#!/usr/bin/env python
"""FundOS backend management entry point."""
import os
import sys
from pathlib import Path


def _preload_env():
    for p in (Path(__file__).resolve().parent / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):].lstrip()
                    k, s, v = line.partition("=")
                    if s and k.strip() and k.strip() not in os.environ:
                        v = v.strip()
                        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                            v = v[1:-1]
                        os.environ[k.strip()] = v
            except Exception:
                pass
            break

_preload_env()


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fundos.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Is it installed and on PYTHONPATH?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
