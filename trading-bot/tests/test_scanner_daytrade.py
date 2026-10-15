from dataclasses import replace
from datetime import datetime, timezone
import json
import time
from unittest.mock import Mock, patch
import unittest
import numpy as np
import pandas as pd

from app.broker import Alpaca, BrokerError
from app.config import Config
from app.engine import Engine
from app.execution import Executor
from app.features import dataset, features, FEATURES
from app.feedback import capture_forecast, resolve_outcomes, replay_dataset, summary_key
from app.models import train_symbol, predict, strategy_signature, evaluate
from app.research import combine
from app.store import Store, utcnow
from app.universe import scan, screen, active_symbols, monitored_symbols, entries_ready
from app.web import create_app
from test_system import TempCase


def intraday_bars(days=16):
    idx = pd.DatetimeIndex([stamp for day in pd.bdate_range('2026-09-01', periods=days)
                           for stamp in pd.date_range(f'{day.date()} 09:30', periods=78, freq='5min', tz='America/New_York')]).tz_convert('UTC')
    close = 100 * np.exp(np.arange(len(idx)) * .0004 + np.sin(np.arange(len(idx))) * .0001)
    return pd.DataFrame({'open': close * .9999, 'high': close * 1.001, 'low': close * .999,
                         'close': close, 'volume': 10000 + np.arange(len(idx))}, index=idx)


class UniverseTests(TempCase):
    def snapshot(self, price=100, volume=100000):
        return {'latestTrade': {'p': price}, 'prevDailyBar': {'c': 100, 'v': volume,
                't': (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=1)).isoformat()},
                'dailyBar': {'c': price, 'v': volume}}

    def test_liquidity_price_stale_and_nonfinite_filters(self):
        now = pd.Timestamp.now(tz='UTC')
        self.assertIsNotNone(screen(self.cfg, 'ABC', self.snapshot(), now))
        for item in (self.snapshot(price=1), self.snapshot(volume=1), self.snapshot(price=float('nan')),
                     {'prevDailyBar': {'c': 100, 'v': 100000, 't': '2000-01-01T00:00:00Z'}}, {}):
            self.assertIsNone(screen(self.cfg, 'ABC', item, now))

    def test_complete_universe_is_batched_and_shortlisted_without_otc(self):
        cfg = replace(self.cfg, universe_mode='market', scan_candidates=4)
        broker = Mock()
        assets = [{'symbol': f'S{i}', 'class': 'us_equity', 'status': 'active', 'tradable': True, 'exchange': 'NASDAQ'} for i in range(205)]
        broker.assets.return_value = assets + [{**assets[0], 'symbol': 'OTC', 'exchange': 'OTC'}, {**assets[0], 'symbol': 'NO', 'tradable': False}]
        broker.snapshots.side_effect = lambda batch: {s: self.snapshot(price=100+int(s[1:])/100) for s in batch}
        stop = Mock()
        stop.is_set.return_value = stop.wait.return_value = False
        scan(cfg, self.store, broker, stop)
        state = self.store.get('universe')
        self.assertEqual([len(c.args[0]) for c in broker.snapshots.call_args_list], [100, 100, 5])
        self.assertEqual(state['total_assets'], 205)
        self.assertEqual(state['covered'], 205)
        self.assertEqual(len(active_symbols(cfg, self.store)), 4)
        self.assertTrue(entries_ready(cfg, self.store))
        self.store.set('owned:OLD', True)
        watched = monitored_symbols(cfg, self.store, [{'symbol': 'OLD'}, {'symbol': 'MANUAL'}])
        self.assertIn('OLD', watched)
        self.assertNotIn('MANUAL', watched)

    def test_partial_scan_cannot_replace_selection_or_allow_entries(self):
        cfg = replace(self.cfg, universe_mode='market')
        old = {'symbols': ['OLD'], 'scanned_at': time.time(), 'feed': 'iex'}
        self.store.set('universe', old)
        broker, stop = Mock(), Mock()
        broker.assets.return_value = [{'symbol': 'NEW', 'class': 'us_equity', 'status': 'active', 'tradable': True, 'exchange': 'NYSE'}]
        broker.snapshots.side_effect = BrokerError('Rate limit', 429)
        stop.is_set.return_value = stop.wait.return_value = False
        scan(cfg, self.store, broker, stop)
        self.assertEqual(self.store.get('universe'), old)
        self.assertFalse(entries_ready(cfg, self.store))
        self.store.set('scan_status', {'status': 'ready'})
        self.store.set('universe', {**old, 'scanned_at': time.time()-100000})
        self.assertFalse(entries_ready(cfg, self.store))

    def test_dashboard_handles_empty_market_universe(self):
        cfg = replace(self.cfg, universe_mode='market')
        state = create_app(cfg, self.store).test_client().get('/api/state').get_json()
        self.assertEqual(state['symbols'], [])
        self.assertEqual(state['settings']['universe_mode'], 'market')


