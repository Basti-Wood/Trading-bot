# Signal Room trading bot

A compact replacement for the original multi-bot/genetic-optimizer project. The implementation and deployment instructions are in [trading-bot/README.md](trading-bot/README.md).

It combines three open-source scikit-learn forecasting models with news analysis through your FreeLLM proxy at `https://ai.bastiwood.com/v1` using `auto`. The dashboard shows each model's forecasts, evidence, validation and order outcomes. Alpaca paper trading is the default.

The local forecasting models also learn from saved prediction inputs paired with subsequent observed market returns. Resolved experiences are replayed during scheduled retraining, with recent sessions reserved for validation. The Learning lab reports actual training-example counts and forward prediction errors.

Market discovery now screens Alpaca-listed tradable US equities and runs forecasts/news analysis on a bounded shortlist (20 by default). Daily investing and paper day trading run together with `TRADING_MODE=both`, sharing account limits while keeping positions, models and signals separate. Day trading uses five-minute bars, 15-minute forecasts, cooldowns and pre-close exits for its own positions. See the application README for configuration and limits.
