from dataclasses import replace
from unittest.mock import Mock

from app.engine import Engine
from app.models import strategy_signature
from app.portfolio import PortfolioEngine
from app.store import utcnow
from app.web import create_app
from test_system import TempCase, book, models


class DecisionFixture(TempCase):
    mode = 'daily'

    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, trading_mode=self.mode, symbols=('SPY', 'QQQ'))
        self.engine = Engine(self.cfg, self.store)
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
        self.engine.broker = self.engine.executor.broker = self.broker
        self.engine.scan_tick = Mock()
        self.engine.forecast_tick = Mock()
        self.engine.research_tick = Mock()
        self.engine.playbook_tick = Mock()
        self.store.set('entries_enabled', True)

    def prediction(self, action, symbol='SPY', news=None, stale=False, votes=None):
        at = '2026-10-15T13:40:00Z' if self.cfg.intraday else '2026-10-14T04:00:00Z'
        if stale:
            at = '2026-09-01T13:40:00Z'
        self.store.set(f'model:{symbol}', {'version': 'v1', 'data_through': at,
            'strategy_signature': strategy_signature(self.cfg)})
        self.store.set(f'prediction:{symbol}', {'symbol': symbol, 'model_version': 'v1', 'bar_at': at,
            'eligible': True, 'expected_return': {'BUY': .01, 'SELL': -.01, 'HOLD': 0}[action],
            'forecast_signal': action, 'trading_mode': self.mode, 'models': models(*(votes or [action]))})
        if self.cfg.intraday:
            self.store.set(f'playbook:{symbol}', book(action == 'BUY'))
        self.store.set(f'research:{symbol}', {'status': 'ready', 'at': utcnow(), 'bar_at': at,
            'action': news or action, 'trading_mode': self.mode})

    def position(self, pnl='-.005', owned=True):
        self.store.set('owned:SPY', owned)
        self.store.set('daytrade_position:SPY', {'entry_id': 'original-entry', 'started_at': '2026-10-15T13:44:00Z'})
        self.broker.positions.return_value = [{'symbol': 'SPY', 'qty': '4', 'market_value': '398',
            'avg_entry_price': '100', 'unrealized_plpc': pnl, 'unrealized_pl': '-2'}]

    def decision(self):
        self.engine.cycle()
        self.assertFalse(self.store.history('error'))
        return self.store.get('forecast:SPY')


