from dataclasses import replace
import json
from unittest.mock import Mock, patch
import numpy as np
import pandas as pd
from app.broker import BrokerError
from app.candles import snapshot, forecast_overlay, indicator_series
from app.daytrade import playbook
from app.store import StrategyStore
from app.web import create_app
from test_playbook import minute_bars, OPEN
from test_system import TempCase


class CandleTests(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, trading_mode='daytrade')
        self.now = OPEN + pd.Timedelta(minutes=31, seconds=10)
        self.frame = minute_bars(np.linspace(100, 101, 32))
        self.base = {'day': str(OPEN.date()), 'atr': 2, 'prev_close': 100, 'opening_volume': 100000,
                     'sigma': {str(k): .003 for k in range(5, 395, 5)}}

    def test_today_only_completed_bars_and_indicator_alignment(self):
        yesterday = self.frame.copy()
        yesterday.index -= pd.Timedelta(days=1)
        all_bars = pd.concat([yesterday, self.frame])
        data = snapshot('SPY', all_bars, self.base, self.now)
        self.assertEqual(len(data['bars']), 31) # excludes the forming 32nd minute
        self.assertEqual(data['bars'][0]['at'], OPEN.isoformat())
        self.assertTrue(all(len(s) == 31 for s in data['indicators'].values()))
        series = data['indicators']
        self.assertEqual(series['opening_high'][:4], [None]*4)
        self.assertEqual(series['opening_high'][4], float(self.frame.high.iloc[:5].max()))
        book = playbook(self.frame.iloc[:31], self.base, self.now, self.cfg)
        for line, key in [('vwap', 'vwap'), ('ema9', 'ema_fast'), ('ema20', 'ema_slow'), ('noise_upper', 'noise_upper')]:
            self.assertAlmostEqual(series[line][-1], book['indicators'][key])
        self.assertLess(series['noise_lower'][-1], series['noise_upper'][-1])
        json.dumps(data, allow_nan=False)

    def test_signals_deduplicate_and_reset_on_new_york_day(self):
        book = {'bar_at': self.frame.index[10].isoformat(), 'signals': [{'strategy':'orb','name':'ORB','entry':100,'reason':'fired'}]}
        one = snapshot('SPY', self.frame, self.base, self.now, book=book)
        two = snapshot('SPY', self.frame, self.base, self.now, previous=one, book=book)
        self.assertEqual(len(two['signals']), 1)
        tomorrow = snapshot('SPY', None, None, self.now+pd.Timedelta(days=1), previous=two)
        self.assertEqual(tomorrow['signals'], [])
        self.assertEqual(tomorrow['bars'], [])

    def test_forecast_has_true_horizon_and_rejects_stale_future_and_halfday_crossing(self):
        now = OPEN + pd.Timedelta(minutes=30, seconds=10)
        pred = {'trading_mode':'daytrade', 'bar_at': (OPEN+pd.Timedelta(minutes=25)).isoformat(),
                'last_close':100, 'expected_return':.01, 'eligible':True, 'models':[]}
        forecast = forecast_overlay(pred, now)
        self.assertEqual(pd.Timestamp(forecast['to'])-pd.Timestamp(forecast['from']), pd.Timedelta(minutes=15))
        self.assertEqual(forecast['target_price'], 101)
        self.assertIsNone(forecast_overlay(pred, now+pd.Timedelta(minutes=5)))
        self.assertIsNone(forecast_overlay(pred, now-pd.Timedelta(minutes=5)))
        self.assertIsNone(forecast_overlay(pred, now, OPEN+pd.Timedelta(minutes=35)))

    def test_endpoint_uses_cached_data_and_requires_supported_monitored_symbol(self):
        data = snapshot('SPY', self.frame, self.base, self.now)
        self.store.set('candles:SPY', data)
        client = create_app(self.cfg, self.store).test_client()
        with patch('app.web.pd.Timestamp.now', return_value=self.now):
            response = client.get('/api/candles/SPY')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['bars']), 31)
        self.assertIsNone(response.json['forecast'])
        self.assertEqual(client.get('/api/candles/QQQ').status_code, 404)
        self.assertEqual(client.get('/api/candles/SPY%20bad').status_code, 400)

    def test_endpoint_fetches_cold_session_and_filters_after_halfday(self):
        now = pd.Timestamp('2026-11-27T19:00:00Z')
        frame = self.frame.copy()
        frame.index = pd.date_range('2026-11-27T17:45:00Z', periods=len(frame), freq='min')
        broker = Mock()
        broker.calendar.return_value = [{'date':'2026-11-27','open':'09:30','close':'13:00'}]
        broker.multi_bars.return_value = {'SPY': frame[frame.index<pd.Timestamp('2026-11-27T18:00:00Z')]}
        client = create_app(self.cfg, self.store, broker_factory=lambda cfg:broker).test_client()
        with patch('app.web.pd.Timestamp.now', return_value=now):
            response = client.get('/api/candles/SPY')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['bars']), 15)
        self.assertEqual(broker.multi_bars.call_args.kwargs['end'], pd.Timestamp('2026-11-27T18:00:00Z'))

    def test_endpoint_includes_only_current_matching_model_forecasts(self):
        from app.models import strategy_signature
        now = OPEN + pd.Timedelta(minutes=30, seconds=10)
        pred = {'symbol':'SPY', 'trading_mode':'daytrade', 'model_version':'one',
                'bar_at':(OPEN+pd.Timedelta(minutes=25)).isoformat(),
                'last_close':100, 'expected_return':.01, 'eligible':True, 'models':[]}
        self.store.set('candles:SPY',snapshot('SPY',self.frame,self.base,now))
        self.store.set('prediction:SPY',pred)
        meta = {'version':'one','strategy_signature':strategy_signature(self.cfg),
                'data_through':pred['bar_at'],'created_at':(now-pd.Timedelta(hours=1)).isoformat()}
        self.store.set('model:SPY',meta)
        client=create_app(self.cfg,self.store).test_client()
        with patch('app.web.pd.Timestamp.now',return_value=now):
            data=client.get('/api/candles/SPY').json
            self.assertEqual(data['forecast']['target_price'],101)
            self.store.set('model:SPY',{**meta,'version':'two'})
            self.assertIsNone(client.get('/api/candles/SPY').json['forecast'])

    def test_authentication_daily_mode_and_provider_errors(self):
        secured = create_app(replace(self.cfg, password='secret'), self.store).test_client()
        self.assertEqual(secured.get('/api/candles/SPY').status_code, 401)
        daily = create_app(replace(self.cfg, trading_mode='daily'), self.store).test_client()
        self.assertEqual(daily.get('/api/candles/SPY').status_code, 409)
        broker = Mock()
        broker.calendar.side_effect = BrokerError('failed')
        client = create_app(self.cfg, self.store, broker_factory=lambda cfg:broker).test_client()
        self.assertEqual(client.get('/api/candles/SPY').status_code, 502)
