from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.broker import Alpaca
from app.config import Config
from app.feedback import capture_forecast
from app.models import MODEL_NAMES, predict, strategy_signature
from app.research import Research, combine
from app.store import utcnow
from app.web import create_app
from test_decisions import DecisionFixture
from test_feedback import session_bars, snapshot
from test_system import TempCase, bars


class VotingTests(unittest.TestCase):
    def test_two_votes_survive_neutral_or_opposing_third_model_in_real_prediction(self):
        cfg = Config()
        for values, expected in [([.0031, .0032, 0], 'BUY'), ([-.0031, -.0032, 0], 'SELL'),
                                 ([.01, .02, -.50], 'BUY'), ([0, 0, 0], 'HOLD'), ([.01, 0, 0], 'HOLD')]:
            with self.subTest(values=values):
                models = {name: Mock(predict=lambda row, v=value: np.full(len(row), v))
                          for name, value in zip(MODEL_NAMES, values)}
                meta = {'symbol': 'SPY', 'version': 'v1', 'eligible': True,
                        'reports': {name: {'holdout': {}} for name in MODEL_NAMES}}
                with patch('app.models.load_bundle', return_value={'models': models}):
                    result = predict(cfg, meta, bars())
                self.assertEqual(result['forecast_signal'], expected)
                self.assertEqual(result['expected_return'], float(np.median(values)))

    def test_directional_news_vetoes_but_neutral_and_missing_news_abstain(self):
        for vote in ('BUY', 'SELL'):
            forecast = {'forecast_signal': vote, 'bar_at': 'bar', 'eligible': True}
            opposite = 'SELL' if vote == 'BUY' else 'BUY'
            news = {'status': 'ready', 'at': utcnow(), 'bar_at': 'bar'}
            for action in ('HOLD', vote):
                self.assertEqual(combine(forecast, {**news, 'action': action})[0], vote)
            self.assertEqual(combine(forecast, None)[0], vote)
            self.assertEqual(combine(forecast, {**news, 'action': opposite})[0], 'HOLD')
            self.assertEqual(combine(forecast, {**news, 'action': opposite, 'at': 'invalid'})[0], vote)

    def test_news_cannot_create_entry_without_two_price_votes_or_validation(self):
        news = {'status': 'ready', 'at': utcnow(), 'bar_at': 'bar', 'action': 'BUY'}
        self.assertEqual(combine({'forecast_signal': 'HOLD', 'bar_at': 'bar', 'eligible': True}, news)[0], 'HOLD')
        self.assertEqual(combine({'forecast_signal': 'BUY', 'bar_at': 'bar', 'eligible': False}, news)[0], 'HOLD')

    def test_neutral_llm_does_not_hide_another_llms_confident_bearish_warning(self):
        forecast = {'forecast_signal': 'BUY', 'bar_at': 'bar', 'eligible': True}
        news = {'status': 'ready', 'at': utcnow(), 'bar_at': 'bar', 'action': 'HOLD',
                'opinions': [{'action': 'HOLD'}, {'action': 'SELL'}]}
        self.assertEqual(combine(forecast, news)[0], 'HOLD')

    def test_weak_or_uncited_bearish_news_abstains(self):
        forecast = {'forecast_signal': 'BUY', 'bar_at': 'bar', 'eligible': True}
        for confidence, sources in ((.4, [1]), (.9, [])):
            opinion = Research.validate(json.dumps({'action': 'SELL', 'confidence': confidence,
                'source_ids': sources, 'rationale': 'Uncertain direction'}), [{'id': 1}])
            news = {'status': 'ready', 'at': utcnow(), 'bar_at': 'bar', 'action': opinion['action'], 'opinions': [opinion]}
            self.assertEqual(combine(forecast, news)[0], 'BUY')


