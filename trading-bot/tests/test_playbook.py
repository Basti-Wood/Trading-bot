from dataclasses import replace
from unittest.mock import Mock, patch
import unittest

import numpy as np
import pandas as pd

from app.config import Config
from app.daytrade import baseline, bracket, exit_signal, fresh, playbook
from app.engine import Engine
from test_decisions import DecisionFixture
from test_system import TempCase, book, models

OPEN = pd.Timestamp('2026-10-15T13:30:00Z')   # 09:30 New York


def minute_bars(closes, opens=None, highs=None, lows=None, volume=1000.):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]] if opens is None else np.asarray(opens, dtype=float)
    index = pd.date_range(OPEN, periods=len(closes), freq='1min')
    frame = pd.DataFrame({'open': opens, 'close': closes,
                          'high': np.maximum(opens, closes) + .02 if highs is None else highs,
                          'low': np.minimum(opens, closes) - .02 if lows is None else lows,
                          'volume': volume}, index=index)
    return frame


ALL = Config(daytrade_strategies=('orb', 'noise_breakout', 'vwap_pullback', 'vwap_reversion'))


def run(frame, base=None, cfg=ALL):
    now = frame.index[-1] + pd.Timedelta(minutes=1, seconds=5)
    return playbook(frame, base, now, cfg)


def fired(result):
    return {s['strategy'] for s in result['signals']}


