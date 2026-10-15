from dataclasses import dataclass
import math
import os
from pathlib import Path
import re


def flag(name, default=False):
    value = os.getenv(name, str(default)).lower().strip()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("data")
    symbols: tuple = ("SPY", "QQQ", "IWM")
    trading_mode: str = "daily"
    daytrade_capital_share: float = .5
    queue_daily_orders: bool = True
    # Internal child-strategy settings; the public mode may run both together.
    coordinated: bool = False
    strategy_fraction: float = 1.
    universe_mode: str = "market"
    scan_candidates: int = 20
    scan_interval_minutes: int = 30
    scan_min_price: float = 5
    scan_min_dollar_volume: float = 1000000
    daytrade_max_positions: int = 3
    daytrade_max_entries: int = 3
    daytrade_cooldown_minutes: int = 15
    daytrade_hold_minutes: int = 15
    daytrade_stop_loss: float = .01
    daytrade_take_profit: float = .02
    daytrade_close_minutes: int = 10
    api_key: str = ""
    api_secret: str = ""
    paper: bool = True
    allow_live: bool = False
    password: str = ""
    host: str = "127.0.0.1"
    port: int = 1337
    feed: str = "iex"
    history_days: int = 1800
    max_bars: int = 1500
    retrain_hours: int = 24
    training_timeout: int = 900
    training_memory_mb: int = 4096
    capital: float = 10000
    position_fraction: float = .10
    gross_fraction: float = .30
    daily_loss: float = .02
    stop_loss: float = .05
    take_profit: float = .10
    cost_bps: float = 10
    edge_bps: float = 10
    max_drawdown: float = .15
    min_trades: int = 5
    feedback_weight: float = 2.0
    validation_bars: int = 30
    llm_url: str = "https://ai.bastiwood.com/v1"
    llm_key: str = ""
    llm_models: tuple = ("auto",)
    brave_key: str = ""
    origins: tuple = ("https://trade.bastiwood.com",)

    def __post_init__(self):
        if not self.symbols or len(self.symbols) > 50 or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Configure 1 to 50 unique US equity/ETF symbols")
        if self.trading_mode not in {"daily", "daytrade", "both"} or self.universe_mode not in {"watchlist", "market"}:
            raise ValueError("TRADING_MODE must be daily/daytrade/both; UNIVERSE_MODE must be watchlist/market")
        if self.trading_mode in {"daytrade", "both"} and not self.paper:
            raise ValueError("The daytrade strategy is currently paper-only")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) for s in self.symbols):
            raise ValueError("Only US equity/ETF symbols are supported")
        if self.feed not in {"iex", "sip"}:
            raise ValueError("ALPACA_DATA_FEED must be iex or sip")
        if not self.llm_url.startswith(("http://", "https://")) or len(self.llm_models) > 2:
            raise ValueError("Set an HTTP FreeLLM base URL and at most two LLM models")
        if not self.paper and not self.allow_live:
            raise ValueError("Live trading requires ALLOW_LIVE_TRADING=true as well as ALPACA_PAPER=false")
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.password:
            raise ValueError("DASHBOARD_PASSWORD is required for a non-loopback host")
        bounds = {"capital": (100, 1000000), "position_fraction": (.01, .20),
                  "gross_fraction": (.01, .80), "daily_loss": (.001, .10),
                  "stop_loss": (.005, .10), "take_profit": (.01, .30),
                  "cost_bps": (1, 100), "edge_bps": (0, 100),
                  "training_memory_mb": (256, 4096), "training_timeout": (10, 1800),
                  "history_days": (400, 3000), "max_bars": (300, 2000),
                  "retrain_hours": (12, 168), "port": (1, 65535), "feedback_weight": (1, 3),
                  "validation_bars": (20, 120), "scan_candidates": (1, 50),
                  "scan_interval_minutes": (15, 1440), "scan_min_price": (1, 10000),
                  "scan_min_dollar_volume": (100000, 10000000000),
                  "daytrade_max_positions": (1, 10), "daytrade_max_entries": (1, 20),
                  "daytrade_cooldown_minutes": (5, 120), "daytrade_hold_minutes": (5, 60),
                  "daytrade_stop_loss": (.005, .05), "daytrade_take_profit": (.01, .10),
                  "daytrade_close_minutes": (5, 60), "daytrade_capital_share": (.05, .95),
                  "strategy_fraction": (.05, 1)}
        for name, (lo, hi) in bounds.items():
            value = getattr(self, name)
            if not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}")
        if self.position_fraction > self.gross_fraction:
            raise ValueError("Position limit cannot exceed gross exposure limit")

    @property
    def configured(self):
        return all(v and not v.lower().startswith(("your_", "changeme")) for v in (self.api_key, self.api_secret))

    @property
    def threshold(self):
        return (2 * self.cost_bps + self.edge_bps) / 10000

    @property
    def intraday(self):
        return self.trading_mode == "daytrade"

    @property
    def effective_stop(self):
        return self.daytrade_stop_loss if self.intraday else self.stop_loss

    @property
    def effective_take_profit(self):
        return self.daytrade_take_profit if self.intraday else self.take_profit

    @property
    def poll_seconds(self):
        return 60 if self.trading_mode == "daily" else 15

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "data-daytrade" if os.getenv("TRADING_MODE") == "daytrade" else "data")),
            symbols=tuple(s.strip().upper() for s in os.getenv("SYMBOLS", "SPY,QQQ,IWM").split(",") if s.strip()),
            trading_mode=os.getenv("TRADING_MODE", "daily"), universe_mode=os.getenv("UNIVERSE_MODE", "market"),
            daytrade_capital_share=float(os.getenv("DAYTRADE_CAPITAL_SHARE", ".5")),
            queue_daily_orders=flag("QUEUE_DAILY_ORDERS", True),
            scan_candidates=int(os.getenv("SCAN_CANDIDATES", "20")),
            scan_interval_minutes=int(os.getenv("SCAN_INTERVAL_MINUTES", "30")),
            scan_min_price=float(os.getenv("SCAN_MIN_PRICE", "5")),
            scan_min_dollar_volume=float(os.getenv("SCAN_MIN_DOLLAR_VOLUME", "1000000")),
            daytrade_max_positions=int(os.getenv("DAYTRADE_MAX_POSITIONS", "3")),
            daytrade_max_entries=int(os.getenv("DAYTRADE_MAX_ENTRIES_PER_SYMBOL", "3")),
            daytrade_cooldown_minutes=int(os.getenv("DAYTRADE_COOLDOWN_MINUTES", "15")),
            daytrade_hold_minutes=int(os.getenv("DAYTRADE_MAX_HOLD_MINUTES", "15")),
            daytrade_stop_loss=float(os.getenv("DAYTRADE_STOP_LOSS", ".01")),
            daytrade_take_profit=float(os.getenv("DAYTRADE_TAKE_PROFIT", ".02")),
            daytrade_close_minutes=int(os.getenv("DAYTRADE_FLATTEN_MINUTES", "10")),
            api_key=os.getenv("ALPACA_API_KEY", "").strip(), api_secret=os.getenv("ALPACA_SECRET_KEY", "").strip(),
            paper=flag("ALPACA_PAPER", True), allow_live=flag("ALLOW_LIVE_TRADING"),
            password=os.getenv("DASHBOARD_PASSWORD", ""), host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "1337")), feed=os.getenv("ALPACA_DATA_FEED", "iex"),
            history_days=int(os.getenv("HISTORY_DAYS", "1800")), max_bars=int(os.getenv("MAX_BARS", "1500")),
            retrain_hours=int(os.getenv("RETRAIN_HOURS", "24")),
            training_timeout=int(os.getenv("TRAINING_TIMEOUT_SECONDS", "900")),
            training_memory_mb=int(os.getenv("TRAINING_MEMORY_MB", "4096")),
            feedback_weight=float(os.getenv("FEEDBACK_SAMPLE_WEIGHT", "2")),
            validation_bars=int(os.getenv("VALIDATION_BARS", "30")),
            capital=float(os.getenv("MAX_CAPITAL_USD", "10000")),
            position_fraction=float(os.getenv("MAX_POSITION_FRACTION", ".10")),
            gross_fraction=float(os.getenv("MAX_GROSS_FRACTION", ".30")),
            daily_loss=float(os.getenv("MAX_DAILY_LOSS", ".02")),
            stop_loss=float(os.getenv("STOP_LOSS", ".05")), take_profit=float(os.getenv("TAKE_PROFIT", ".10")),
            cost_bps=float(os.getenv("COST_BPS_PER_SIDE", "10")), edge_bps=float(os.getenv("MIN_EDGE_BPS", "10")),
            llm_url=os.getenv("FREELLM_BASE_URL", "https://ai.bastiwood.com/v1").rstrip("/"),
            llm_key=os.getenv("FREELLM_API_KEY", ""),
            llm_models=tuple(s.strip() for s in os.getenv("FREELLM_MODELS", "auto").split(",") if s.strip()),
            brave_key=os.getenv("BRAVE_SEARCH_API_KEY", ""),
            origins=tuple(s.strip().rstrip("/").lower() for s in os.getenv("DASHBOARD_ORIGINS", "https://trade.bastiwood.com").split(",") if s.strip()))
