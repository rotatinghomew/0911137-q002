"""滚动回测与回访名单决策层。

回测在多个历史 cutoff 上训练、预测随后六期，并与实际净回款对照。
每个 cutoff 的训练样本只能使用该时点可见的信息，标签从事后数据取，
从而避免未来信息泄漏。

决策层只做一件可解释的事：把"无回访基线的期望净回款"与"单次回访成本"
比较，给出名单与净价值排序。资料里没有任何回访动作与结果的记录，
因此回访的因果增益（uplift）无法从这些数据识别，统一显式标注为
不可估计，绝不伪装成模型输出。
"""

from collections import defaultdict
import math

from .forecast import HORIZON, eligible_accounts, realized_net_cash
from .model import ValueModel, horizon_risk, project


def backtest_cutoffs(data, start, end):
    """生成满足完整六期标签窗口的训练 cutoff 序列。"""
    from .model import max_cutoff
    limit = max_cutoff(data) - HORIZON
    return [cutoff for cutoff in range(start, min(end, limit) + 1)]


def run_backtest(data, start, end):
    """滚动回测：每个 cutoff 只用此前资料训练，预测后六期并对照真值。"""
    cutoffs = backtest_cutoffs(data, start, end)
    folds = []
    for cutoff in cutoffs:
        train_cutoffs = list(range(start, cutoff + 1))
        model, samples = ValueModel.train(data, train_cutoffs)
        features_rows, excluded = eligible_accounts(data, cutoff)
        rows = []
        for features in features_rows:
            projection = project(model, features)
            actual = realized_net_cash(data, features['account'], cutoff, cutoff + HORIZON)
            error = projection['expected_net_cash'] - actual
            rows.append({
                'account': features['account'],
                'cohort': features['cohort'],
                'cutoff': cutoff,
                'state': features['state'],
                'expected_net_cash': projection['expected_net_cash'],
                'actual_net_cash': round(actual, 4),
                'error': round(error, 4),
                'abs_error': round(abs(error), 4),
                'p10': projection['p10'],
                'p90': projection['p90'],
                'covered': projection['p10'] <= actual <= projection['p90'],
            })
        folds.append({
            'cutoff': cutoff,
            'training_samples': len(samples),
            'evaluated': len(rows),
            'excluded': len(excluded),
            'rows': rows,
            'mae': round(sum(row['abs_error'] for row in rows) / len(rows), 4) if rows else None,
            'bias': round(sum(row['error'] for row in rows) / len(rows), 4) if rows else None,
            'coverage': round(sum(1 for row in rows if row['covered']) / len(rows), 4) if rows else None,
        })
    summary = summarize_backtest(folds)
    return {'cutoffs': cutoffs, 'folds': folds, 'summary': summary, 'notes': backtest_notes()}


def summarize_backtest(folds):
    usable = [fold for fold in folds if fold['mae'] is not None]
    if not usable:
        return {'fold_count': 0, 'mae': None, 'bias': None, 'coverage': None}
    weights = [fold['evaluated'] for fold in usable]
    total = sum(weights)
    return {
        'fold_count': len(usable),
        'mae': round(sum(fold['mae'] * weight for fold, weight in zip(usable, weights)) / total, 4),
        'bias': round(sum(fold['bias'] * weight for fold, weight in zip(usable, weights)) / total, 4),
        'coverage': round(sum(fold['coverage'] * weight for fold, weight in zip(usable, weights)) / total, 4),
    }


def backtest_notes():
    return [
        '回测标签为 cutoff 之后六期实际净回款，训练特征只使用 cutoff 时点可见记录。',
        '未结清账单按应收差额与付款滞后估计回收，不计为流失。',
        '退款在 known_at 之后才进入训练，不回溯影响此前时点的判断。',
        '回测衡量的是无干预基线的预测精度，不含回访动作，不能证明回访的因果收益。',
    ]


def rank_outreach(model, features_rows, contact_cost, capacity=None):
    """按策略净价值排序回访名单。

    净价值 = 期望净回款 - 单次回访成本。这是基线预测上的成本比较规则，
    不是因果效应估计：模型没有任何回访处理组/对照组的信息。
    成本改变后排序按同一预测重新计算，预测值本身不变。
    """
    if not isinstance(contact_cost, (int, float)) or isinstance(contact_cost, bool) or not math.isfinite(contact_cost) or contact_cost < 0:
        raise ValueError('回访成本须为非负有限数值')
    rows = []
    for features in features_rows:
        projection = project(model, features)
        risk = horizon_risk(model, features)
        decision_value = round(projection['expected_net_cash'] - contact_cost, 4)
        rows.append({
            'account': features['account'],
            'cohort': features['cohort'],
            'state': features['state'],
            'tenure': features['tenure'],
            'expected_net_cash': projection['expected_net_cash'],
            'p10': projection['p10'],
            'p50': projection['p50'],
            'p90': projection['p90'],
            'expected_by_step': projection['expected_by_step'],
            'churn_probability': risk['churn_probability'],
            'pause_probability': risk['pause_probability'],
            'reactivation_probability': risk['reactivation_probability'],
            'outstanding': round(features['outstanding'], 4),
            'contact_cost': round(contact_cost, 4),
            'decision_value': decision_value,
            'recommend_contact': decision_value > 0,
            'causal_uplift': None,
            'causal_note': '现有订阅与账务记录不含回访动作及结果，无法识别回访的因果增益；名单仅按基线回款与成本比较。',
            'basis': {
                'transition': projection['transition_basis'],
                'cash': projection['cash_basis'],
                'unit_price': features['unit_price'],
                'observation': features['observation'],
            },
        })
    rows.sort(key=lambda row: (-row['decision_value'], row['account']))
    if capacity is not None:
        if not isinstance(capacity, int) or capacity < 0:
            raise ValueError('名单容量须为非负整数')
        for index, row in enumerate(rows):
            row['recommend_contact'] = row['recommend_contact'] and index < capacity
    return rows


def cohort_summary(rows, excluded):
    """批次页：按获客批次汇总预测总额、风险与策略口径。"""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['cohort']].append(row)
    excluded_counts = defaultdict(int)
    for row in excluded:
        excluded_counts[row['cohort']] += 1
    summary_rows = []
    for cohort, members in sorted(grouped.items()):
        expected = sum(row['expected_net_cash'] for row in members)
        downside = sum(row['expected_net_cash'] - row['p10'] for row in members)
        churn_weighted = sum(row['churn_probability'] for row in members)
        contacted = [row for row in members if row['recommend_contact']]
        summary_rows.append({
            'cohort': cohort,
            'accounts': len(members),
            'excluded_observation': excluded_counts.get(cohort, 0),
            'expected_net_cash': round(expected, 4),
            'p10_total': round(sum(row['p10'] for row in members), 4),
            'p90_total': round(sum(row['p90'] for row in members), 4),
            'downside_risk': round(downside, 4),
            'mean_churn_probability': round(churn_weighted / len(members), 4),
            'outstanding_total': round(sum(row['outstanding'] for row in members), 4),
            'contact_count': len(contacted),
            'contact_cost_total': round(sum(row['contact_cost'] for row in contacted), 4),
            'decision_value_total': round(sum(row['decision_value'] for row in contacted), 4),
        })
    return summary_rows
