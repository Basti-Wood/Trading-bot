from dataclasses import dataclass
import math
import os
from pathlib import Path
import re


CHILD_MODES = ("loop", "loop_day")


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
    daytrade_cooldown_minutes: int = 5
    daytrade_hold_minutes: int = 15
    daytrade_stop_loss: float = .01
    daytrade_take_profit: float = .02
    daytrade_close_minutes: int = 10
    # Winners may outlive the base holding period while their votes stay bullish.
    daytrade_run_minutes: int = 60
    daytrade_trail_trigger: float = .006
    daytrade_trail_gap: float = .004
    # Of four voters (three models plus LLM news or the day-trading setup).
    min_votes: int = 3
    # In a 20-session replay only the two published setups had a positive gross edge.
    daytrade_strategies: tuple = ("orb", "noise_breakout")
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
    # Expire each stock's trained model independently, even if its inputs are recent.
    model_max_age_hours: int = 168
    training_timeout: int = 900
    training_memory_mb: int = 4096
    # Sizing follows the account: this share of current equity, optionally capped in dollars (0 = no cap).
    capital_fraction: float = 1.
    capital: float = 0
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
    llm_models: tuple = ()
    origins: tuple = ("https://trade.bastiwood.com",)
    # Read-only API for an external assistant (e.g. a GPT Action); empty = disabled.
    readonly_token: str = ""
    readonly_daily_limit: int = 100
    # Trading-loop folder (strategien/, berichte/, risiko.py); None = no loop strategies available.
    loop_dir: Path = None
    loop_timeout: int = 300
    trading212_enabled: bool = False
    trading212_key: str = ""
    trading212_secret: str = ""
    trading212_paper: bool = True
    trading212_allow_live: bool = False
    trading212_instruments: Path = None
    trading212_capital_fraction: float = 1.
    trading212_capital_cap: float = 0.
    trading212_allow_fx: bool = False
    twelvedata_key: str = ""
    reporting_currency: str = "EUR"
    def __post_init__(self):
        if self.trading212_enabled and self.trading_mode != "both" and not self.coordinated:
            raise ValueError("Trading 212's trading-loop requires TRADING_MODE=both")
        if not re.fullmatch(r"[A-Z]{3}", self.reporting_currency):
            raise ValueError("REPORTING_CURRENCY must be a three-letter currency code")
        if not math.isfinite(self.trading212_capital_fraction) or not .01 <= self.trading212_capital_fraction <= 1:
            raise ValueError("TRADING212_CAPITAL_FRACTION must be between .01 and 1")
        if not math.isfinite(self.trading212_capital_cap) or not 0 <= self.trading212_capital_cap <= 10000000:
            raise ValueError("TRADING212_CAPITAL_CAP must be between 0 and 10000000 in account currency")
        if not self.symbols or len(self.symbols) > 50 or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Configure 1 to 50 unique US equity/ETF symbols")
        # "loop" is an internal child strategy of TRADING_MODE=both, never standalone modes.
        if self.trading_mode not in {"daily", "daytrade", "both", *CHILD_MODES} or self.universe_mode not in {"watchlist", "market"}:
            raise ValueError("TRADING_MODE must be daily/daytrade/both; UNIVERSE_MODE must be watchlist/market")
        if self.trading_mode in CHILD_MODES and not self.coordinated:
            raise ValueError("The trading-loop strategies run only inside TRADING_MODE=both")
        if self.trading_mode in {"daytrade", "both", *CHILD_MODES} and not self.paper:
            raise ValueError("The daytrade and trading-loop strategies are currently paper-only")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) for s in self.symbols):
            raise ValueError("Only US equity/ETF symbols are supported")
        if self.feed not in {"iex", "sip"}:
            raise ValueError("ALPACA_DATA_FEED must be iex or sip")
        if not self.llm_url.startswith(("http://", "https://")) or len(self.llm_models) > 1:
            raise ValueError("Set an HTTP FreeLLM base URL and at most one Llama model")
        if not self.paper and not self.allow_live:
            raise ValueError("Live trading requires ALLOW_LIVE_TRADING=true as well as ALPACA_PAPER=false")
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.password:
            raise ValueError("DASHBOARD_PASSWORD is required for a non-loopback host")
        if not math.isfinite(self.capital) or self.capital != 0 and not 100 <= self.capital <= 10000000:
            raise ValueError("MAX_CAPITAL_USD must be 0 (no cap) or between 100 and 10000000")
        if not set(self.daytrade_strategies) <= {"orb", "noise_breakout", "vwap_pullback", "vwap_reversion"}:
            raise ValueError("DAYTRADE_STRATEGIES may contain orb, noise_breakout, vwap_pullback, vwap_reversion")
        bounds = {"capital_fraction": (.01, 1), "position_fraction": (.01, .20),
                  "gross_fraction": (.01, .80), "daily_loss": (.001, .10),
                  "stop_loss": (.005, .10), "take_profit": (.01, .30),
                  "cost_bps": (1, 100), "edge_bps": (0, 100),
                  "training_memory_mb": (256, 4096), "training_timeout": (10, 1800),
                  "history_days": (400, 3000), "max_bars": (300, 2000),
                  "retrain_hours": (12, 168), "model_max_age_hours": (12, 720), "port": (1, 65535), "feedback_weight": (1, 3),
                  "validation_bars": (20, 120), "scan_candidates": (1, 50),
                  "scan_interval_minutes": (15, 1440), "scan_min_price": (1, 10000),
                  "scan_min_dollar_volume": (100000, 10000000000),
                  "daytrade_max_positions": (1, 10), "daytrade_max_entries": (1, 20),
                  "daytrade_cooldown_minutes": (5, 120), "daytrade_hold_minutes": (5, 60),
                  "daytrade_stop_loss": (.005, .05), "daytrade_take_profit": (.01, .10),
                  "daytrade_close_minutes": (5, 60), "daytrade_capital_share": (.05, .95),
                  "strategy_fraction": (.05, 1), "min_votes": (2, 4),
                  "daytrade_run_minutes": (5, 240), "daytrade_trail_trigger": (.001, .05),
                  "daytrade_trail_gap": (.001, .05), "loop_timeout": (10, 1800)}
        for name, (lo, hi) in bounds.items():
            value = getattr(self, name)
            if not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}")
        if self.readonly_token and (len(self.readonly_token) < 32 or not re.fullmatch(r"[A-Za-z0-9_\-.~]+", self.readonly_token)
                                    or self.readonly_token in {self.password, self.api_key, self.api_secret, self.llm_key}):
            raise ValueError("READONLY_API_TOKEN must be at least 32 letters/digits (-_.~ allowed) and differ from every other secret")
        if not 1 <= self.readonly_daily_limit <= 5000:
            raise ValueError("READONLY_API_DAILY_LIMIT must be between 1 and 5000")
        if self.position_fraction > self.gross_fraction:
            raise ValueError("Position limit cannot exceed gross exposure limit")
        if self.daytrade_run_minutes < self.daytrade_hold_minutes:
            raise ValueError("DAYTRADE_LET_WINNERS_RUN_MINUTES cannot be shorter than DAYTRADE_MAX_HOLD_MINUTES")
        if self.daytrade_trail_gap > self.daytrade_trail_trigger:
            raise ValueError("DAYTRADE_TRAIL_GAP cannot exceed DAYTRADE_TRAIL_TRIGGER; the trail would lock in a loss")

    def sizing_capital(self, equity):
        """The capital this bot may use at the current account equity."""
        base = max(0., equity) * self.capital_fraction
        return min(base, self.capital) if self.capital else base

    @property
    def loop(self):
        """Trades tested trading-loop stock strategy files (daily or intraday candles)."""
        return self.trading_mode in {"loop", "loop_day"}

    @property
    def loop_intraday(self):
        """The intraday trading-loop: strategy files with ZEITRAHMEN below one day."""
        return self.trading_mode == "loop_day"

    @property
    def same_day(self):
        """Strategies that trade inside one session and are flat before the close."""
        return self.trading_mode in {"daytrade", "loop_day"}


    @property
    def configured(self):
        return all(v and not v.lower().startswith(("your_", "changeme")) for v in (self.api_key, self.api_secret))

    @property
    def trading212_configured(self):
        return self.trading212_enabled and all(v and not v.lower().startswith(("your_", "changeme"))
                                              for v in (self.trading212_key, self.trading212_secret))

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
            daytrade_cooldown_minutes=int(os.getenv("DAYTRADE_COOLDOWN_MINUTES", "5")),
            daytrade_hold_minutes=int(os.getenv("DAYTRADE_MAX_HOLD_MINUTES", "15")),
            daytrade_stop_loss=float(os.getenv("DAYTRADE_STOP_LOSS", ".01")),
            daytrade_take_profit=float(os.getenv("DAYTRADE_TAKE_PROFIT", ".02")),
            daytrade_close_minutes=int(os.getenv("DAYTRADE_FLATTEN_MINUTES", "10")),
            daytrade_run_minutes=int(os.getenv("DAYTRADE_LET_WINNERS_RUN_MINUTES", "60")),
            daytrade_trail_trigger=float(os.getenv("DAYTRADE_TRAIL_TRIGGER", ".006")),
            daytrade_trail_gap=float(os.getenv("DAYTRADE_TRAIL_GAP", ".004")),
            min_votes=int(os.getenv("MIN_AGREEING_VOTES", "3")),
            daytrade_strategies=tuple(s.strip() for s in os.getenv("DAYTRADE_STRATEGIES", "orb,noise_breakout").split(",") if s.strip()),
            api_key=os.getenv("ALPACA_API_KEY", "").strip(), api_secret=os.getenv("ALPACA_SECRET_KEY", "").strip(),
            paper=flag("ALPACA_PAPER", True), allow_live=flag("ALLOW_LIVE_TRADING"),
            password=os.getenv("DASHBOARD_PASSWORD", ""), host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "1337")), feed=os.getenv("ALPACA_DATA_FEED", "iex"),
            history_days=int(os.getenv("HISTORY_DAYS", "1800")), max_bars=int(os.getenv("MAX_BARS", "1500")),
            retrain_hours=int(os.getenv("RETRAIN_HOURS", "24")),
            model_max_age_hours=int(os.getenv("MODEL_MAX_AGE_HOURS", "168")),
            training_timeout=int(os.getenv("TRAINING_TIMEOUT_SECONDS", "900")),
            training_memory_mb=int(os.getenv("TRAINING_MEMORY_MB", "4096")),
            feedback_weight=float(os.getenv("FEEDBACK_SAMPLE_WEIGHT", "2")),
            validation_bars=int(os.getenv("VALIDATION_BARS", "30")),
            capital=float(os.getenv("MAX_CAPITAL_USD", "0")),
            capital_fraction=float(os.getenv("CAPITAL_FRACTION", "1")),
            position_fraction=float(os.getenv("MAX_POSITION_FRACTION", ".10")),
            gross_fraction=float(os.getenv("MAX_GROSS_FRACTION", ".30")),
            daily_loss=float(os.getenv("MAX_DAILY_LOSS", ".02")),
            stop_loss=float(os.getenv("STOP_LOSS", ".05")), take_profit=float(os.getenv("TAKE_PROFIT", ".10")),
            cost_bps=float(os.getenv("COST_BPS_PER_SIDE", "10")), edge_bps=float(os.getenv("MIN_EDGE_BPS", "10")),
            llm_url=os.getenv("FREELLM_BASE_URL", "https://ai.bastiwood.com/v1").rstrip("/"),
            llm_key=os.getenv("FREELLM_API_KEY", ""),
            llm_models=tuple(s.strip() for s in os.getenv("FREELLM_MODELS", "").split(",") if s.strip()),
            # Relative paths start at the trading-bot folder, whatever the working directory.
            loop_dir=(Path(__file__).resolve().parent.parent / os.getenv("TRADING_LOOP_DIR", "trading-loop")).resolve(),
            loop_timeout=int(os.getenv("TRADING_LOOP_TIMEOUT_SECONDS", "300")),
            trading212_enabled=flag("TRADING212_ENABLED"),
            trading212_key=os.getenv("TRADING212_API_KEY", "").strip(),
            trading212_secret=os.getenv("TRADING212_API_SECRET", "").strip(),
            trading212_paper=flag("TRADING212_PAPER", True),
            trading212_allow_live=flag("TRADING212_ALLOW_LIVE_TRADING"),
            trading212_instruments=(Path(__file__).resolve().parent.parent / os.getenv("TRADING212_INSTRUMENTS_FILE", "config/trading212-instruments.json")).resolve(),
            trading212_capital_fraction=float(os.getenv("TRADING212_CAPITAL_FRACTION", "1")),
            trading212_capital_cap=float(os.getenv("TRADING212_CAPITAL_CAP", "0")),
            trading212_allow_fx=flag("TRADING212_ALLOW_FX"),
            twelvedata_key=os.getenv("TWELVEDATA_API_KEY", "").strip(),
            reporting_currency=os.getenv("REPORTING_CURRENCY", "EUR").strip().upper(),
            origins=tuple(s.strip().rstrip("/").lower() for s in os.getenv("DASHBOARD_ORIGINS", "https://trade.bastiwood.com").split(",") if s.strip()))
