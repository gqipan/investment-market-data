"""Optional AkShare valuation adapter, bounded by the parent process timeout."""
from contextlib import redirect_stdout
import io
import json
import sys


def main():
    import akshare as ak
    import pandas as pd
    import requests
    original = requests.sessions.Session.request

    def bounded(self, method, url, **kwargs):
        kwargs['timeout'] = 15
        return original(self, method, url, **kwargs)
    requests.sessions.Session.request = bounded
    symbol = sys.argv[1]
    result = {}
    with redirect_stdout(io.StringIO()):
        for name, fetch in [('pe', ak.stock_index_pe_lg), ('pb', ak.stock_index_pb_lg)]:
            frame = fetch(symbol=symbol)
            frame['日期'] = frame['日期'].astype(str)
            # Missing values stay null; allow_nan=False catches infinities.
            result[name] = frame.astype(object).where(pd.notna(frame), None).to_dict(orient='records')
    result['version'] = ak.__version__
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
