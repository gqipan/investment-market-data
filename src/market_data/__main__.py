"""Allow ``python -m market_data`` during local development."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