class IntradayModelTests(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, trading_mode='daytrade')

    def test_intraday_labels_are_contiguous_and_never_cross_sessions(self):
        bars = intraday_bars()
        frame = dataset(bars, intraday=True)
        at = bars.index[100]
        self.assertAlmostEqual(frame.loc[at, 'target'], bars.close.iloc[103] / bars.close.iloc[100] - 1)
        self.assertEqual(frame.loc[at, 'label_end'], bars.index[103]+pd.Timedelta(minutes=5))
        self.assertNotIn(bars.index[77], frame.index)
        self.assertNotIn(at, dataset(bars.drop(bars.index[102]), intraday=True).index)
        changed = bars.copy()
        changed.iloc[200:, :4] *= 2
        pd.testing.assert_frame_equal(features(bars).loc[:bars.index[199]], features(changed).loc[:bars.index[199]])

    def test_intraday_replay_waits_for_outcome_and_has_separate_schema(self):
        bars = intraday_bars()
        at = bars.index[100]
        forecast = {'symbol': 'SPY', 'at': (at+pd.Timedelta(minutes=5, seconds=15)).isoformat(),
                    'bar_at': at.isoformat(), 'model_version': 'intraday-1', 'features': features(bars).loc[at].to_dict(),
                    'expected_return': .01, 'models': [{'id': 'ridge', 'expected_return': .01}]}
        self.assertTrue(capture_forecast(self.cfg, self.store, forecast))
        self.assertEqual(resolve_outcomes(self.cfg, self.store, 'SPY', bars.iloc[:103], now=at+pd.Timedelta(minutes=15)), 0)
        self.assertEqual(resolve_outcomes(self.cfg, self.store, 'SPY', bars, now=bars.index[-1]+pd.Timedelta(minutes=5)), 1)
        self.assertEqual(self.store.experience('SPY', 'iex', 1), [])
        replay, _ = replay_dataset(self.cfg, self.store, 'SPY', bars)
        self.assertEqual(replay.loc[at, 'sample_weight'], 2)
        self.assertAlmostEqual(replay.loc[at, 'target'], bars.close.iloc[103]/bars.close.iloc[100]-1)
        self.assertEqual(self.store.get(summary_key(self.cfg, 'SPY'))['resolved'], 1)

    def test_real_intraday_training_uses_purged_holdout_and_distinct_signature(self):
        bars = intraday_bars()
        train_symbol(self.cfg, self.store, 'SPY', bars)
        meta = self.store.get('model:SPY')
        self.assertLess(meta['trained_through'], meta['metrics']['start'])
        self.assertLessEqual(meta['metrics']['observations'], 132)
        forecast = predict(self.cfg, meta, bars)
        self.assertEqual(forecast['trading_mode'], 'daytrade')
        self.assertIn('15 minutes', forecast['horizon'])
        self.assertNotEqual(strategy_signature(self.cfg), strategy_signature(replace(self.cfg, trading_mode='daily')))

    def test_bars_exclude_unfinished_extended_hours_and_half_day_after_close(self):
        broker = Alpaca(self.cfg)
        stamps = ['2026-11-27T14:25:00Z', '2026-11-27T14:30:00Z', '2026-11-27T17:55:00Z',
                  '2026-11-27T18:00:00Z', '2026-11-30T14:30:00Z', '2026-11-30T14:35:00Z']
        broker.request = Mock(return_value={'bars': [{'t': s, 'o': 100, 'h': 101, 'l': 99, 'c': 100, 'v': 1000} for s in stamps]})
        broker.calendar = Mock(return_value=[{'date': '2026-11-27', 'open': '09:30', 'close': '13:00'},
                                             {'date': '2026-11-30', 'open': '09:30', 'close': '16:00'}])
        bars = broker.bars('SPY', datetime(2026, 11, 30, 14, 37, tzinfo=timezone.utc))
        self.assertEqual(list(bars.index), [pd.Timestamp(stamps[i]) for i in (1, 2, 4)])
        self.assertEqual(broker.request.call_args.kwargs['params']['timeframe'], '5Min')

    def test_quote_freshness_and_spread(self):
        broker = Alpaca(self.cfg)
        for quote in ({'bp': 99, 'ap': 101, 't': utcnow()}, {'bp': 100, 'ap': 100.1, 't': '2020-01-01T00:00:00Z'},
                      {'bp': 100.2, 'ap': 100.1, 't': utcnow()}):
            broker.request = Mock(return_value={'quote': quote})
            with self.assertRaises(BrokerError): broker.quote('SPY')
        broker.request = Mock(return_value={'quote': {'bp': 100, 'ap': 100.1, 't': utcnow()}})
        self.assertEqual(broker.quote('SPY'), (100, 100.1))

    def test_daytrade_is_paper_only_and_cannot_reuse_daily_journal(self):
        with self.assertRaises(ValueError): replace(self.cfg, paper=False, allow_live=True)
        Engine(replace(self.cfg, trading_mode='daily'), self.store)
        with self.assertRaisesRegex(ValueError, 'separate DATA_DIR'): Engine(self.cfg, self.store)

    def test_intraday_news_has_short_expiry_without_requiring_same_bar(self):
        forecast = {'trading_mode': 'daytrade', 'eligible': True, 'forecast_signal': 'BUY', 'bar_at': 'new'}
        research = {'trading_mode': 'daytrade', 'status': 'ready', 'action': 'BUY', 'bar_at': 'old', 'at': utcnow()}
        self.assertEqual(combine(forecast, research)[0], 'BUY')
        self.assertEqual(combine(forecast, {**research, 'trading_mode': 'daily'})[0], 'HOLD')
        self.assertEqual(combine(forecast, {**research, 'at': (pd.Timestamp.now(tz='UTC')-pd.Timedelta(minutes=11)).isoformat()})[0], 'HOLD')


