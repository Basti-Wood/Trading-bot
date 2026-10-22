from dataclasses import replace
import json
import time
from unittest.mock import Mock, patch

from app.config import Config
from app.engine import Engine
from app.research import Research
from test_decisions import DecisionFixture
from test_system import TempCase, models


def forecast(symbol, bar_at='2026-10-14T04:00:00Z'):
    return {'symbol': symbol, 'bar_at': bar_at, 'last_close': 100., 'horizon': 'next session',
            'forecast_signal': 'BUY', 'expected_return': .01, 'models': models('BUY')}


class PerStockResearchTests(TempCase):
    def research(self):
        research = Research(replace(self.cfg, llm_models=('llama-test',)), self.store)
        research.news = Mock(side_effect=lambda symbol, **kwargs: (
            [{'id': 1, 'title': symbol, 'summary': '', 'published_at': 'now', 'url': 'https://x', 'provider': 'p'}], []))
        research.overview = Mock(return_value={'status': 'ready', 'summary': 'Market news', 'source_ids': [1], 'sources': []})
        def reply(*args, **kwargs):
            stock = json.loads(kwargs['json']['messages'][1]['content'])['stocks'][0]
            symbol = stock['symbol']
            if symbol == 'QQQ':
                raise ValueError('QQQ connection failed')
            action = 'BUY' if symbol == 'SPY' else 'SELL'
            response = Mock(ok=True)
            response.json.return_value = {'model': 'llama-routed', 'choices': [{'message': {'content': json.dumps({'stocks': [
                {'symbol': symbol, 'action': action, 'confidence': .9, 'rationale': symbol+' own summary', 'source_ids': [1]}]})}}]}
            return response
        research.http.post = Mock(side_effect=reply)
        return research

    def test_one_request_per_stock_and_one_vote_without_cross_stock_failure(self):
        research = self.research()
        result = research.analyze_all([forecast(s) for s in ('SPY', 'QQQ', 'IWM')])
        self.assertEqual(research.http.post.call_count, 3)
        sent = [json.loads(c.kwargs['json']['messages'][1]['content'])['stocks'] for c in research.http.post.call_args_list]
        self.assertEqual([len(stocks) for stocks in sent], [1, 1, 1])
        self.assertEqual([stocks[0]['symbol'] for stocks in sent], ['SPY', 'QQQ', 'IWM'])
        self.assertEqual(result['SPY']['action'], 'BUY')
        self.assertEqual(result['QQQ']['status'], 'unavailable')
        self.assertEqual(result['IWM']['action'], 'SELL')
        self.assertEqual(len(result['SPY']['opinions']), 1)
        report = self.store.get('news_report')
        self.assertEqual(set(report['stocks']), {'SPY', 'QQQ', 'IWM'})
        self.assertEqual(report['overview']['summary'], 'Market news')
        self.assertEqual(self.store.get('news_report:'+report['day']), report)
        research.analyze_all([forecast('SPY'), forecast('QQQ'), forecast('IWM')])
        self.assertEqual(research.http.post.call_count, 4) # only the failed stock retries
        research.overview.assert_called_once()

    def test_cross_stock_or_duplicate_reply_cannot_cast_a_vote(self):
        research = self.research()
        research.ask = Mock(return_value=([{'symbol': 'OTHER', 'action': 'BUY'}], 'llama'))
        self.assertEqual(research.analyze_all([forecast('SPY')])['SPY']['status'], 'unavailable')
        own = {'symbol': 'SPY', 'action': 'BUY', 'confidence': .9, 'rationale': 'r', 'source_ids': [1]}
        research.ask.return_value = ([own, own], 'llama')
        self.assertEqual(research.analyze_all([forecast('SPY')])['SPY']['status'], 'unavailable')

    def test_multi_stock_request_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.research().ask('llama', [{'symbol': 'SPY'}, {'symbol': 'QQQ'}])

    def test_no_news_and_invalid_citations_abstain(self):
        research = self.research()
        research.news.return_value = ([], [])
        research.news.side_effect = None
        self.assertEqual(research.analyze_all([forecast('SPY')])['SPY']['action'], 'HOLD')
        research.http.post.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'source'):
            Research.validate({'action': 'BUY', 'confidence': .9, 'rationale': 'r', 'source_ids': [99]}, [{'id': 1}])


