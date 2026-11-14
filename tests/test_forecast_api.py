import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from renewal_value.application import ROOT, server
from renewal_value.application import Application


class ForecastApiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.directory = tempfile.TemporaryDirectory()
        self.application = Application(ROOT / 'fixtures/data.json', Path(self.directory.name) / 'state.sqlite3', ROOT / 'web')
        self.httpd = server(self.application)
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'

    def tearDown(self):
        self.httpd.shutdown()
        self.worker.join()
        self.httpd.server_close()
        self.directory.cleanup()

    def get(self, path):
        with urlopen(self.base + path) as response:
            return json.load(response)

    def test_forecast_roundtrip_and_causal_separation(self):
        value = self.get('/api/forecast?' + urlencode({'cutoff': 24, 'contact_cost': 40}))
        result = value['result']
        self.assertEqual(result['horizon'], 6)
        self.assertTrue(result['rows'])
        self.assertTrue(result['cohorts'])
        self.assertEqual(value['options']['contact_cost'], 40.0)
        for row in result['rows']:
            self.assertIsNone(row['causal_uplift'])
            self.assertIn('expected_net_cash', row)
            self.assertLessEqual(row['p10'], row['p50'])
            self.assertLessEqual(row['p50'], row['p90'])
        # 批次汇总与客户行总额衔接。
        self.assertAlmostEqual(
            sum(row['expected_net_cash'] for row in result['cohorts']),
            sum(row['expected_net_cash'] for row in result['rows']),
        )
        self.assertTrue(any('因果' in note for note in result['notes']))

    def test_cost_change_re_ranks_without_changing_prediction(self):
        cheap = self.get('/api/forecast?' + urlencode({'cutoff': 24, 'contact_cost': 10}))
        expensive = self.get('/api/forecast?' + urlencode({'cutoff': 24, 'contact_cost': 500}))
        cheap_map = {row['account']: row['expected_net_cash'] for row in cheap['result']['rows']}
        for row in expensive['result']['rows']:
            self.assertEqual(cheap_map[row['account']], row['expected_net_cash'])
        self.assertGreater(
            cheap['result']['summary']['建议回访客户数'],
            expensive['result']['summary']['建议回访客户数'],
        )

    def test_forecast_group_filter_and_capacity(self):
        query = urlencode({'cutoff': 24, 'group': '渠道1', 'capacity': 5}, quote_via=quote)
        value = self.get('/api/forecast?' + query)
        self.assertTrue({row['cohort'] for row in value['result']['rows']} <= {'渠道1'})
        self.assertEqual(sum(1 for row in value['result']['rows'] if row['recommend_contact']), 5)

    def test_backtest_endpoint_returns_folds_and_summary(self):
        value = self.get('/api/backtest?' + urlencode({'start': 10, 'end': 14}))
        self.assertEqual(value['options'], {'start': 10, 'end': 14, 'group': ''})
        self.assertEqual(value['result']['cutoffs'], [10, 11, 12, 13, 14])
        self.assertIsNotNone(value['result']['summary']['mae'])
        for fold in value['result']['folds']:
            for row in fold['rows']:
                self.assertIn('expected_net_cash', row)
                self.assertIn('actual_net_cash', row)

    def test_forecast_validation_errors(self):
        queries = [
            'cutoff=1.5', 'cutoff=999', urlencode({'group': '未知'}, quote_via=quote),
            'contact_cost=-1', 'contact_cost=abc', 'contact_cost=nan', 'contact_cost=inf',
            'capacity=1.5', 'capacity=-2', 'unexpected=1',
        ]
        for query in queries:
            with self.assertRaises(HTTPError) as caught:
                self.get('/api/forecast?' + query)
            self.assertEqual(caught.exception.code, 400, query)

    def test_backtest_validation_errors(self):
        for query in ['start=20&end=10', 'start=abc', urlencode({'group': '未知'}, quote_via=quote)]:
            with self.assertRaises(HTTPError) as caught:
                self.get('/api/backtest?' + query)
            self.assertEqual(caught.exception.code, 400, query)

    def test_backtest_group_filter_recounts_excluded(self):
        from urllib.parse import quote
        all_value = self.get('/api/backtest?' + urlencode({'start': 8, 'end': 10}))
        group_value = self.get('/api/backtest?' + urlencode({'start': 8, 'end': 10, 'group': '渠道1'}, quote_via=quote))
        for all_fold, group_fold in zip(all_value['result']['folds'], group_value['result']['folds']):
            self.assertLessEqual(group_fold['excluded'], all_fold['excluded'])
            self.assertTrue({row['cohort'] for row in group_fold['rows']} <= {'渠道1'})

    def test_forecast_does_not_use_information_from_its_own_window(self):
        # cutoff=24 的训练 cutoff 全部严格小于 24。
        value = self.get('/api/forecast?' + urlencode({'cutoff': 24}))
        self.assertTrue(all(c < 24 for c in value['result']['training_cutoffs']))
        early = self.get('/api/forecast?' + urlencode({'cutoff': 12}))
        self.assertTrue(all(c < 12 for c in early['result']['training_cutoffs']))


if __name__ == '__main__':
    unittest.main()
