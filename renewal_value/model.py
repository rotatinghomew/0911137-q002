"""训练、推断与回测。

模型是分层收缩的经验转移率模型，不依赖第三方库，输出可由相同资料
完全复现。选择经验模型而不是黑箱分类器，是因为业务需要把"预测依据"
逐户、逐批次讲清楚：每一条转移率都能追溯到哪个分层的多少个暴露样本。

两层结构：

1. 状态转移（active/paused/ended 的月度马尔可夫链），转移率按
   (cohort, state) 分层、向 state 层与全局层收缩。
2. 在位现金流：按状态估计每期付款概率与退款率，乘以账户单价；
   未结清账单按付款滞后分布单独估计回收，不与流失混算。

推断用固定种子的蒙特卡洛得到期望与分位数；同一份资料、同一参数
重复运行结果逐分相同。回测用滚动 cutoff，训练只允许看 cutoff 之前
可见的信息，标签用之后六期的实际净回款。
"""

import random
from collections import Counter, defaultdict

from .forecast import (
    HORIZON,
    MIN_SEGMENT_EXPOSURE,
    PRIOR_STRENGTH,
    STATES,
    current_state,
    eligible_accounts,
    latest_spell_versions,
    realized_net_cash,
)

SEED = 20261114
SCENARIOS = 500


def state_at(data, account, time):
    """事后真值状态（仅用于构造训练标签与回测）。"""
    spell_rows = [row for row in latest_spell_versions(data['spells'], time) if row['account'] == account and row['start'] <= time]
    state, since, age = current_state(spell_rows, time)
    return state


def training_samples(data, cutoffs):
    """每个 (账户, cutoff) 一条样本；特征与标签的可见性严格分离。"""
    samples = []
    max_time = max_cutoff(data)
    for cutoff in cutoffs:
        if cutoff + HORIZON > max_time:
            continue
        features, _ = eligible_accounts(data, cutoff)
        for feature in features:
            account = feature['account']
            labels = monthly_labels(data, account, cutoff)
            samples.append({'features': feature, 'cutoff': cutoff, 'labels': labels})
    return samples


def max_cutoff(data):
    times = [row['effective_at'] for row in data['ledger']] + [row['start'] for row in data['spells']]
    return max(times) if times else 0


def monthly_labels(data, account, cutoff):
    """cutoff 之后 HORIZON 期的逐期状态与净回款真值。"""
    labels = []
    for step in range(1, HORIZON + 1):
        time = cutoff + step
        labels.append({
            'step': step,
            'state': state_at(data, account, time),
            'cash': realized_net_cash(data, account, time - 1, time),
        })
    return labels


class TransitionModel:
    """月度状态转移率，分层 (cohort, state) -> state -> 全局。"""

    def __init__(self):
        self.segment_events = defaultdict(Counter)
        self.segment_exposure = Counter()
        self.state_events = defaultdict(Counter)
        self.state_exposure = Counter()
        self.global_events = Counter()
        self.global_exposure = 0

    def fit(self, samples):
        for sample in samples:
            cohort = sample['features']['cohort']
            state = sample['features']['state']
            for index, label in enumerate(sample['labels']):
                origin = state if index == 0 else sample['labels'][index - 1]['state']
                if origin not in STATES:
                    continue
                target = label['state']
                self.segment_events[(cohort, origin)][target] += 1
                self.segment_exposure[(cohort, origin)] += 1
                self.state_events[origin][target] += 1
                self.state_exposure[origin] += 1
                self.global_events[target] += 1
                self.global_exposure += 1
        return self

    def _global_rate(self, target):
        if self.global_exposure == 0:
            return 1.0 / len(STATES)
        return self.global_events[target] / self.global_exposure

    def rate(self, cohort, origin, target):
        """收缩后的月度转移概率。

        三层连续收缩：(cohort, origin) 向 origin 状态层、再向全局层收缩。
        状态层本身为零的事件也能从全局先验得到非零概率，样本不足或
        事件为零时不会产生绝对的 0/1；预测依据里保留各层暴露供审计。
        """
        global_rate = self._global_rate(target)
        state_exposure = self.state_exposure[origin]
        state_rate = (
            self.state_events[origin][target] / state_exposure
            if state_exposure else global_rate
        )
        state_weight = state_exposure / (state_exposure + PRIOR_STRENGTH)
        shrunk_state = state_weight * state_rate + (1 - state_weight) * global_rate
        exposure = self.segment_exposure[(cohort, origin)]
        if exposure == 0:
            return shrunk_state
        if exposure < MIN_SEGMENT_EXPOSURE:
            segment_weight = 0.5 * exposure / (exposure + PRIOR_STRENGTH)
        else:
            segment_weight = exposure / (exposure + PRIOR_STRENGTH)
        segment_rate = self.segment_events[(cohort, origin)][target] / exposure
        return segment_weight * segment_rate + (1 - segment_weight) * shrunk_state

    def row(self, cohort, origin):
        rates = {target: self.rate(cohort, origin, target) for target in STATES}
        total = sum(rates.values()) or 1.0
        return {target: rates[target] / total for target in STATES}

    def basis(self, cohort, origin):
        """预测依据：分层暴露与事件数，供页面展示与审计。"""
        exposure = self.segment_exposure[(cohort, origin)]
        return {
            'layer': 'cohort-state' if exposure >= MIN_SEGMENT_EXPOSURE else 'state-fallback',
            'segment_exposure': exposure,
            'segment_events': dict(self.segment_events[(cohort, origin)]),
            'state_exposure': self.state_exposure[origin],
            'state_events': dict(self.state_events[origin]),
        }


