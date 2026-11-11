"""How much one buy may spend: percentage budgets, risk per trade, test quality and market posture.

Adapted from the "position-sizer" skill (claude-trading-skills, MIT licence, TraderMonty):
- Fixed-fractional risk: a stop-out loses at most `risk_per_trade` of the bot's budget.
- Half Kelly from the strategy's own test statistics, used only as an upper limit.
- Portfolio heat: the summed stop risk of a bot's open positions stays below `heat`.
- Strictest limit wins, and sizes are floored, never rounded up.
On top: the position cap scales with the stress test's quality score (trading-loop/qualitaet.py)
and the bot's total investment is capped by the market posture (app/regime.py).

All percentages are of the bot's budget (bot budget = account × bot share × bot weight), and the
dashboard can change them without a restart. Defaults come from .env.
"""
import math

# Hard ceilings, whatever the settings say.
MAX_POSITION = .25
QUALITY_RANGE = (.5, 1.25)
BOUNDS = {"position_fraction": (.01, .20), "gross_fraction": (.05, 1.), "risk_per_trade": (.001, .05),
          "heat": (.01, .30)}
LABELS = {"position_fraction": "Einsatz pro Aktie", "gross_fraction": "Höchstens investiert",
          "risk_per_trade": "Risiko pro Trade", "heat": "Offenes Risiko (Portfolio-Hitze)"}


def defaults(config):
    return {"position_fraction": config.position_fraction, "gross_fraction": config.gross_fraction,
            "risk_per_trade": config.risk_per_trade, "heat": config.portfolio_heat, "quality_scaling": True}


def settings(config, store):
    """Current sizing settings: dashboard values over .env defaults."""
    root = getattr(store, "root", store)
    saved = ((root.get("allocation") or {}).get("sizing")) or {}
    result = defaults(config)
    for key, (lo, hi) in BOUNDS.items():
        value = saved.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value) and lo <= value <= hi:
            result[key] = float(value)
    if isinstance(saved.get("quality_scaling"), bool):
        result["quality_scaling"] = saved["quality_scaling"]
    result["position_fraction"] = min(result["position_fraction"], result["gross_fraction"])
    return result


def validate(body):
    """Dashboard input (percent values) → stored fractions; raises ValueError with a German message."""
    if not body:
        return None
    try:
        cleaned = {key: float(body[key]) / 100 for key in BOUNDS}
    except (KeyError, TypeError, ValueError):
        raise ValueError("Positionsgrößen als Prozentzahlen angeben")
    for key, (lo, hi) in BOUNDS.items():
        if not math.isfinite(cleaned[key]) or not lo <= cleaned[key] <= hi:
            raise ValueError(f"{LABELS[key]} muss zwischen {lo * 100:g} % und {hi * 100:g} % liegen")
    if cleaned["position_fraction"] > cleaned["gross_fraction"]:
        raise ValueError("Einsatz pro Aktie darf nicht größer sein als 'Höchstens investiert'")
    cleaned["quality_scaling"] = bool(body.get("quality_scaling", True))
    return cleaned


def edge(test):
    """(quality factor, half Kelly or None, note) from a trading-loop test; neutral without statistics."""
    test = test or {}
    stats = test.get("statistik") or {}
    quality = (test.get("qualitaet") or {}).get("punkte")
    factor = 1.
    notes = []
    if isinstance(quality, (int, float)):
        lo, hi = QUALITY_RANGE
        factor = min(hi, max(lo, .5 + quality / 100))
        notes.append(f"Qualität {quality:.0f}/100 → {factor:.2f}×")
    kelly = None
    win, gain, loss = stats.get("gewinnquote"), stats.get("avg_gewinn"), stats.get("avg_verlust")
    if all(isinstance(v, (int, float)) for v in (win, gain, loss)) and (stats.get("anzahl") or 0) >= 10:
        p = win / 100
        if loss > 0 and gain > 0:
            kelly = (p - (1 - p) / (gain / loss)) / 2
        elif loss <= 0 < gain:
            kelly = None  # No losing trade in the test: Kelly says "everything", which is no information.
        else:
            kelly = -1.
        if kelly is not None:
            notes.append(f"halbes Kelly {kelly:.1%}")
    return factor, kelly, "; ".join(notes)


def budget(*, capital, share, price, stop, gross_all, gross_bot, open_risk_bot, cash, settings,
           test=None, ceiling=1.):
    """Cash this buy may spend and which limit decided it.

    capital: all bots together; share: this bot's fraction of it; stop: stop distance as a fraction of
    the price (None = no stop known); ceiling: market-posture share of `gross_fraction` (0..1).
    Returns {"amount", "binding", "limits": {label: amount}, "note"}.
    """
    bot = max(0., capital) * max(0., share)
    factor, kelly, note = edge(test) if settings.get("quality_scaling", True) else (1., None, "")
    limits = {"Einsatz pro Aktie": bot * min(MAX_POSITION, settings["position_fraction"] * factor)}
    if stop and stop > 0:
        limits["Risiko pro Trade"] = bot * settings["risk_per_trade"] / stop
        limits["Offenes Risiko (Portfolio-Hitze)"] = (bot * settings["heat"] - open_risk_bot) / stop
    if kelly is not None:
        # Kelly gives the share of capital to *risk* (as in position-sizer); the position is risk ÷ stop distance.
        limits["Halbes Kelly aus dem Test"] = bot * max(0., kelly) / (stop if stop and stop > 0 else 1.)
    ceiling = min(1., max(0., ceiling))
    limits["Höchstens investiert (dieser Bot)"] = bot * settings["gross_fraction"] * ceiling - gross_bot
    limits["Höchstens investiert (alle Bots)"] = capital * settings["gross_fraction"] * ceiling - gross_all
    limits["Freies Bargeld"] = cash
    binding = min(limits, key=limits.get)
    amount = max(0., limits[binding])
    return {"amount": amount, "binding": binding, "limits": {k: round(v, 2) for k, v in limits.items()},
            "note": note, "bot_budget": round(bot, 2), "ceiling": ceiling}


def open_risk(positions, stop):
    """Approximate stop risk of open positions: market value × stop distance."""
    return sum(abs(float(p.get("market_value") or 0)) for p in positions) * (stop or 0)
