from dataclasses import replace
import hashlib
import json
from unittest.mock import Mock
import pandas as pd
from app.config import Config
from app.evidence import provenance
from app.models import strategy_signature
from app.prediction_quality import forecast_quality
from app.research import Research
from test_system import TempCase


class ForecastQualityTests(TempCase):
    def test_expiry_is_per_stock_and_distinct_from_fresh_inputs(self):
        cfg=replace(self.cfg, model_max_age_hours=24)
        now=pd.Timestamp('2026-10-22T14:00:00Z')
        forecast={'symbol':'SPY','model_version':'one','bar_at':'2026-10-21T04:00:00Z'}
        meta={'version':'one','strategy_signature':strategy_signature(cfg),'data_through':forecast['bar_at'],
              'created_at':'2026-10-20T14:00:00Z'}
        self.assertEqual(forecast_quality(cfg,meta,forecast,now)['status'],'model_expired')
        recent={**meta,'created_at':'2026-10-22T10:00:00Z'}
        self.assertTrue(forecast_quality(cfg,recent,forecast,now)['ready'])
        self.assertFalse(forecast_quality(cfg,{**recent,'version':'two'},forecast,now)['ready'])

    def test_future_naive_and_bad_timestamps_abstain_and_config_is_bounded(self):
        cfg=self.cfg; now=pd.Timestamp('2026-10-22T14:00:00Z')
        f={'model_version':'one','bar_at':'2026-10-21T04:00:00Z'}
        meta={'version':'one','strategy_signature':strategy_signature(cfg),'data_through':f['bar_at']}
        for date in ('bad','2026-10-22T10:00:00','2026-10-23T00:00:00Z'):
            self.assertFalse(forecast_quality(cfg,{**meta,'created_at':date},f,now)['ready'])
        for hours in (0, float('nan'), 721):
            with self.assertRaises(ValueError):
                Config(model_max_age_hours=hours)

    def test_intraday_expired_models_abstain_but_playbook_can_still_enter(self):
        from app.engine import Engine
        from test_system import book
        cfg=replace(self.cfg,trading_mode='daytrade',model_max_age_hours=24)
        engine=Engine(cfg,self.store)
        f={'symbol':'SPY','model_version':'old','bar_at':'2026-10-15T13:40:00Z','models':[{'signal':'SELL'}]*3,
           'playbook':book()}
        self.store.set('model:SPY',{'version':'old','strategy_signature':strategy_signature(cfg),
                                 'created_at':'2026-10-13T13:40:00Z','data_through':f['bar_at']})
        self.assertEqual(engine.daytrade_decision(f,pd.Timestamp('2026-10-15T13:45:00Z'))[0],'BUY')


class EvidenceTests(TempCase):
    def test_provenance_is_reproducible_and_contains_no_credentials(self):
        news=[{'id':1,'title':'Evidence','url':'https://example.org','published_at':'2026-10-22T10:00:00Z'}]
        at=pd.Timestamp('2026-10-22T11:00:00Z')
        record=provenance('SPY',news,at,'llama','routed',{'bar_at':'bar','horizon':'next session'})
        again=provenance('SPY',news,at,'llama','routed')
        self.assertEqual(record['news_sha256'],again['news_sha256'])
        self.assertEqual(record['input_bar'],'bar')
        self.assertEqual(len(record['news_sha256']),64)
        self.assertNotIn(self.cfg.api_key,json.dumps(record))

    def test_news_window_excludes_future_stale_undated_and_duplicate_articles(self):
        r=Research(self.cfg,self.store)
        r.as_of=pd.Timestamp('2026-10-22T11:00:00Z').to_pydatetime()
        item=lambda stamp,url='https://example.org': {'headline':'news','url':url,'created_at':stamp,'summary':'s'}
        items=[item('2026-10-22T10:00:00Z'),item('2026-10-22T10:00:00Z'),
               item('2026-10-22T11:01:00Z','https://future.org'),item(None),
               item('2026-10-18T10:00:00Z','https://old.org')]
        sources,warnings=r.news('SPY',{'SPY':items})
        self.assertEqual(len(sources),1)
        self.assertEqual(sources[0]['id'],1)
        self.assertEqual(warnings,[])

    def test_legacy_auto_setting_discovers_llama_once_per_run(self):
        r=Research(replace(self.cfg,llm_models=('auto',)),self.store)
        response=Mock()
        response.json.return_value={'data':[{'id':'other-model'},{'id':'llama-installed'}]}
        r.http.get=Mock(return_value=response)
        self.assertEqual(r.models(),['llama-installed'])
        self.assertEqual(r.models(),['llama-installed'])
        r.http.get.assert_called_once()


class StockOnlyBoundaryTests(TempCase):
    def test_non_equity_asset_cannot_enter_through_generic_executor(self):
        from app.execution import Executor
        broker=Mock(base='paper')
        broker.asset.return_value={'tradable':True,'status':'active','class':'crypto'}
        self.store.set('entries_enabled',True)
        result=Executor(self.cfg,self.store,broker).execute('OTHER','BUY','2026-10-22',
            {'status':'ACTIVE','equity':'10000','cash':'10000','buying_power':'10000'},[],[])
        self.assertIn('US stock or ETF',result)
        broker.submit.assert_not_called()
