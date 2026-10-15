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


class BatchResearchTests(TempCase):
    def research(self, content):
        research = Research(replace(self.cfg, llm_models=('auto',)), self.store)
        research.market_news = Mock(return_value={'SPY': [], 'QQQ': [], 'IWM': []})
        research.news = Mock(side_effect=lambda symbol, alpaca, limit, first_id: (
            [{'id': first_id, 'title': symbol, 'summary': '', 'published_at': 'now', 'url': 'https://x', 'provider': 'p'}], []))
        response = Mock(ok=True)
        response.json.return_value = {'model': 'routed', 'choices': [{'message': {'content': json.dumps(content)}}]}
        research.http.post = Mock(return_value=response)
        return research

    def test_one_request_returns_an_opinion_for_every_stock(self):
        research = self.research({'stocks': [
            {'symbol': 'SPY', 'action': 'BUY', 'confidence': .8, 'rationale': 'Upgrade', 'source_ids': [1]},
            {'symbol': 'QQQ', 'action': 'SELL', 'confidence': .9, 'rationale': 'Miss', 'source_ids': [2]},
            {'symbol': 'QQQ', 'action': 'BUY', 'confidence': .9, 'rationale': 'Duplicate', 'source_ids': [2]},
            {'symbol': 'IWM', 'action': 'BUY', 'confidence': .9, 'rationale': 'Cites SPY news', 'source_ids': [1]},
            {'symbol': 'TSLA', 'action': 'BUY', 'confidence': .9, 'rationale': 'Not asked', 'source_ids': [1]}]})
        results = research.analyze_all([forecast('SPY'), forecast('QQQ'), forecast('IWM')])
        research.http.post.assert_called_once()
        sent = json.loads(research.http.post.call_args.kwargs['json']['messages'][1]['content'])
        self.assertEqual([s['symbol'] for s in sent['stocks']], ['SPY', 'QQQ', 'IWM'])
        self.assertEqual({s: r['action'] for s, r in results.items()}, {'SPY': 'BUY', 'QQQ': 'SELL', 'IWM': 'HOLD'})
        self.assertEqual(self.store.get('research:SPY')['bar_at'], '2026-10-14T04:00:00Z')
        self.assertIsNone(self.store.get('research:TSLA'))


class ResearchScheduleTests(TempCase):
    def engine(self, mode='daily'):
        engine = Engine(replace(self.cfg, trading_mode=mode), self.store)
        self.calls = []
        def analyze_all(research, forecasts):
            self.calls.append([f['symbol'] for f in forecasts])
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

    def test_waits_briefly_for_all_forecasts_of_the_new_bar(self):
        engine = self.engine()
        mixed = [forecast('SPY', '2026-10-15T04:00:00Z'), forecast('QQQ')]
        self.tick(engine, mixed)
        self.assertEqual(self.calls, [])
        self.store.set('research_batch', {**self.store.get('research_batch'), 'first_seen': time.time() - 601})
        self.tick(engine, mixed)
        self.assertEqual(self.calls, [['SPY']])

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

    def test_entry_bracket_uses_volatility_stop(self):
        self.prediction('BUY')
        prediction = self.store.get('prediction:SPY')
        self.store.set('prediction:SPY', {**prediction, 'setup': {'signal': 'BUY', 'atr': .002}})
        self.decision()
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual(payload['stop_loss']['stop_price'], '99.60')


class VoteConfigTests(TempCase):
    def test_vote_and_trail_settings_are_bounded(self):
        for options in ({'min_votes': 1}, {'min_votes': 5}, {'daytrade_trail_gap': .01, 'daytrade_trail_trigger': .005},
                        {'daytrade_run_minutes': 10, 'daytrade_hold_minutes': 15}):
            with self.assertRaises(ValueError):
                Config(**options)
