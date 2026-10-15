from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time
from unittest.mock import Mock, patch
import unittest

from app.broker import BrokerError
from app.engine import Engine
from app.models import strategy_signature, train_worker, train_symbol, predict
from app.portfolio import PortfolioEngine, strategy_contexts
from app.store import Store, StrategyStore, store_for_config, utcnow
from app.web import create_app
from test_system import TempCase
from test_system import bars, book, models
from test_scanner_daytrade import intraday_bars


class ParallelTests(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, trading_mode='both', symbols=('AAA', 'BBB', 'CCC'))
        self.portfolio = PortfolioEngine(self.cfg, self.store)
        self.daily, self.daytrade = self.portfolio.engines
        self.broker = Mock(base='paper')
        self.broker.account.return_value = {'status': 'ACTIVE', 'equity': '10000', 'last_equity': '10000',
            'cash': '10000', 'buying_power': '10000'}
        self.broker.clock.return_value = {'timestamp': '2026-10-15T13:45:00Z', 'next_close': '2026-10-15T20:00:00Z', 'is_open': True}
        self.broker.positions.return_value = []
        self.broker.open_orders.return_value = []
        self.broker.latest_price.return_value = 100
        self.broker.quote.return_value = (99.99, 100)
        self.broker.asset.return_value = {'tradable': True, 'status': 'active'}
        self.broker.submit.return_value = {'id': 'accepted', 'status': 'new', 'filled_qty': '0'}
        self.broker.order.return_value = {'id': 'accepted', 'status': 'filled', 'filled_qty': '4'}
        self.portfolio.broker = self.broker
        for engine in self.portfolio.engines:
            engine.broker = engine.executor.broker = self.broker
            engine.scan_tick = Mock()
            engine.forecast_tick = Mock()
            engine.research_tick = Mock()
            engine.playbook_tick = Mock()
            engine.store.set('entries_enabled', True)

    def signal(self, engine, symbol, action='BUY'):
        at = '2026-10-15T13:40:00Z' if engine.config.intraday else '2026-10-14T04:00:00Z'
        engine.store.set(f'model:{symbol}', {'version': 'v1', 'data_through': at,
                         'strategy_signature': strategy_signature(engine.config)})
        engine.store.set(f'prediction:{symbol}', {'symbol': symbol, 'model_version': 'v1', 'bar_at': at,
            'eligible': True, 'expected_return': .01, 'forecast_signal': action, 'trading_mode': engine.config.trading_mode,
            'models': models(action)})
        if engine.config.intraday:
            engine.store.set(f'playbook:{symbol}', book(action == 'BUY'))
        engine.store.set(f'research:{symbol}', {'status': 'ready', 'at': utcnow(), 'bar_at': at,
                         'action': action, 'trading_mode': engine.config.trading_mode})

    def positions(self):
        positions = [{'symbol': 'AAA', 'qty': '4', 'market_value': '400', 'unrealized_plpc': '.001'},
                     {'symbol': 'BBB', 'qty': '4', 'market_value': '400', 'unrealized_plpc': '.001'}]
        self.daily.store.set('owned:AAA', True)
        self.daytrade.store.set('owned:BBB', True)
        self.daytrade.store.set('daytrade_position:BBB', {'entry_id': 'prior-entry', 'started_at': '2026-10-15T13:44:00Z'})
        self.broker.positions.return_value = positions
        return positions

    def test_both_strategies_buy_with_fresh_snapshots_and_independent_owners(self):
        self.signal(self.daily, 'AAA')
        self.signal(self.daytrade, 'BBB')
        self.portfolio.cycle()
        self.broker.submit.assert_called_once()
        self.assertEqual(self.broker.submit.call_args.args[0]['symbol'], 'AAA')
        self.broker.positions.return_value = [{'symbol': 'AAA', 'qty': '4', 'market_value': '400', 'unrealized_plpc': '.001'}]
        self.portfolio.cycle()
        self.assertEqual(self.broker.submit.call_count, 2)
        self.assertEqual(self.broker.submit.call_args.args[0]['symbol'], 'BBB')
        self.assertEqual(self.broker.account.call_count, 2)
        self.assertEqual(self.store.owner('AAA'), 'daily')
        self.assertEqual(self.store.owner('BBB'), 'daytrade')
        self.assertEqual({o['strategy'] for o in self.store.orders()}, {'daily', 'daytrade'})
        self.assertEqual({o['qty'] for o in self.store.orders()}, {'4'})

    def test_daytrade_close_does_not_liquidate_daily_holdings(self):
        self.positions()
        self.broker.clock.return_value['timestamp'] = '2026-10-15T19:51:00Z'
        self.portfolio.cycle()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload['symbol'], payload['side'], payload['qty']), ('BBB', 'sell', '4'))
        self.assertTrue(self.daily.store.get('owned:AAA'))
        self.assertFalse(self.daytrade.store.get('owned:AAA'))

    def test_risk_in_second_strategy_precedes_first_strategy_entry(self):
        self.signal(self.daily, 'AAA')
        self.daytrade.store.set('owned:BBB', True)
        self.daytrade.store.set('daytrade_position:BBB', {'entry_id': 'prior-entry', 'started_at': '2026-10-15T13:00:00Z'})
        self.broker.positions.return_value = [{'symbol': 'BBB', 'qty': '4', 'market_value': '400', 'unrealized_plpc': '0'}]
        self.portfolio.cycle()
        self.broker.submit.assert_called_once()
        self.assertEqual(self.broker.submit.call_args.args[0]['side'], 'sell')
        self.assertEqual(self.broker.submit.call_args.args[0]['symbol'], 'BBB')

    def test_other_strategy_cannot_buy_sell_or_cancel_owned_symbol(self):
        positions = self.positions()
        account = self.broker.account()
        for action in ('BUY', 'SELL'):
            result = self.daytrade.executor.execute('AAA', action, '2026-10-15', account, positions,
                [{'symbol': 'AAA', 'side': 'sell', 'type': 'stop', 'id': 'daily-stop'}], force_exit=True)
            self.assertIn('daily strategy', result)
        self.broker.submit.assert_not_called()
        self.broker.cancel.assert_not_called()

    def test_pausing_one_strategy_cancels_only_its_pending_entries(self):
        orders = []
        for engine, symbol, oid in ((self.daily, 'AAA', 'daily-order'), (self.daytrade, 'BBB', 'daytrade-order')):
            cid = 'ensemble-' + oid
            engine.store.reserve_order(cid, {'symbol': symbol, 'side': 'buy', 'status': 'new', 'strategy': engine.config.trading_mode})
            orders.append({'symbol': symbol, 'side': 'buy', 'id': oid, 'client_order_id': cid, 'submitted_at': '2026-10-15T13:44:30Z'})
        self.broker.open_orders.return_value = orders
        self.broker.order.return_value = {'id': 'remote', 'status': 'new', 'filled_qty': '0'}
        self.daytrade.store.set('entries_enabled', False)
        self.portfolio.cycle()
        self.broker.cancel.assert_called_once_with('daytrade-order')
        self.assertTrue(self.daily.store.get('entries_enabled'))

    def test_daily_loss_halt_pauses_both_books(self):
        self.positions()
        self.broker.account.return_value['equity'] = '9500'
        self.portfolio.cycle()
        self.assertFalse(self.daily.store.get('entries_enabled'))
        self.assertFalse(self.daytrade.store.get('entries_enabled'))
        self.assertEqual(self.store.get('loss_halt_day'), '2026-10-15')
        self.assertTrue(self.store.get('account')['loss_halt'])

    def test_ambiguous_order_in_one_strategy_blocks_both(self):
        self.daily.store.reserve_order('ensemble-unknown', {'symbol': 'AAA', 'side': 'buy', 'status': 'unknown', 'strategy': 'daily'})
        self.broker.order.side_effect = BrokerError('not found', 404)
        self.signal(self.daily, 'CCC')
        self.signal(self.daytrade, 'BBB')
        self.portfolio.cycle()
        self.broker.submit.assert_not_called()

    def test_capital_partition_preserves_daytrade_capacity_and_global_cap(self):
        self.daily.store.set('owned:AAA', True)
        positions = [{'symbol': 'AAA', 'qty': '15', 'market_value': '1500'}]
        account = self.broker.account()
        self.daily.executor.execute('CCC', 'BUY', '2026-10-15', account, positions, [])
        self.broker.submit.assert_not_called()
        self.daytrade.executor.execute('BBB', 'BUY', '2026-10-15', account, positions, [])
        self.broker.submit.assert_called_once()
        self.assertEqual(self.broker.submit.call_args.args[0]['qty'], '4')
        order = self.store.orders()[0]
        self.store.update_order(order['client_id'], {**order, 'status': 'filled'})
        positions.append({'symbol': 'EXTERNAL', 'qty': '15', 'market_value': '1500'})
        self.daytrade.executor.execute('CCC', 'BUY', '2026-10-15', account, positions, [])
        self.broker.submit.assert_called_once()

    def test_atomic_ownership_claim_cannot_be_won_by_both_strategies(self):
        def reserve(engine):
            return engine.store.reserve_order('ensemble-' + engine.config.trading_mode,
                {'symbol': 'CCC', 'side': 'buy', 'status': 'pending_submission', 'strategy': engine.config.trading_mode})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, self.portfolio.engines))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.store.orders()), 1)

    def test_rotated_out_symbol_needs_two_flat_observations_before_release(self):
        self.daily.store.set('owned:OLD', True)
        with patch('app.engine.time.time', return_value=1000):
            self.portfolio.cycle()
        self.assertEqual(self.store.owner('OLD'), 'daily')
        with patch('app.engine.time.time', return_value=1016):
            self.portfolio.cycle()
        self.assertIsNone(self.store.owner('OLD'))

    def test_training_worker_and_state_are_separate_but_journal_is_shared(self):
        self.daily.store.set('model:AAA', {'version': 'daily-existing'})
        self.daytrade.store.set('model:AAA', {'version': 'intraday-new'})
        reopened = store_for_config(self.daytrade.config)
        self.assertEqual(reopened.get('model:AAA')['version'], 'intraday-new')
        self.assertEqual(self.store.get('model:AAA')['version'], 'daily-existing')
        with patch('app.models.Alpaca') as broker, patch('app.models.train_symbol') as train:
            broker.return_value.bars.return_value = 'bars'
            train_worker(self.daytrade.config, ['AAA'])
        self.assertIsInstance(train.call_args.args[1], StrategyStore)
        self.assertEqual(train.call_args.args[1].strategy, 'daytrade')
        self.assertEqual(self.daytrade.store.get('training')['status'], 'complete')
        self.assertIsNone(self.daily.store.get('training'))

    def test_real_checkpoints_coexist_for_same_symbol_in_same_directory(self):
        daily_bars, short_bars = bars(), intraday_bars()
        train_symbol(self.daily.config, self.daily.store, 'AAA', daily_bars)
        daily_meta = self.daily.store.get('model:AAA')
        saved = (self.cfg.data_dir / 'models' / daily_meta['file']).read_bytes()
        train_symbol(self.daytrade.config, self.daytrade.store, 'AAA', short_bars)
        short_meta = self.daytrade.store.get('model:AAA')
        self.assertNotEqual(daily_meta['file'], short_meta['file'])
        self.assertEqual(saved, (self.cfg.data_dir / 'models' / daily_meta['file']).read_bytes())
        self.assertEqual(predict(self.daily.config, daily_meta, daily_bars)['trading_mode'], 'daily')
        self.assertEqual(predict(self.daytrade.config, short_meta, short_bars)['trading_mode'], 'daytrade')

    def test_single_training_worker_limit_and_fair_next_strategy(self):
        for engine in self.portfolio.engines:
            engine.trainer = None
            engine.training_tick = Mock()
        def daily_tick(allow_start=True):
            if allow_start:
                self.daily.trainer = Mock()
        self.daily.training_tick.side_effect = daily_tick
        self.portfolio.training_tick()
        self.daytrade.training_tick.assert_called_once_with(allow_start=False)
        self.portfolio.training_tick()
        self.assertEqual(self.daily.training_tick.call_count, 3)
        self.daily.trainer = None
        def daytrade_tick(allow_start=True):
            if allow_start:
                self.daytrade.trainer = Mock()
        self.daytrade.training_tick.side_effect = daytrade_tick
        self.portfolio.training_tick()
        self.assertIsNotNone(self.daytrade.trainer)

    def test_restart_keeps_ownership_and_pauses_both(self):
        self.positions()
        restarted = PortfolioEngine(self.cfg, Store(self.cfg.data_dir))
        self.assertFalse(restarted.engines[0].store.get('entries_enabled'))
        self.assertFalse(restarted.engines[1].store.get('entries_enabled'))
        self.assertEqual(self.store.owner('AAA'), 'daily')
        self.assertEqual(self.store.owner('BBB'), 'daytrade')
        with self.assertRaisesRegex(ValueError, 'TRADING_MODE=both'):
            Engine(replace(self.cfg, trading_mode='daily'), self.store)

    def test_dashboard_scopes_controls_forecasts_and_position_labels(self):
        self.positions()
        self.portfolio.cycle()
        self.daily.store.set('forecast:AAA', {'action': 'HOLD', 'horizon': 'daily'})
        self.daytrade.store.set('forecast:AAA', {'action': 'BUY', 'horizon': '15 minutes'})
        client = create_app(self.cfg, self.store).test_client()
        daily = client.get('/api/state?strategy=daily').get_json()
        daytrade = client.get('/api/state?strategy=daytrade').get_json()
        self.assertEqual(daily['portfolio_mode'], 'both')
        self.assertEqual(len(daily['strategies']), 2)
        self.assertEqual(daily['symbols'][0]['forecast']['horizon'], 'daily')
        self.assertEqual(daytrade['symbols'][0]['forecast']['horizon'], '15 minutes')
        self.assertEqual({p['strategy'] for p in daily['account']['positions']}, {'daily', 'daytrade'})
        headers = {'X-Bot-Control': 'dashboard'}
        self.assertEqual(client.post('/api/control', json={'action': 'pause', 'strategy': 'daytrade'}, headers=headers).status_code, 200)
        self.assertTrue(self.daily.store.get('entries_enabled'))
        self.assertFalse(self.daytrade.store.get('entries_enabled'))
        self.assertEqual(client.post('/api/control', json={'action': 'resume'}, headers=headers).status_code, 200)
        self.assertTrue(self.daytrade.store.get('entries_enabled'))
        self.assertEqual(client.post('/api/control', json={'action': 'train', 'strategy': 'daytrade'}, headers=headers).status_code, 200)
        self.assertTrue(self.daytrade.store.get('train_requested'))
        self.assertFalse(self.daily.store.get('train_requested', False))
        self.assertEqual(client.get('/api/state?strategy=invalid').status_code, 400)
        self.assertEqual(client.post('/api/control', json={'action': 'pause', 'strategy': []}, headers=headers).status_code, 400)


class UpgradeTests(TempCase):
    def test_existing_daily_positions_models_and_journal_survive_upgrade(self):
        Engine(self.cfg, self.store)
        self.store.set('owned:SPY', True)
        self.store.set('model:SPY', {'version': 'existing-daily'})
        self.store.reserve_order('ensemble-legacy', {'symbol': 'SPY', 'side': 'buy', 'status': 'filled'})
        portfolio = PortfolioEngine(replace(self.cfg, trading_mode='both'), self.store)
        daily, daytrade = portfolio.engines
        self.assertTrue(daily.store.get('owned:SPY'))
        self.assertFalse(daytrade.store.get('owned:SPY'))
        self.assertEqual(daily.store.get('model:SPY')['version'], 'existing-daily')
        self.assertIsNone(daytrade.store.get('model:SPY'))
        self.assertTrue(daily.store.owns_order({'client_order_id': 'ensemble-legacy'}))
        self.assertFalse(daytrade.store.owns_order({'client_order_id': 'ensemble-legacy'}))
        self.assertEqual(len(self.store.orders()), 1)


if __name__ == '__main__':
    unittest.main()
