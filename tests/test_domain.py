import copy
import json
import unittest

from renewal_value.application import ROOT
from renewal_value.domain import run


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / 'fixtures/data.json').read_text())
        self.data = {key: [] for key in self.fixture['tables']}

    def test_fixture_is_deterministic_and_not_mutated(self):
        original = copy.deepcopy(self.fixture['tables'])
        options = {'cutoff': self.fixture['meta']['default_cutoff'], 'group': ''}
        first = run(self.fixture['tables'], options)
        second = run(self.fixture['tables'], options)
        self.assertEqual(first, second)
        self.assertEqual(original, self.fixture['tables'])
        self.assertTrue(first['rows'])
        json.dumps(first, allow_nan=False)

    def sample(self):
        self.data['accounts'] = [dict(account='a', cohort='一月', acquired_at=1)]
        self.data['spells'] = [dict(spell='s1', account='a', start=1, end=3, status='ended', known_at=1), dict(spell='s2', account='a', start=4, end=None, status='active', known_at=4)]
        self.data['ledger'] = [dict(account='a', kind='charge', amount=100, effective_at=1, known_at=1), dict(account='a', kind='payment', amount=60, effective_at=2, known_at=2), dict(account='a', kind='refund', amount=20, effective_at=2, known_at=6)]

    def test_cash_is_not_charge_and_future_refund_is_hidden(self):
        self.sample()
        early = run(self.data, {'cutoff': 5, 'group': ''})['rows'][0]
        later = run(self.data, {'cutoff': 6, 'group': ''})['rows'][0]
        self.assertEqual(early['cash'], 60)
        self.assertEqual(early['outstanding'], 40)
        self.assertEqual(later['cash'], 40)
        self.assertTrue(early['open_service'])
        self.assertEqual(early['spells'], 2)

    def test_ended_service_does_not_erase_cash(self):
        self.sample()
        row = run(self.data, {'cutoff': 3, 'group': '一月'})['rows'][0]
        self.assertFalse(row['open_service'])
        self.assertEqual(row['cash'], 60)

    def test_invalid_kind_fails(self):
        self.sample()
        self.data['ledger'][0]['kind'] = 'unknown'
        with self.assertRaises(ValueError):
            run(self.data, {'cutoff': 5, 'group': ''})

    def test_latest_service_state_does_not_keep_old_open_period(self):
        self.sample()
        self.data['spells'] = [dict(spell='s', account='a', start=1, end=None, status='active', known_at=1), dict(spell='s', account='a', start=1, end=3, status='ended', known_at=3)]
        early = run(self.data, {'cutoff': 2, 'group': ''})['rows'][0]
        later = run(self.data, {'cutoff': 3, 'group': ''})['rows'][0]
        self.assertTrue(early['open_service'])
        self.assertFalse(later['open_service'])
        self.assertEqual(later['spells'], 1)