class DaytradeExecutionTests(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, trading_mode='daytrade', daytrade_max_entries=2)
        self.broker = Mock(base='paper')
        self.broker.asset.return_value = {'tradable': True, 'status': 'active'}
        self.broker.quote.return_value = (99.99, 100)
        self.broker.submit.return_value = {'id': 'remote', 'status': 'filled', 'filled_qty': '9'}
        self.executor = Executor(self.cfg, self.store, self.broker)
        self.account = {'status': 'ACTIVE', 'equity': '10000', 'cash': '10000', 'buying_power': '10000'}
        self.store.set('entries_enabled', True)

    def buy(self, symbol='SPY', positions=(), account=None):
        return self.executor.execute(symbol, 'BUY', '2026-10-15', account or self.account, positions, [])

    def test_multiple_symbols_reentry_unique_ids_and_daily_cap(self):
        self.buy()
        position = [{'symbol': 'SPY', 'qty': '9', 'market_value': '900'}]
        self.assertIn('already exists', self.buy(positions=position))
        self.buy('QQQ', positions=position)
        self.executor.execute('SPY', 'SELL', '2026-10-15', self.account, position, [])
        self.buy()
        self.executor.execute('SPY', 'SELL', '2026-10-15', self.account, position, [])
        self.assertIn('entry limit', self.buy())
        ids = [c.args[0]['client_order_id'] for c in self.broker.submit.call_args_list]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)
        self.assertTrue(all(len(cid) <= 48 for cid in ids))

    def test_cooldown_and_position_cap_and_broker_capacity(self):
        self.store.set('flat_since:SPY', time.time())
        self.assertIn('cooldown', self.buy())
        self.assertIn('position limit', self.buy('QQQ', [{'symbol': str(i), 'qty': '1'} for i in range(3)]))
        self.buy('QQQ', account={**self.account, 'daytrading_buying_power': '0'})
        self.broker.submit.assert_not_called()

    def test_unknown_entry_blocks_other_symbols_and_survives_restart(self):
        self.broker.submit.side_effect = BrokerError('timeout')
        self.buy()
        self.executor = Executor(self.cfg, Store(self.cfg.data_dir), self.broker)
        self.assertIn('unresolved', self.buy('QQQ'))
        self.broker.submit.assert_called_once()