class ResearchScheduleTests(TempCase):
    def engine(self, mode='daily'):
        engine = Engine(replace(self.cfg, trading_mode=mode), self.store)
        self.calls = []
        def analyze_all(research, forecasts):
            self.calls.append([f['symbol'] for f in forecasts])
            return {f['symbol']: {'status': 'ready'} for f in forecasts}
        patcher = patch('app.engine.Research.analyze_all', analyze_all)
        patcher.start()
        self.addCleanup(patcher.stop)
        return engine

    def tick(self, engine, forecasts):
        engine.research_tick(forecasts)
        if engine.research_thread:
            engine.research_thread.join(5)

    def test_llm_asked_once_per_daily_bar_for_all_stocks(self):
        engine = self.engine()
        both = [forecast('SPY'), forecast('QQQ')]
        for _ in range(3):
            self.tick(engine, both)
        self.assertEqual(self.calls, [['SPY', 'QQQ']])
        self.tick(engine, [forecast('SPY', '2026-10-15T04:00:00Z'), forecast('QQQ', '2026-10-15T04:00:00Z')])
        self.assertEqual(len(self.calls), 2)

    def test_new_stock_and_mixed_bars_are_scheduled_independently(self):
        engine = self.engine()
        self.tick(engine, [forecast('SPY')])
        self.tick(engine, [forecast('SPY'), forecast('QQQ')])
        self.tick(engine, [forecast('SPY', '2026-10-15T04:00:00Z'), forecast('QQQ')])
        self.assertEqual(self.calls, [['SPY'], ['QQQ'], ['SPY']])

    def test_daytrade_never_calls_the_llm(self):
        engine = self.engine('daytrade')
        self.tick(engine, [forecast('SPY')])
        self.assertEqual(self.calls, [])
        self.assertIn('not used for day trading', self.store.get('research_status')['status'])


class DaytradeExitTests(DecisionFixture):
    mode = 'daytrade'

    def hold(self, pnl, started='2026-10-15T13:44:00Z', peak=None):
        self.position(pnl=pnl)
        record = {'entry_id': 'original-entry', 'started_at': started}
        if peak is not None:
            record['peak'] = peak
        self.store.set('daytrade_position:SPY', record)

    def test_trailing_stop_locks_in_profit(self):
        self.hold('.003', peak=.008)
        self.prediction('BUY')
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('Trailing profit stop', forecast['reason'])

    def test_peak_is_tracked_while_trade_runs(self):
        self.hold('.007')
        self.prediction('HOLD')
        self.decision()
        self.assertEqual(self.store.get('daytrade_position:SPY')['peak'], .007)
        self.broker.submit.assert_not_called()

    def test_winner_keeps_running_while_votes_stay_bullish(self):
        self.hold('.002', started='2026-10-15T13:20:00Z')
        self.prediction('BUY')
        self.assertEqual(self.decision()['action'], 'HOLD')
        self.broker.submit.assert_not_called()

    def test_winner_extension_ends(self):
        self.hold('.002', started='2026-10-15T12:40:00Z')
        self.prediction('BUY')
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('extension used up', forecast['reason'])

    def test_loser_exits_at_holding_period_even_when_bullish(self):
        self.hold('-.002', started='2026-10-15T13:20:00Z')
        self.prediction('BUY')
        self.assertEqual(self.decision()['action'], 'SELL')


