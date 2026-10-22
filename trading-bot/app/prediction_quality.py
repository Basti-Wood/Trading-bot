"""Per-stock forecast freshness, inspired by FreqAI's explicit expiry status.

This is a local implementation, with no dependency on the reference project.
"""
import pandas as pd
from .models import strategy_signature


def forecast_quality(config, meta, forecast, now):
    def result(status, reason=None, age=None):
        return {'status': status, 'ready': status == 'ready', 'reason': reason, 'model_age_hours': age}

    if (not meta or meta.get('strategy_signature') != strategy_signature(config)
            or forecast.get('model_version') != meta.get('version')):
        return result('settings_changed', 'Settings or model changed; awaiting a current forecast')
    try:
        now = pd.Timestamp(now)
        trained = pd.Timestamp(meta.get('created_at') or meta['data_through'])
        bar = pd.Timestamp(forecast['bar_at'])
        data = pd.Timestamp(meta['data_through'])
        if any(t.tzinfo is None for t in (now, trained, bar, data)):
            raise ValueError('timestamps must include a timezone')
        age_hours = (now - trained).total_seconds() / 3600
        if not 0 <= age_hours <= config.model_max_age_hours:
            return result('model_expired', 'Model expired; awaiting retraining for this stock', age_hours)
        input_age = (now - bar).total_seconds()
        data_age = (now - data).total_seconds()
        if (not (300 <= input_age <= 390 if config.intraday else 0 <= input_age <= 7 * 86400)
                or not 0 <= data_age <= (3 if config.intraday else 14) * 86400):
            return result('stale_input', 'Market data or model is too old for this horizon', age_hours)
        return result('ready', age=age_hours)
    except (KeyError, TypeError, ValueError, OverflowError):
        return result('invalid_timestamps', 'Forecast freshness cannot be verified')
