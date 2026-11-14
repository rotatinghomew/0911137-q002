import copy
import unittest

from renewal_value.application import ROOT
from renewal_value.forecast import (
    HORIZON,
    MIN_OBSERVATION,
    account_features,
    classify_end,
    current_state,
    eligible_accounts,
    event_history,
    latest_spell_versions,
    realized_net_cash,
)
from renewal_value.model import ValueModel, horizon_risk, project, training_samples
from renewal_value.strategy import cohort_summary, rank_outreach, run_backtest


def fixture_tables():
    import json
    return json.loads((ROOT / 'fixtures/data.json').read_text())['tables']


def base_data():
    return {
        'accounts': [
            {'account': 'a', 'cohort': '一月', 'acquired_at': 1},
            {'account': 'b', 'cohort': '一月', 'acquired_at': 1},
            {'account': 'c', 'cohort': '二月', 'acquired_at': 20},
        ],
        'spells': [],
        'ledger': [],
    }


def charge(account, time, amount=100, known=None):
    return {'entry': f'{account}-{time}-c', 'account': account, 'kind': 'charge', 'amount': amount, 'effective_at': time, 'known_at': known if known is not None else time}


def payment(account, time, amount=100, known=None):
    return {'entry': f'{account}-{time}-p', 'account': account, 'kind': 'payment', 'amount': amount, 'effective_at': time, 'known_at': known if known is not None else time}


def refund(account, time, amount, known):
    return {'entry': f'{account}-{time}-r', 'account': account, 'kind': 'refund', 'amount': amount, 'effective_at': time, 'known_at': known}


