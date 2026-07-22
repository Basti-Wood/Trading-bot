"""
shared/monitoring/controls.py — Runtime Strategy Controls (Kill Switches)

Shared control panel state between the dashboard and all bots, stored as JSON
on the shared `logs` volume. The dashboard writes it; bots read it every cycle.

Why: shorting has UNLIMITED loss potential (a stock can rise forever), while
longs can lose at most 100%. So shorting gets a master kill switch that
overrides the GA's DNA — a short only opens if BOTH the evolved DNA says
short_enabled AND the dashboard switch is on.

Also lets you enable/disable individual strategies (ML / fallback / politician)
per asset class, pause a whole bot, and control challenger auto-promotion —
so AI-evolved strategies (challengers) can be reviewed and gated by a human.
"""

import json
import os
import threading

CONTROLS_PATH = os.environ.get("CONTROLS_PATH", os.path.join(
    os.environ.get("LOGS_DIR", "logs"), "controls.json"))

_lock = threading.Lock()

ASSET_CLASSES = ["crypto", "stocks", "etf", "politician", "funds"]

DEFAULT_CONTROLS = {
    # Global master switch — False halts ALL new trades on every bot
    "global_trading": True,
    # Global shorting kill switch — overrides everything below AND all DNA
    "global_shorting": False,   # OFF by default: shorts have unlimited loss
    "per_asset": {
        asset: {
            "trading_enabled": True,        # pause this bot's new trades
            "shorting_enabled": False,      # per-asset short switch (default OFF)
            "strategies": {
                "ml": True,                 # LSTM predictions
                "fallback": True,           # RSI + trend rules
                "politician": True,         # politician signal (where applicable)
            },
            # Challenger governance: if False, circuit-breaker demotions still
            # close positions but a human must promote the challenger manually
            # from the dashboard.
            "auto_promote": True,
        }
        for asset in ASSET_CLASSES
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge saved controls over defaults so new keys appear automatically."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_controls() -> dict:
    """Read controls from the shared volume; returns defaults if missing."""
    try:
        with open(CONTROLS_PATH, "r") as f:
            saved = json.load(f)
        return _deep_merge(DEFAULT_CONTROLS, saved)
    except (FileNotFoundError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_CONTROLS))


def save_controls(controls: dict) -> None:
    """Atomic write of the full controls dict."""
    with _lock:
        os.makedirs(os.path.dirname(CONTROLS_PATH) or ".", exist_ok=True)
        tmp = CONTROLS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(controls, f, indent=2)
        os.replace(tmp, CONTROLS_PATH)


def set_control(path: list, value) -> dict:
    """
    Update one control by key path, e.g.
    set_control(["per_asset", "crypto", "shorting_enabled"], True).
    Returns the updated controls dict.
    """
    controls = load_controls()
    node = controls
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    save_controls(controls)
    return controls


# ---------------------------------------------------------------------------
# Convenience checks used by bots each cycle
# ---------------------------------------------------------------------------

def trading_allowed(asset_class: str, controls: dict = None) -> bool:
    c = controls or load_controls()
    return bool(c["global_trading"]
                and c["per_asset"].get(asset_class, {}).get("trading_enabled", True))


def shorting_allowed(asset_class: str, controls: dict = None) -> bool:
    """
    True only if the GLOBAL short switch AND the per-asset short switch are on.
    The caller must additionally check dna.short_enabled — the dashboard can
    only veto shorting, never force it on against the GA's judgement.
    Funds never short regardless of any switch.
    """
    if asset_class == "funds":
        return False
    c = controls or load_controls()
    return bool(c["global_shorting"]
                and c["per_asset"].get(asset_class, {}).get("shorting_enabled", False))


def strategy_enabled(asset_class: str, strategy: str, controls: dict = None) -> bool:
    c = controls or load_controls()
    return bool(c["per_asset"].get(asset_class, {})
                .get("strategies", {}).get(strategy, True))


def auto_promote_allowed(asset_class: str, controls: dict = None) -> bool:
    c = controls or load_controls()
    return bool(c["per_asset"].get(asset_class, {}).get("auto_promote", True))