class StrategyTests(unittest.TestCase):
    def test_opening_range_breakout_for_stock_in_play(self):
        closes = [100.2, 100.4, 100.6, 100.8, 101.0] + [100.9] * 10 + [101.1]
        frame = minute_bars(closes, volume=400.)
        base = {'atr': 3., 'prev_close': 100., 'opening_volume': 1000., 'sigma': {}}
        result = run(frame, base)
        orb = next(s for s in result['signals'] if s['strategy'] == 'orb')
        self.assertAlmostEqual(orb['stop'], 101.1 - .3)
        self.assertEqual(orb['exit'], 'eod')
        # Normal opening volume, a red range or a late chase are not stocks in play.
        self.assertNotIn('orb', fired(run(frame, {**base, 'opening_volume': 5000.})))
        self.assertNotIn('orb', fired(run(minute_bars(closes[:5][::-1] + closes[5:], volume=400.), base)))
        self.assertNotIn('orb', fired(run(minute_bars(closes[:-1] + [101.5], volume=400.), base)))

    def test_noise_boundary_momentum(self):
        closes = np.linspace(100, 101, 30)
        base = {'atr': 2., 'prev_close': 100., 'opening_volume': 1e9, 'sigma': {'30': .005, '25': .005}}
        result = run(minute_bars(closes), base)
        noise = next(s for s in result['signals'] if s['strategy'] == 'noise_breakout')
        self.assertEqual(noise['exit'], 'noise_trail')
        self.assertAlmostEqual(result['indicators']['noise_upper'], 100.5)
        self.assertNotIn('noise_breakout', fired(run(minute_bars(closes), {**base, 'sigma': {'30': .02}})))
        # Only half-hour marks act, as in the paper.
        self.assertNotIn('noise_breakout', fired(run(minute_bars(np.r_[closes, 101.05]), {**base, 'sigma': {'30': .005}})))

    def test_disabled_strategies_never_fire(self):
        closes = np.linspace(100, 101, 30)
        base = {'atr': 2., 'prev_close': 100., 'opening_volume': 1e9, 'sigma': {'30': .005}}
        only_orb = Config(daytrade_strategies=('orb',))
        self.assertEqual(run(minute_bars(closes), base, only_orb)['signals'], [])
        self.assertEqual(Config().daytrade_strategies, ('orb', 'noise_breakout'))
        with self.assertRaises(ValueError):
            Config(daytrade_strategies=('martingale',))

    def test_vwap_trend_pullback(self):
        closes = list(np.linspace(100, 103, 30)) + [102.9, 102.85, 102.8, 103.3]
        opens = [closes[0]] + closes[:-1]
        opens[-1] = 102.8
        lows = [min(o, c) - .02 for o, c in zip(opens, closes)]
        for i in (-4, -3, -2):
            lows[i] = 101.  # wicks back into VWAP
        highs = [max(o, c) + .02 for o, c in zip(opens, closes)]
        result = run(minute_bars(closes, opens, highs, lows))
        pullback = next(s for s in result['signals'] if s['strategy'] == 'vwap_pullback')
        self.assertAlmostEqual(pullback['target'] - pullback['entry'], 2 * (pullback['entry'] - pullback['stop']))
        lows[-4:-1] = [102.7] * 3  # no touch, no pullback entry
        self.assertNotIn('vwap_pullback', fired(run(minute_bars(closes, opens, highs, lows))))

    def test_vwap_band_mean_reversion(self):
        closes = list(100 + np.sin(np.arange(40)) * .05) + [99.5, 99., 98.5, 98., 98.3]
        result = run(minute_bars(closes))
        reversion = next(s for s in result['signals'] if s['strategy'] == 'vwap_reversion')
        self.assertEqual(reversion['exit'], 'vwap_target')
        self.assertAlmostEqual(reversion['target'], result['indicators']['vwap'])
        self.assertNotIn('vwap_reversion', fired(run(minute_bars(closes[:-1] + [97.9]))))

    def test_quiet_market_fires_nothing_and_short_sessions_wait(self):
        self.assertEqual(run(minute_bars(100 + np.sin(np.arange(60)) * .03))['signals'], [])
        self.assertIn('five one-minute bars', run(minute_bars([100.] * 3))['note'])

    def test_baseline_from_prior_sessions_only(self):
        days = pd.bdate_range('2026-09-14', '2026-10-15', tz='UTC')
        daily = pd.DataFrame({'open': 100., 'high': 101., 'low': 99., 'close': 100., 'volume': 1e6},
                             index=days + pd.Timedelta(hours=4))
        five = []
        for day in days[-15:]:
            index = pd.date_range(day + pd.Timedelta(hours=13, minutes=30), periods=78, freq='5min')
            five.append(pd.DataFrame({'open': 100., 'high': 100.5, 'low': 99.5, 'close': np.linspace(100, 101, 78),
                                      'volume': 500. if day.date() < days[-1].date() else 1e9}, index=index))
        result = baseline(daily, pd.concat(five), days[-1].date())
        self.assertAlmostEqual(result['atr'], 2.)
        self.assertEqual(result['opening_volume'], 500.)   # today's bars excluded
        self.assertAlmostEqual(result['sigma']['5'], 0.)
        self.assertAlmostEqual(result['sigma']['390'], .01)
        with self.assertRaises(ValueError):
            baseline(daily.tail(5), pd.concat(five), days[-1].date())

    def test_bracket_and_exit_rules(self):
        cfg = Config()
        self.assertEqual(bracket(cfg, {'entry': 100., 'stop': 99.5, 'target': 101.}), (.005, .01))
        self.assertEqual(bracket(cfg, {'entry': 100., 'stop': 90., 'target': None}), (cfg.daytrade_stop_loss, .08))
        plan = {'name': 'Setup', 'stop': 99., 'target': 102., 'exit': 'target'}
        self.assertIn('stop', exit_signal(plan, 98.9, {}))
        self.assertIn('target', exit_signal(plan, 102., {}))
        self.assertIsNone(exit_signal(plan, 101., {}))
        trail = {**plan, 'exit': 'noise_trail'}
        self.assertIsNone(exit_signal(trail, 101., {'close': 101., 'noise_upper': 100.5, 'vwap': 100.}))
        self.assertIn('Momentum faded', exit_signal(trail, 101., {'close': 100.4, 'noise_upper': 100.5, 'vwap': 100.}))
        self.assertIn('VWAP', exit_signal({**plan, 'exit': 'vwap_target'}, 100.1, {'vwap': 100.}))

    def test_freshness_needs_a_bar_closed_within_two_minutes(self):
        now = pd.Timestamp('2026-10-15T13:45:00Z')
        self.assertTrue(fresh({'bar_at': '2026-10-15T13:44:00Z'}, now))
        self.assertFalse(fresh({'bar_at': '2026-10-15T13:41:00Z'}, now))
        self.assertFalse(fresh({'bar_at': None}, now))
        self.assertFalse(fresh({}, now))