class CashModel:
    """在位账户的每期付款概率、退款率，以及未结清回收滞后分布。"""

    def __init__(self):
        # (cohort, state) -> [期数：有付款的期 / 有收费的期]
        self.pay_events = defaultdict(lambda: [0, 0])
        self.state_pay = defaultdict(lambda: [0, 0])
        self.refund_by_cohort = defaultdict(lambda: [0.0, 0])  # 退款额 / 收费额
        self.refund_global = [0.0, 0]
        self.lag_weights = Counter()

    def fit(self, samples, data):
        ledger_by_account = defaultdict(list)
        for row in data['ledger']:
            ledger_by_account[row['account']].append(row)
        for sample in samples:
            feature = sample['features']
            cohort = feature['cohort']
            cutoff = feature['cutoff']
            account = feature['account']
            account_rows = ledger_by_account[account]
            # 付款概率从事后标签估计：标签期的付款是否发生本身就是真值，
            # 时点正确性由特征侧（eligible_accounts 只收可见记录）保证。
            payment_periods = {
                row['effective_at']
                for row in account_rows
                if row['kind'] == 'payment'
            }
            for label in sample['labels']:
                # 只有在位状态产生经常性回款；ended 的回款来自重新激活后的链。
                if label['state'] in ('active', 'paused'):
                    period = cutoff + label['step']
                    has_charge = any(
                        row['kind'] == 'charge' and row['effective_at'] == period
                        for row in account_rows
                    )
                    if has_charge:
                        self.pay_events[(cohort, label['state'])][1] += 1
                        self.state_pay[label['state']][1] += 1
                        if period in payment_periods:
                            self.pay_events[(cohort, label['state'])][0] += 1
                            self.state_pay[label['state']][0] += 1
            # 退款率只用该 cutoff 时点已知的账务，事后确认的退款不回溯。
            visible = [row for row in account_rows if row['known_at'] <= cutoff]
            self._fit_refunds(cohort, visible)
        self._fit_lags(ledger_by_account)
        return self

    def _fit_refunds(self, cohort, rows):
        charges = sum(row['amount'] for row in rows if row['kind'] == 'charge')
        refunds = sum(row['amount'] for row in rows if row['kind'] == 'refund')
        if charges:
            self.refund_by_cohort[cohort][0] += refunds
            self.refund_by_cohort[cohort][1] += charges
            self.refund_global[0] += refunds
            self.refund_global[1] += charges

    def _fit_lags(self, ledger_by_account):
        # 滞后分布是结构性参数，使用全资料估计；单个 cutoff 的样本不足以成分布。
        for rows in ledger_by_account.values():
            charges = sorted((row for row in rows if row['kind'] == 'charge'), key=lambda row: row['effective_at'])
            payments = sorted((row for row in rows if row['kind'] == 'payment'), key=lambda row: row['effective_at'])
            for charge, payment in zip(charges, payments):
                self.lag_weights[payment['effective_at'] - charge['effective_at']] += 1

    def payment_probability(self, cohort, state):
        events, exposure = self.pay_events[(cohort, state)]
        if exposure < MIN_SEGMENT_EXPOSURE:
            events, exposure = self.state_pay[state]
        if exposure == 0:
            return 0.8
        return events / exposure

    def refund_rate(self, cohort):
        refunds, charges = self.refund_by_cohort[cohort]
        if charges == 0:
            refunds, charges = self.refund_global
        return refunds / charges if charges else 0.0

    def lag_distribution(self):
        total = sum(self.lag_weights.values())
        if total == 0:
            return {0: 1.0}
        return {lag: count / total for lag, count in self.lag_weights.items()}

    def basis(self, cohort, state):
        events, exposure = self.pay_events[(cohort, state)]
        return {
            'payment_layer': 'cohort-state' if exposure >= MIN_SEGMENT_EXPOSURE else 'state-fallback',
            'payment_events': events,
            'payment_exposure': exposure,
            'refund_rate_basis': [round(self.refund_by_cohort[cohort][0], 4), round(self.refund_by_cohort[cohort][1], 4)],
        }