class PositionDecisionTests(DecisionFixture):
    def test_neutral_unowned_stock_waits_and_never_buys(self):
        self.prediction('HOLD')
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'WAIT')
        self.assertEqual(forecast['forecast_signal'], 'HOLD')
        self.assertEqual(forecast['position']['qty'], 0)
        self.broker.submit.assert_not_called()
        self.broker.cancel.assert_not_called()

    def test_bearish_unowned_stock_waits_without_shorting(self):
        self.prediction('SELL')
        self.assertEqual(self.decision()['action'], 'WAIT')
        self.assertFalse(self.store.get('exit_pending:SPY', False))
        self.broker.submit.assert_not_called()

    def test_neutral_losing_position_above_stop_keeps_protection(self):
        self.position()
        self.prediction('HOLD')
        self.broker.open_orders.return_value = [{'symbol': 'SPY', 'side': 'sell', 'type': 'stop', 'id': 'stop'}]
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'HOLD')
        self.assertTrue(forecast['position']['owned'])
        self.assertEqual(forecast['position']['qty'], 4)
        self.broker.submit.assert_not_called()
        self.broker.cancel.assert_not_called()

    def test_disagreement_keeps_owned_shares(self):
        self.position()
        self.prediction('SELL', news='BUY', votes=['SELL', 'SELL', 'HOLD'])
        self.assertEqual(self.decision()['action'], 'HOLD')
        self.broker.submit.assert_not_called()

    def test_bullish_owned_stock_holds_and_other_candidate_can_buy(self):
        self.position()
        self.prediction('BUY')
        self.prediction('BUY', symbol='QQQ')
        prediction = self.store.get('prediction:SPY')
        self.store.set('prediction:SPY', {**prediction, 'expected_return': .02})
        self.assertEqual(self.decision()['action'], 'HOLD')
        self.broker.submit.assert_called_once()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload['symbol'], payload['side']), ('QQQ', 'buy'))

    def test_bearish_losing_position_sells_above_stop_while_entries_paused(self):
        self.position()
        self.prediction('SELL')
        self.store.set('entries_enabled', False)
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('regardless of current profit or loss', forecast['reason'])
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload['symbol'], payload['side'], payload['qty']), ('SPY', 'sell', '4'))

    def test_exit_remains_sell_after_bracket_cancel_when_forecast_turns_neutral(self):
        self.position()
        self.prediction('SELL')
        self.broker.open_orders.return_value = [{'symbol': 'SPY', 'side': 'sell', 'type': 'stop', 'id': 'stop'}]
        self.assertEqual(self.decision()['action'], 'SELL')
        self.broker.cancel.assert_called_once_with('stop')
        self.broker.submit.assert_not_called()
        self.broker.open_orders.return_value = []
        self.prediction('HOLD')
        self.assertEqual(self.decision()['action'], 'SELL')
        self.broker.submit.assert_called_once()
        self.assertEqual(self.broker.submit.call_args.args[0]['side'], 'sell')

    def test_stop_overrides_neutral_outlook_and_stale_forecast(self):
        self.position(pnl=str(-self.cfg.effective_stop))
        self.prediction('HOLD', stale=True)
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('stop-loss reached', forecast['reason'])
        self.broker.submit.assert_called_once()

    def test_account_loss_exit_overrides_bullish_outlook(self):
        self.position()
        self.prediction('BUY')
        self.broker.account.return_value['equity'] = '9700'
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('daily-loss limit', forecast['reason'])
        self.broker.submit.assert_called_once()

    def test_external_holding_waits_even_for_bearish_forecast_below_stop(self):
        self.position(pnl='-.08', owned=False)
        self.prediction('SELL')
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'WAIT')
        self.assertEqual(forecast['position']['strategy'], 'external')
        self.broker.submit.assert_not_called()

    def test_pending_entry_does_not_count_as_held_shares(self):
        self.store.set('owned:SPY', True)
        self.store.reserve_order('ensemble-pending', {'symbol': 'SPY', 'side': 'buy', 'status': 'new'})
        self.broker.order.return_value = {'id': 'pending', 'status': 'new'}
        self.prediction('HOLD')
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'WAIT')
        self.assertFalse(forecast['position']['owned'])
        self.broker.submit.assert_not_called()

    def test_missing_news_allows_bearish_exit_but_never_shorts(self):
        self.position()
        self.prediction('SELL')
        self.store.set('research:SPY', None)
        self.assertEqual(self.decision()['action'], 'SELL')
        self.broker.positions.return_value = []
        self.assertEqual(self.decision()['action'], 'WAIT')
        self.broker.submit.assert_called_once()

    def test_api_exposes_actual_position_and_decision_separately(self):
        self.position()
        self.prediction('HOLD')
        self.decision()
        data = create_app(self.cfg, self.store).test_client().get('/api/state').get_json()
        row = data['symbols'][0]
        self.assertEqual(row['forecast']['action'], 'HOLD')
        self.assertEqual(row['position']['qty'], 4)
        self.assertEqual(row['position']['avg_entry_price'], '100')
        self.assertEqual(row['position']['unrealized_plpc'], '-.005')
        self.assertTrue(row['forecast']['decision_at'])

    def test_cached_legacy_hold_is_not_presented_as_ownership(self):
        self.store.set('forecast:SPY', {'action': 'HOLD', 'forecast_signal': 'HOLD'})
        data = create_app(self.cfg, self.store).test_client().get('/api/state').get_json()
        row = data['symbols'][0]
        self.assertEqual(row['forecast']['action'], 'WAIT')
        self.assertIsNone(row['position'])


class IntradayPositionDecisionTests(PositionDecisionTests):
    mode = 'daytrade'

    def test_timed_exit_is_sell_with_neutral_forecast(self):
        self.position()
        self.prediction('HOLD')
        self.store.set('daytrade_position:SPY', {'entry_id': 'original-entry', 'started_at': '2026-10-15T13:20:00Z'})
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('holding period', forecast['reason'])
        self.broker.submit.assert_called_once()

    def test_preclose_exit_is_sell_with_stale_bullish_forecast(self):
        self.position()
        self.prediction('BUY')
        self.broker.clock.return_value['timestamp'] = '2026-10-15T19:51:00Z'
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('before market close', forecast['reason'])
        self.broker.submit.assert_called_once()


class ParallelPositionDisplayTests(TempCase):
    def test_same_holding_belongs_only_to_owning_strategy(self):
        cfg = replace(self.cfg, trading_mode='both')
        portfolio = PortfolioEngine(cfg, self.store)
        daily, daytrade = portfolio.engines
        daily.store.set('owned:SPY', True)
        self.store.set('account', {'positions': [{'symbol': 'SPY', 'qty': '4', 'avg_entry_price': '100'}]})
        client = create_app(cfg, self.store).test_client()
        daily_position = client.get('/api/state?strategy=daily').get_json()['symbols'][0]['position']
        intraday_position = client.get('/api/state?strategy=daytrade').get_json()['symbols'][0]['position']
        self.assertTrue(daily_position['owned'])
        self.assertFalse(intraday_position['owned'])
        self.assertEqual(intraday_position['strategy'], 'daily')