class PlaybookEngineTests(DecisionFixture):
    mode = 'daytrade'

    def test_playbook_enters_without_any_trained_model(self):
        self.store.set('playbook:SPY', book())
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'BUY')
        self.assertIn('prediction models abstain', forecast['reason'])
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload['stop_loss']['stop_price'], payload['take_profit']['limit_price']), ('99.50', '101.00'))
        record = self.store.get('daytrade_position:SPY')
        self.assertEqual((record['strategy'], record['exit']), ('vwap_pullback', 'target'))

    def test_bearish_models_veto_a_setup(self):
        self.prediction('HOLD', votes=['SELL', 'SELL', 'HOLD'])
        self.store.set('playbook:SPY', book())
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'WAIT')
        self.assertIn('veto', forecast['reason'])
        self.broker.submit.assert_not_called()

    def test_stale_playbook_cannot_buy(self):
        self.store.set('playbook:SPY', book(bar_at='2026-10-15T13:40:00Z'))
        self.decision()
        self.broker.submit.assert_not_called()

    def test_entries_start_at_0935(self):
        self.broker.clock.return_value['timestamp'] = '2026-10-15T13:36:00Z'
        self.store.set('playbook:SPY', book(bar_at='2026-10-15T13:35:00Z'))
        self.assertEqual(self.decision()['action'], 'BUY')

    def hold_plan(self, exit_rule, started='2026-10-15T12:00:00Z', pnl='.002', **plan):
        self.position(pnl=pnl)
        self.store.set('daytrade_position:SPY', {'entry_id': 'e', 'started_at': started, 'name': 'Setup',
                                                 'stop': 98., 'target': None, 'exit': exit_rule, **plan})

    def test_trend_setups_ride_past_time_stops_until_their_trail(self):
        self.hold_plan('eod')
        self.store.set('playbook:SPY', book(fired=False))
        self.assertEqual(self.decision()['action'], 'HOLD')
        self.hold_plan('noise_trail')
        self.assertEqual(self.decision()['action'], 'HOLD')
        faded = book(fired=False)
        faded['indicators']['close'] = 99.6
        self.store.set('playbook:SPY', faded)
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('Momentum faded', forecast['reason'])

    def test_mean_reversion_exits_at_vwap(self):
        self.hold_plan('vwap_target', started='2026-10-15T13:40:00Z')
        self.broker.positions.return_value[0]['current_price'] = '99.9'
        self.store.set('playbook:SPY', book(fired=False))
        self.assertIn('VWAP', self.decision()['reason'])

    def test_target_setups_time_out(self):
        self.hold_plan('target', target=110.)
        self.store.set('playbook:SPY', book(fired=False))
        self.assertIn('time stop', self.decision()['reason'])


class PlaybookDataTests(TempCase):
    def test_one_request_per_minute_for_all_symbols_and_baseline_once_per_day(self):
        engine = Engine(replace(self.cfg, trading_mode='daytrade'), self.store)
        broker = Mock()
        session = minute_bars(np.linspace(100, 101, 20))
        broker.multi_bars.side_effect = lambda symbols, timeframe, start, end=None: (
            {s: session for s in symbols} if timeframe == '1Min' else {})
        now = OPEN + pd.Timedelta(minutes=20, seconds=10)
        with patch('app.engine.Alpaca', return_value=broker):
            engine.playbook_tick(['SPY', 'QQQ'], now, True)
            engine.playbook_thread.join(5)
            engine.playbook_tick(['SPY', 'QQQ'], now + pd.Timedelta(seconds=40), True)
            engine.playbook_thread.join(5)
            engine.playbook_tick(['SPY', 'QQQ'], now + pd.Timedelta(seconds=45), True)  # same 30-second slot
        timeframes = [c.args[1] for c in broker.multi_bars.call_args_list]
        self.assertEqual(timeframes, ['1Day', '5Min', '1Min', '1Min'])
        self.assertEqual(self.store.get('playbook:SPY')['bar_at'], session.index[-1].isoformat())
        self.assertIn('error', self.store.get('playbook_base:QQQ'))

    def test_daily_mode_and_closed_market_skip_the_playbook(self):
        engine = Engine(replace(self.cfg, trading_mode='daily'), self.store)
        for mode, is_open in (('daily', True), ('daytrade', False)):
            engine.config = replace(engine.config, trading_mode=mode)
            with patch('app.engine.Alpaca') as alpaca:
                engine.playbook_tick(['SPY'], OPEN + pd.Timedelta(minutes=20), is_open)
            alpaca.assert_not_called()
