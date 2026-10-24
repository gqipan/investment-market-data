"""Isolated optional dependency worker; the parent enforces the overall timeout."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import sys


def main() -> None:
    import akshare as ak

    symbol, start, end = sys.argv[1:]
    with redirect_stdout(io.StringIO()):
        frame = ak.stock_zh_index_daily_em(symbol=symbol, start_date=start, end_date=end)
    fields = ["date", "open", "high", "low", "close", "volume"]
    frame = frame[fields].copy()
    frame["date"] = frame["date"].astype(str)
    print(json.dumps({"bars": frame.to_dict(orient="records"), "version": ak.__version__}, allow_nan=False))


if __name__ == "__main__":
    main()