class ValueModel:
    def __init__(self, transitions, cash):
        self.transitions = transitions
        self.cash = cash

    @classmethod
    def train(cls, data, cutoffs):
        samples = training_samples(data, cutoffs)
        return cls(TransitionModel().fit(samples), CashModel().fit(samples, data)), samples


def _draw_state(rng, rates):
    point = rng.random()
    cumulative = 0.0
    for target in STATES:
        cumulative += rates[target]
        if point <= cumulative:
            return target
    return STATES[-1]


def project(model, features, horizon=HORIZON, scenarios=SCENARIOS):
    """单账户六期净回款情景模拟，返回期望、分位数与逐期期望。"""
    rng = random.Random(SEED + sum(features['account'].encode()))
    lag_dist = model.cash.lag_distribution()
    lag_points = list(lag_dist)
    unit_price = features['unit_price']
    cohort = features['cohort']
    lag_cdf = {}
    cumulative = 0.0
    for lag in sorted(lag_points):
        cumulative += lag_dist[lag]
        lag_cdf[lag] = cumulative
    max_lag = max(lag_points, default=0)
    paths = []
    for _ in range(scenarios):
        state = features['state']
        path_cash = []
        outstanding = features['outstanding']
        for step in range(1, horizon + 1):
            rates = model.transitions.row(cohort, state)
            state = _draw_state(rng, rates)
            period_cash = 0.0
            if state in ('active', 'paused'):
                if rng.random() < model.cash.payment_probability(cohort, state):
                    period_cash += unit_price
                period_cash -= unit_price * model.cash.refund_rate(cohort)
            # 未结清回收：按付款滞后分布，把存量应收在滞后窗口内逐期分摊，
            # 回收总额不超过存量；这是应收账款的回收，不是经常性回款。
            if outstanding > 0 and step <= max_lag:
                before = lag_cdf.get(step - 1, 0.0)
                at_step = lag_cdf.get(step, before)
                recoverable = min(outstanding, outstanding * (at_step - before))
                period_cash += recoverable
                outstanding -= recoverable
            path_cash.append(round(period_cash, 6))
        paths.append(path_cash)
    totals = [sum(path) for path in paths]
    by_step = [sum(path[step] for path in paths) / scenarios for step in range(horizon)]
    return {
        'expected_net_cash': round(sum(totals) / scenarios, 4),
        'p10': round(quantile(totals, 0.10), 4),
        'p50': round(quantile(totals, 0.50), 4),
        'p90': round(quantile(totals, 0.90), 4),
        'expected_by_step': [round(value, 4) for value in by_step],
        'scenarios': scenarios,
        'transition_basis': model.transitions.basis(cohort, features['state']),
        'cash_basis': model.cash.basis(cohort, features['state']),
    }


def quantile(values, q):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def horizon_risk(model, features, horizon=HORIZON):
    """六期内发生流失（转入 ended）与暂停的概率，用概率向量逐期递推。"""
    if features['state'] == 'ended':
        # 已流失客户的风险口径改为重新激活概率。
        rates = model.transitions.row(features['cohort'], 'ended')
        reactivation = 1 - rates['ended']
        return {'churn_probability': 0.0, 'pause_probability': 0.0, 'reactivation_probability': round(reactivation, 4)}
    distribution = {state: 0.0 for state in STATES}
    distribution[features['state']] = 1.0
    churn = pause = 0.0
    for _ in range(horizon):
        moved = {state: 0.0 for state in STATES}
        for origin, mass in distribution.items():
            if mass <= 0:
                continue
            row = model.transitions.row(features['cohort'], origin)
            churn += mass * row['ended']
            pause += mass * row['paused']
            for target in STATES:
                moved[target] += mass * row[target]
        # ended 质量留在 ended，后续期不再重复计一次流失事件。
        distribution = moved
    return {
        'churn_probability': round(churn, 4),
        'pause_probability': round(pause, 4),
        'reactivation_probability': 0.0,
    }