class QueuedOrderTests(DecisionFixture):
    def setUp(self):
        super().setUp()
        self.broker.clock.return_value.update(timestamp='2026-10-15T21:00:00Z',
            next_open='2026-10-16T13:30:00Z', is_open=False)
        self.prediction('BUY', news='HOLD')
        self.forecast = {**self.store.get('prediction:SPY'), 'bar_at': '2026-10-15T04:00:00Z',
                         'target_session': '2026-10-16', 'last_close': 100.}
        self.store.set('prediction:SPY', self.forecast)
        self.store.set('model:SPY', {'version': 'v1', 'data_through': self.forecast['bar_at'],
                                   'strategy_signature': strategy_signature(self.cfg)})
        self.store.set('research:SPY', {'status': 'ready', 'action': 'HOLD', 'at': utcnow(), 'bar_at': self.forecast['bar_at']})

    def pending(self):
        self.decision()
        record = self.store.orders()[0]
        self.broker.order.return_value = {'id': 'accepted', 'status': 'new', 'filled_qty': '0'}
        self.broker.open_orders.return_value = [{'id': 'accepted', 'symbol': 'SPY', 'side': 'buy', 'type': 'limit',
            'client_order_id': record['client_id'], 'submitted_at': '2026-10-15T21:00:00Z'}]
        return record

    def test_closed_market_submits_daily_limit_bracket_for_target_session(self):
        forecast = self.decision()
        self.assertIn('queued for regular session 2026-10-16', forecast['reason'])
        payload = self.broker.submit.call_args.args[0]
        self.assertEqual((payload['side'], payload['type'], payload['order_class'], payload['time_in_force']),
                         ('buy', 'limit', 'bracket', 'day'))
        self.assertFalse(payload['extended_hours'])
        self.assertEqual(payload['limit_price'], '100.20')
        self.assertIn('stop_loss', payload)
        self.assertIn('take_profit', payload)
        record = self.store.orders()[0]
        self.assertEqual(record['session'], '2026-10-16')
        self.assertTrue(record['queued_for_open'])
        self.broker.latest_price.assert_not_called()
        self.broker.quote.assert_not_called()

    def test_queue_survives_midnight_and_first_five_open_minutes_without_duplicate(self):
        record = self.pending()
        for timestamp, opened in [('2026-10-16T05:00:00Z', False), ('2026-10-16T13:30:00Z', True),
                                  ('2026-10-16T13:36:00Z', True)]:
            self.broker.clock.return_value.update(timestamp=timestamp, is_open=opened)
            self.decision()
        self.broker.cancel.assert_not_called()
        self.broker.submit.assert_called_once()
        self.assertEqual(len(self.store.orders()), 1)
        self.assertIn('2026-10-16', record['client_id'])

    def test_weekend_uses_monday_session_not_submission_date(self):
        self.broker.clock.return_value.update(timestamp='2026-10-16T21:00:00Z', next_open='2026-10-19T13:30:00Z')
        self.store.set('prediction:SPY', {**self.forecast, 'bar_at': '2026-10-16T04:00:00Z', 'target_session': '2026-10-19'})
        self.decision()
        self.assertEqual(self.store.orders()[0]['session'], '2026-10-19')

    def test_old_session_forecast_cannot_queue_for_new_session(self):
        self.store.set('prediction:SPY', {**self.forecast, 'target_session': '2026-10-15'})
        self.assertIn('targeting the next market session', self.decision()['reason'])
        self.broker.submit.assert_not_called()

    def test_missing_broker_next_open_cannot_queue(self):
        del self.broker.clock.return_value['next_open']
        self.decision()
        self.broker.submit.assert_not_called()

    def test_neutral_forecasts_do_not_queue_a_buy(self):
        self.store.set('prediction:SPY', {**self.forecast, 'forecast_signal': 'HOLD'})
        self.assertEqual(self.decision()['action'], 'WAIT')
        self.broker.submit.assert_not_called()

    def test_bearish_news_cancels_queued_bullish_entry(self):
        self.pending()
        self.store.set('research:SPY', {'status': 'ready', 'action': 'SELL', 'at': utcnow(), 'bar_at': self.forecast['bar_at']})
        self.assertEqual(self.decision()['action'], 'WAIT')
        self.broker.cancel.assert_called_once_with('accepted')
        self.broker.submit.assert_called_once()

    def test_neutralized_prediction_cancels_queue(self):
        self.pending()
        self.store.set('prediction:SPY', {**self.forecast, 'forecast_signal': 'HOLD'})
        self.decision()
        self.broker.cancel.assert_called_once_with('accepted')

    def test_paused_entries_cancel_queue(self):
        self.pending()
        self.store.set('entries_enabled', False)
        self.decision()
        self.broker.cancel.assert_called_once_with('accepted')

    def test_unfilled_queue_canceled_after_daily_entry_window(self):
        self.pending()
        self.broker.clock.return_value.update(timestamp='2026-10-16T14:00:00Z', is_open=True)
        self.decision()
        self.broker.cancel.assert_called_once_with('accepted')

    def test_queued_buy_reserves_capacity_across_candidates(self):
        self.pending()
        self.prediction('BUY', symbol='QQQ', news='HOLD')
        self.store.set('prediction:QQQ', {**self.forecast, 'symbol': 'QQQ'})
        self.decision()
        self.broker.submit.assert_called_once()

    def test_queue_rejects_invalid_prices_and_disabled_setting(self):
        for price in (None, 0, float('nan')):
            result = self.engine.executor.execute('SPY', 'BUY', '2026-10-16', self.broker.account(), [], [],
                                                  queued_for_open=True, reference_price=price)
            self.assertIn('Invalid reference price', result)
        self.engine.config = replace(self.cfg, queue_daily_orders=False)
        self.decision()
        self.broker.submit.assert_not_called()

    def test_no_llm_key_required_to_enable_entries(self):
        self.store.set('account', {'loss_halt': False})
        client = create_app(replace(self.cfg, llm_key=''), self.store).test_client()
        self.assertEqual(client.post('/api/control', json={'action': 'resume'},
            headers={'X-Bot-Control': 'dashboard'}).status_code, 200)


