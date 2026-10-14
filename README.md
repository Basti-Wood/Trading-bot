# Signal Room trading bot

A compact replacement for the original multi-bot/genetic-optimizer project. The implementation and deployment instructions are in [trading-bot/README.md](trading-bot/README.md).

It combines three open-source scikit-learn forecasting models with news analysis through your FreeLLM proxy at `https://ai.bastiwood.com/v1` using `auto`. The dashboard shows each model's forecasts, evidence, validation and order outcomes. Alpaca paper trading is the default.

The local forecasting models also learn from saved prediction inputs paired with subsequent observed market returns. Resolved experiences are replayed during scheduled retraining, with recent sessions reserved for validation. The Learning lab reports actual training-example counts and forward prediction errors.
