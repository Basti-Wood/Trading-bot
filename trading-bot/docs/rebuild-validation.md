# Rebuild validation

Verified on 22 October 2026 in an isolated Python virtual environment using `requirements.txt` (the pre-existing lock file was not regenerated).

- `python -m unittest discover -s tests -v`: **210 tests passed** (19.791 seconds). The environment used `OMP_NUM_THREADS=2`, `OPENBLAS_NUM_THREADS=2` and a writable temporary directory.
- `node --check dashboard/app.js`: passed.
- Dashboard DOM smoke check with synthetic API fixtures: passed; all referenced elements exist, 30 candles produce 60 candle/volume rectangles, per-stock loop and news rows render, removed sections are absent, and SVG coordinates contain no nonfinite values.
- Generated candlestick SVG parsed as valid XML. Empty-session behavior was also checked.
- `git diff --check` and Python compilation: passed.

The focused regressions cover the former red LISTE group gate, per-stock return/drawdown selection, winner signals, simultaneous versions of one idea, owning-version exits, file/risk changes, per-stock news retries and caching, one news vote, session/half-day candle filtering, indicator agreement with the playbook, stale/mismatched forecasts, model expiry, evidence timestamps, authentication and removed endpoints.

A real browser render could not be checked: Chromium was absent, and its download returned an invalid archive in this environment. The JavaScript/DOM/SVG checks above do not substitute for a visual browser review.

No real broker order was submitted. Tests use synthetic prices and mocked broker/LLM responses. API entitlements, the installed Llama model ID, real data latency, live paper fills and trading profitability were not verified.