class DaytradeEngineTests(TempCase):
    def engine(self, mode='daytrade', now='2026-11-27T17:51:00Z', close='2026-11-27T18:00:00Z'):
        cfg = replace(self.cfg, trading_mode=mode)
        engine = Engine(cfg, self.store)
        engine.scan_tick = engine.forecast_tick = engine.research_tick = Mock()
        engine.broker = Mock()
        engine.broker.account.return_value = {'equity': '10000', 'last_equity': '10000', 'cash': '10000', 'buying_power': '10000'}
        engine.broker.clock.return_value = {'timestamp': now, 'next_close': close, 'is_open': True}
        engine.broker.open_orders.return_value = []
        engine.broker.positions.return_value = []
        engine.executor = Mock(changed=True)
        engine.executor.reconcile.return_value = True
        engine.executor.execute.return_value = 'exit submitted'
        return engine

    def test_half_day_flatten_and_rotated_out_holdings_without_models(self):
        engine = self.engine()
        engine.broker.positions.return_value = [{'symbol': 'OLD', 'qty': '5', 'unrealized_plpc': '.001'}]
        self.store.set('owned:OLD', True)
        self.store.set('daytrade_position:OLD', {'started_at': '2026-11-27T17:50:00Z'})
        engine.cycle()
        self.assertEqual(engine.executor.execute.call_args.args[:2], ('OLD', 'SELL'))
        self.assertTrue(engine.executor.execute.call_args.kwargs['force_exit'])

    def test_max_hold_exit_while_paused_and_missing_news(self):
        engine = self.engine(now='2026-11-27T15:00:00Z')
        engine.broker.positions.return_value = [{'symbol': 'SPY', 'qty': '5', 'unrealized_plpc': '.001'}]
        self.store.set('owned:SPY', True)
        self.store.set('daytrade_position:SPY', {'started_at': '2026-11-27T14:40:00Z'})
        engine.cycle()
        self.assertEqual(engine.executor.execute.call_args.args[1], 'SELL')

    def test_stale_entry_canceled_and_no_new_action_on_same_snapshot(self):
        engine = self.engine(now='2026-11-27T15:00:00Z')
        self.store.set('entries_enabled', True)
        engine.broker.open_orders.return_value = [{'symbol': 'SPY', 'side': 'buy', 'id': 'remote',
            'client_order_id': 'ensemble-test', 'submitted_at': '2026-11-27T14:58:00Z'}]
        engine.cycle()
        engine.broker.cancel.assert_called_once_with('remote')
        engine.executor.execute.assert_not_called()

    def test_stop_exits_prioritized_over_higher_ranked_entries(self):
        engine = self.engine(mode='daily', now='2026-11-27T14:40:00Z')
        engine.broker.positions.return_value = [{'symbol': 'OLD', 'qty': '5', 'unrealized_plpc': '-.08'}]
        self.store.set('owned:OLD', True)
        engine.cycle()
        self.assertEqual(engine.executor.execute.call_args.args[:2], ('OLD', 'SELL'))

    def test_ranked_entry_that_cannot_execute_does_not_starve_other_stocks(self):
        engine = self.engine(mode='daily', now='2026-11-27T14:40:00Z')
        engine.config = replace(engine.config, symbols=('AAA', 'BBB', 'CCC'))
        for symbol, expected in [('CCC', .01), ('AAA', .03), ('BBB', .02)]:
            self.store.set(f'model:{symbol}', {'version': 'v1', 'data_through': '2026-11-25T05:00:00Z',
                                             'strategy_signature': strategy_signature(engine.config)})
            self.store.set(f'prediction:{symbol}', {'symbol': symbol, 'bar_at': '2026-11-25T05:00:00Z',
                'model_version': 'v1', 'eligible': True, 'expected_return': expected, 'forecast_signal': 'BUY'})
            self.store.set(f'research:{symbol}', {'status': 'ready', 'action': 'BUY', 'at': utcnow(), 'bar_at': '2026-11-25T05:00:00Z'})
        def execute(symbol, *args, **kwargs):
            engine.executor.changed = symbol == 'BBB'
            return 'Position already exists' if symbol == 'AAA' else 'entry submitted'
        engine.executor.execute.side_effect = execute
        engine.cycle()
        self.assertEqual([c.args[0] for c in engine.executor.execute.call_args_list], ['AAA', 'BBB'])

    def test_stale_intraday_prediction_cannot_buy(self):
        engine = self.engine(now='2026-11-27T15:00:00Z')
        self.store.set('model:SPY', {'version': 'v1', 'data_through': '2026-11-27T14:30:00Z',
                                   'strategy_signature': strategy_signature(engine.config)})
        self.store.set('prediction:SPY', {'symbol': 'SPY', 'bar_at': '2026-11-27T14:45:00Z',
            'model_version': 'v1', 'eligible': True, 'expected_return': .05, 'forecast_signal': 'BUY', 'trading_mode': 'daytrade'})
        self.store.set('research:SPY', {'status': 'ready', 'action': 'BUY', 'at': utcnow(), 'trading_mode': 'daytrade'})
        engine.cycle()
        engine.executor.execute.assert_not_called()
        self.assertEqual(self.store.get('forecast:SPY')['action'], 'WAIT')

    def test_forecast_request_failure_retries_without_waiting_for_next_day(self):
        engine = self.engine(mode='daily')
        # Use the real asynchronous forecasting method while keeping all I/O mocked.
        engine.forecast_tick = Engine.forecast_tick.__get__(engine)
        self.store.set('model:SPY', {'version': 'v1', 'strategy_signature': strategy_signature(engine.config)})
        broker = Mock()
        broker.bars.side_effect = BrokerError('temporary outage')
        with patch('app.engine.Alpaca', return_value=broker), patch('app.engine.time.time', side_effect=[1000, 1000, 1061, 1061]):
            engine.forecast_tick(['SPY'], '2026-11-27')
            engine.forecast_thread.join(timeout=5)
            engine.forecast_tick(['SPY'], '2026-11-27')
            engine.forecast_thread.join(timeout=5)
        self.assertEqual(broker.bars.call_count, 2)


if __name__ == '__main__':
    unittest.main()