class EventSemanticsTests(unittest.TestCase):
    def test_pause_is_not_termination(self):
        data = base_data()
        data['spells'] = [{'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'paused', 'known_at': 4}]
        state, since, age = current_state([row for row in data['spells']], 10)
        self.assertEqual(state, 'paused')
        self.assertEqual(age, 9)
        events = event_history(data['spells'], data['ledger'], 10, 'a')
        self.assertEqual([event['event'] for event in events], ['pause'])

    def test_cancel_vs_expire_distinguished_by_charge_coverage(self):
        data = base_data()
        # a：最后收费在第 4 期，服务在第 5 期结束，账期未覆盖到结束点 -> 取消。
        cancel_spell = {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': 5, 'status': 'ended', 'known_at': 5}
        ledger_a = [charge('a', t) for t in range(1, 5)]
        self.assertEqual(classify_end(cancel_spell, ledger_a), 'cancel')
        # b：最后收费在第 5 期，覆盖到结束点 -> 到期不续。
        expire_spell = {'spell': 'b-1', 'account': 'b', 'start': 1, 'end': 5, 'status': 'ended', 'known_at': 5}
        ledger_b = [charge('b', t) for t in range(1, 6)]
        self.assertEqual(classify_end(expire_spell, ledger_b), 'expire')

    def test_reactivation_requires_a_new_spell_after_end(self):
        data = base_data()
        data['spells'] = [
            {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': 5, 'status': 'ended', 'known_at': 5},
            {'spell': 'a-2', 'account': 'a', 'start': 7, 'end': None, 'status': 'active', 'known_at': 7},
        ]
        data['ledger'] = [charge('a', t) for t in range(1, 5)]
        events = event_history(data['spells'], data['ledger'], 10, 'a')
        self.assertEqual([event['event'] for event in events], ['cancel', 'reactivation'])
        # 第 6 期时新服务期还不可见，看不到重新激活。
        early = event_history(data['spells'], data['ledger'], 6, 'a')
        self.assertEqual([event['event'] for event in early], ['cancel'])

    def test_pause_then_resume_is_reactivation_event(self):
        data = base_data()
        data['spells'] = [
            {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'active', 'known_at': 1},
            {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'paused', 'known_at': 4},
            {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'active', 'known_at': 6},
        ]
        events = event_history(data['spells'], data['ledger'], 10, 'a')
        self.assertEqual([event['event'] for event in events], ['pause', 'reactivation'])

    def test_outstanding_is_never_treated_as_churn(self):
        data = base_data()
        data['spells'] = [{'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'active', 'known_at': 1}]
        data['ledger'] = [charge('a', 1, 100), payment('a', 1, 60)]
        features = account_features(data, 10, data['accounts'][0])
        self.assertEqual(features['outstanding'], 40)
        self.assertEqual(features['state'], 'active')

    def test_refund_only_affects_judgement_after_known_at(self):
        data = base_data()
        data['spells'] = [{'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'active', 'known_at': 1}]
        data['ledger'] = [charge('a', 1, 100), payment('a', 1, 100), refund('a', 1, 30, known=8)]
        before = account_features(data, 7, data['accounts'][0])
        after = account_features(data, 8, data['accounts'][0])
        self.assertEqual(before['cash_to_date'], 100)
        self.assertEqual(before['refunds_to_date'], 0)
        self.assertEqual(after['cash_to_date'], 70)
        self.assertEqual(after['refunds_to_date'], 30)

    def test_future_spell_revision_is_hidden(self):
        data = base_data()
        data['spells'] = [
            {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'active', 'known_at': 1},
            {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': 3, 'status': 'ended', 'known_at': 3},
        ]
        at_two = latest_spell_versions(data['spells'], 2)
        at_three = latest_spell_versions(data['spells'], 3)
        self.assertEqual(current_state(at_two, 2)[0], 'active')
        self.assertEqual(current_state(at_three, 3)[0], 'ended')


class ObservationWindowTests(unittest.TestCase):
    def test_short_observation_accounts_are_excluded_not_zero_filled(self):
        data = base_data()
        data['spells'] = [
            {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'active', 'known_at': 1},
            {'spell': 'b-1', 'account': 'b', 'start': 1, 'end': None, 'status': 'active', 'known_at': 1},
            {'spell': 'c-1', 'account': 'c', 'start': 20, 'end': None, 'status': 'active', 'known_at': 20},
        ]
        rows, excluded = eligible_accounts(data, 24)
        self.assertEqual({row['account'] for row in rows}, {'a', 'b'})
        self.assertEqual(excluded[0]['account'], 'c')
        self.assertIn(str(MIN_OBSERVATION), excluded[0]['reason'])

    def test_accounts_acquired_after_cutoff_are_absent(self):
        data = base_data()
        rows, excluded = eligible_accounts(data, 10)
        accounts = {row['account'] for row in rows} | {row['account'] for row in excluded}
        self.assertNotIn('c', accounts)


class LabelTests(unittest.TestCase):
    def test_realized_label_uses_effective_window_not_visibility(self):
        data = base_data()
        data['ledger'] = [
            charge('a', 10), payment('a', 10, 100, known=12),
            charge('a', 11), payment('a', 11, 50),
            refund('a', 11, 20, known=13),
        ]
        # 标签在事后评估时使用，区间 (9, 12] 的净回款 = 100 + 50 - 20。
        self.assertEqual(realized_net_cash(data, 'a', 9, 12), 130)

    def test_training_samples_have_complete_six_period_labels(self):
        tables = fixture_tables()
        samples = training_samples(tables, [10])
        self.assertTrue(samples)
        for sample in samples:
            self.assertEqual(len(sample['labels']), HORIZON)
            # 特征 cutoff 早于任何标签时点。
            self.assertTrue(all(label['step'] >= 1 for label in sample['labels']))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tables = fixture_tables()
        self.model, _ = ValueModel.train(self.tables, list(range(0, 18)))

    def test_refund_rate_respects_known_at(self):
        # 构造一笔在 t=12 才获知的退款：t=10 的训练样本退款率必须为零。
        data = base_data()
        data['accounts'] = [
            {'account': 'a', 'cohort': '一月', 'acquired_at': 1},
            {'account': 'b', 'cohort': '一月', 'acquired_at': 1},
        ]
        data['spells'] = [
            {'spell': 'a-1', 'account': 'a', 'start': 1, 'end': None, 'status': 'active', 'known_at': 1},
            {'spell': 'b-1', 'account': 'b', 'start': 1, 'end': None, 'status': 'active', 'known_at': 1},
        ]
        data['ledger'] = []
        for account in ('a', 'b'):
            for time in range(1, 25):
                data['ledger'].append(charge(account, time, 100))
                data['ledger'].append(payment(account, time, 100))
        data['ledger'].append(refund('a', 8, 50, known=12))
        early, _ = ValueModel.train(data, [10])
        late, _ = ValueModel.train(data, [14])
        self.assertEqual(early.cash.refund_rate('一月'), 0.0)
        self.assertGreater(late.cash.refund_rate('一月'), 0.0)

    def test_transition_rows_are_probabilities(self):
        for cohort in ('渠道1', '渠道2', '渠道3'):
            for origin in ('active', 'paused', 'ended'):
                row = self.model.transitions.row(cohort, origin)
                self.assertAlmostEqual(sum(row.values()), 1.0)
                self.assertTrue(all(0 <= rate <= 1 for rate in row.values()))
                # 收缩后不允许出现绝对的 0/1 确定性。
                self.assertTrue(all(0 < rate < 1 for rate in row.values()), (cohort, origin, row))

    def test_projection_is_deterministic_and_finite(self):
        features, excluded = eligible_accounts(self.tables, 18)
        first = project(self.model, features[0])
        second = project(self.model, features[0])
        self.assertEqual(first, second)
        for key in ('expected_net_cash', 'p10', 'p50', 'p90'):
            self.assertTrue(isinstance(first[key], (int, float)))
        self.assertLessEqual(first['p10'], first['p50'])
        self.assertLessEqual(first['p50'], first['p90'])
        self.assertEqual(len(first['expected_by_step']), HORIZON)

    def test_ended_account_risk_uses_reactivation_probability(self):
        features = next(row for row in eligible_accounts(self.tables, 18)[0] if row['state'] == 'ended')
        risk = horizon_risk(self.model, features)
        self.assertEqual(risk['churn_probability'], 0.0)
        self.assertGreater(risk['reactivation_probability'], 0.0)

    def test_active_account_has_positive_churn_and_bounded_probability(self):
        features = next(row for row in eligible_accounts(self.tables, 18)[0] if row['state'] == 'active')
        risk = horizon_risk(self.model, features)
        self.assertGreater(risk['churn_probability'], 0.0)
        self.assertLessEqual(risk['churn_probability'], 1.0)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tables = fixture_tables()
        self.model, _ = ValueModel.train(self.tables, list(range(0, 24)))
        self.features, self.excluded = eligible_accounts(self.tables, 24)

    def test_ranking_uses_prediction_minus_cost_and_cost_changes_order(self):
        cheap = rank_outreach(self.model, self.features, 10)
        expensive = rank_outreach(self.model, self.features, 300)
        # 预测值不随成本改变。
        expectations = {row['account']: row['expected_net_cash'] for row in cheap}
        for row in expensive:
            self.assertEqual(expectations[row['account']], row['expected_net_cash'])
        # 成本升高后建议回访的客户减少。
        self.assertGreater(sum(row['recommend_contact'] for row in cheap),
                           sum(row['recommend_contact'] for row in expensive))
        self.assertTrue(all(row['decision_value'] > 0 for row in cheap if row['recommend_contact']))

    def test_causal_uplift_is_explicitly_unidentifiable(self):
        rows = rank_outreach(self.model, self.features, 50)
        self.assertTrue(all(row['causal_uplift'] is None for row in rows))
        self.assertTrue(all('因果' in row['causal_note'] for row in rows))

    def test_capacity_limits_contact_list(self):
        rows = rank_outreach(self.model, self.features, 0, capacity=7)
        self.assertEqual(sum(row['recommend_contact'] for row in rows), 7)

    def test_invalid_cost_rejected(self):
        with self.assertRaises(ValueError):
            rank_outreach(self.model, self.features, -1)

    def test_cohort_summary_totals_match_account_rows(self):
        rows = rank_outreach(self.model, self.features, 40)
        summary = cohort_summary(rows, self.excluded)
        self.assertEqual(sum(row['accounts'] for row in summary), len(rows))
        self.assertAlmostEqual(
            sum(row['expected_net_cash'] for row in summary),
            sum(row['expected_net_cash'] for row in rows),
        )


class BacktestTests(unittest.TestCase):
    def test_walk_forward_backtest_uses_only_past_information(self):
        tables = fixture_tables()
        report = run_backtest(tables, 8, 14)
        self.assertEqual(report['cutoffs'], [8, 9, 10, 11, 12, 13, 14])
        for fold in report['folds']:
            self.assertIsNotNone(fold['mae'])
            for row in fold['rows']:
                # 真值标签必须来自 cutoff 之后的窗口。
                self.assertIn('actual_net_cash', row)
        self.assertIsNotNone(report['summary']['mae'])
        self.assertTrue(0 <= report['summary']['coverage'] <= 1)
        self.assertTrue(any('因果' in note for note in report['notes']))

    def test_backtest_is_deterministic(self):
        tables = fixture_tables()
        first = run_backtest(tables, 10, 14)
        second = run_backtest(tables, 10, 14)
        self.assertEqual(first, second)

    def test_fixture_not_mutated(self):
        tables = fixture_tables()
        original = copy.deepcopy(tables)
        run_backtest(tables, 10, 14)
        self.assertEqual(tables, original)


if __name__ == '__main__':
    unittest.main()