class IntradayQueueTests(DecisionFixture):
    mode = 'daytrade'

    def test_intraday_cannot_queue_overnight(self):
        self.prediction('BUY', news='HOLD')
        self.broker.clock.return_value.update(is_open=False, next_open='2026-10-16T13:30:00Z')
        self.decision()
        self.broker.submit.assert_not_called()
        result = self.engine.executor.execute('SPY', 'BUY', '2026-10-16', self.broker.account(), [], [],
                                              queued_for_open=True, reference_price=100.)
        self.assertIn('Only daily buy orders', result)
        self.broker.submit.assert_not_called()


class CompletedSessionTests(TempCase):
    def test_half_day_completion_and_holiday_target(self):
        broker = Alpaca(self.cfg)
        broker.calendar = Mock(return_value=[{'date': '2026-11-25', 'close': '16:00'},
            {'date': '2026-11-27', 'close': '13:00'}, {'date': '2026-11-30', 'close': '16:00'}])
        broker.request = Mock(return_value={'bars': [
            {'t': f'2026-11-{day}T05:00:00Z', 'o': 100, 'h': 101, 'l': 99, 'c': 100, 'v': 1000} for day in (25, 27)]})
        before = broker.bars('SPY', datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc))
        self.assertEqual(before.index[-1].day, 25)
        after = broker.bars('SPY', datetime(2026, 11, 27, 18, 16, tzinfo=timezone.utc))
        self.assertEqual(after.index[-1].day, 27)
        self.assertEqual(after.attrs['target_session'], '2026-11-30')
        broker.calendar.assert_called_once()

    def test_after_close_forecast_can_be_recorded_but_incomplete_bar_cannot(self):
        b = session_bars()
        record = snapshot(b, 100)
        date = b.index[100].tz_convert('America/New_York').date().isoformat()
        record.update(at=f'{date}T17:00:00-04:00', input_complete_at=f'{date}T16:16:00-04:00')
        self.assertTrue(capture_forecast(self.cfg, self.store, record))
        with self.assertRaises(ValueError):
            capture_forecast(self.cfg, self.store, {**record, 'at': f'{date}T15:00:00-04:00'})