class PercentSizingTests(TempCase):
    def buy(self, equity, symbol, price=100., fractionable=True, **options):
        from app.execution import Executor
        broker = Mock(base='paper')
        broker.asset.return_value = {'tradable': True, 'status': 'active', 'fractionable': fractionable}
        broker.latest_price.return_value = price
        broker.submit.return_value = {'id': 'x', 'status': 'filled'}
        self.broker = broker
        self.store.set('entries_enabled', True)
        executor = Executor(replace(self.cfg, **options), self.store, broker)
        account = {'status': 'ACTIVE', 'equity': str(equity), 'cash': str(equity), 'buying_power': str(equity)}
        self.status = executor.execute(symbol, 'BUY', '2026-10-15', account, [], [])
        return float(broker.submit.call_args.args[0]['qty']) if broker.submit.called else 0

    def test_expensive_stock_is_bought_in_fractions_without_bracket(self):
        # $500 budget, AMD at $580: the old code refused with "cannot fund one whole share".
        qty = self.buy(10000, 'AMD', price=580., capital=10000, strategy_fraction=.5)
        self.assertAlmostEqual(qty, .8603)
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload['type'], payload['time_in_force']), ('limit', 'day'))
        self.assertNotIn('order_class', payload)
        self.assertIn('fractional shares', self.status)
        self.assertEqual(self.store.get('unbracketed:AMD'), {'stop': self.cfg.stop_loss, 'target': self.cfg.take_profit})
        self.assertEqual(self.buy(10000, 'MSFT', price=580., fractionable=False, capital=10000, strategy_fraction=.5), 0)
        self.assertIn('cannot be bought in fractions', self.status)
        self.assertEqual(self.buy(5, 'NVDA', price=580.), 0)
        self.assertIn('$1 minimum', self.status)

    def test_whole_share_entries_keep_brackets(self):
        self.buy(100000, 'SPY')
        self.assertEqual(self.broker.submit.call_args.args[0]['order_class'], 'bracket')
        self.assertIsNone(self.store.get('unbracketed:SPY'))

    def test_position_size_scales_with_account_equity(self):
        # 10% per position of the equity share; limit price 100.20.
        self.assertEqual(self.buy(100000, 'AAA'), 99)
        self.assertEqual(self.buy(2000, 'BBB', capital_fraction=1.), 1)
        self.assertEqual(self.buy(100000, 'CCC', capital_fraction=.5), 49)
        self.assertEqual(self.buy(100000, 'DDD', capital=10000), 9)

    def test_sizing_capital(self):
        self.assertEqual(Config(capital_fraction=.25).sizing_capital(40000), 10000)
        self.assertEqual(Config(capital=5000).sizing_capital(40000), 5000)
        for options in ({'capital': 50}, {'capital_fraction': 0}, {'capital_fraction': 1.5}):
            with self.assertRaises(ValueError):
                Config(**options)


class BotManagedExitTests(DecisionFixture):
    def test_fractional_position_take_profit_and_stop_enforced_by_engine(self):
        self.position(pnl='.11')
        self.store.set('unbracketed:SPY', {'stop': .05, 'target': .10})
        self.prediction('HOLD')
        forecast = self.decision()
        self.assertEqual(forecast['action'], 'SELL')
        self.assertIn('Take-profit reached', forecast['reason'])

    def test_guard_cleared_when_flat(self):
        self.store.set('owned:SPY', True)
        self.store.set('unbracketed:SPY', {'stop': .05, 'target': .10})
        self.prediction('HOLD')
        self.decision()
        self.assertIsNone(self.store.get('unbracketed:SPY'))


class VoteConfigTests(TempCase):
    def test_vote_and_trail_settings_are_bounded(self):
        for options in ({'min_votes': 1}, {'min_votes': 5}, {'daytrade_trail_gap': .01, 'daytrade_trail_trigger': .005},
                        {'daytrade_run_minutes': 10, 'daytrade_hold_minutes': 15}):
            with self.assertRaises(ValueError):
                Config(**options)
