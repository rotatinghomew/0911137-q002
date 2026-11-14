"""面向未来半年的净回款预测与回测口径。

只用标准库、确定性计算。所有时点判断都基于业务时标与 known_at：
一条记录在 cutoff 时点只有在 known_at <= cutoff 时才可见，
effective_at 只决定账务归属期，不决定可见性。

事件语义（与 domain.run 的历史口径保持同一套可见性规则）：

- 暂停 pause：服务期最新版本 status=paused 且 end 为空，服务并未终止。
- 取消 cancel：服务期提前结束，结束点之后仍有本账期未消耗的收费。
- 到期不续 expire：服务期在自然续费边界结束，最后一次收费恰好覆盖到结束点。
- 重新激活 reactivation：结束的服务期之后出现新的服务期（含暂停后恢复）。

未结清账单是应收与已收的差额，本身不构成流失事件；
退款在 known_at 之前不可见，只能影响该时点之后的预测。
"""

import math
from collections import Counter, defaultdict
from statistics import median

HORIZON = 6
# 进入训练/评分所需的最短观察期（业务时标）。
MIN_OBSERVATION = 6
# 一个分层至少需要的暴露样本，不足则回退到更粗的分层。
MIN_SEGMENT_EXPOSURE = 5
# 收缩时的等效先验样本量。
PRIOR_STRENGTH = 2.0

LEDGER_KINDS = {'charge', 'payment', 'refund'}
STATES = ('active', 'paused', 'ended')


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('数值必须为有限实数')
    return value


def visible_rows(rows, cutoff):
    """known_at 决定可见性；与 domain.run 一致。"""
    return [row for row in rows if row['known_at'] <= cutoff]


def latest_spell_versions(spells, cutoff):
    """每个 spell 在 cutoff 时点可见的最新版本，旧版本不能与新版本同时计入。"""
    chosen = {}
    for row in visible_rows(spells, cutoff):
        previous = chosen.get(row['spell'])
        if previous is None or row['known_at'] > previous['known_at']:
            chosen[row['spell']] = row
    return [row for row in chosen.values() if row['start'] <= cutoff]


def account_spells(spells, cutoff, account):
    rows = [row for row in latest_spell_versions(spells, cutoff) if row['account'] == account]
    return sorted(rows, key=lambda row: (row['start'], row['spell']))


def current_state(spell_rows, cutoff):
    """返回 (状态, 当前服务期开始时点, 状态已持续期数)。

    没有任何服务期时状态为 ended；open_service 与历史摘要口径一致。
    end == cutoff 的结束版本在该时点已经生效，按 ended 处理。
    """
    opens = [row for row in spell_rows if row['end'] is None or row['end'] > cutoff]
    if opens:
        current = max(opens, key=lambda row: row['start'])
        status = current['status'] if current['status'] in ('active', 'paused') else 'active'
        return status, current['start'], cutoff - current['start']
    if spell_rows:
        last = max(spell_rows, key=lambda row: (row['end'] if row['end'] is not None else row['start'], row['start']))
        end = last['end'] if last['end'] is not None else cutoff
        return 'ended', end, cutoff - end
    return 'ended', cutoff, 0


def classify_end(spell, ledger_rows):
    """区分取消与到期不续。

    账期按账户收费间隔推断（本资料为每期收费）。结束点之后若仍有
    已归属的收费期，说明服务在账期中途终止，记为取消；最后一次收费
    恰好覆盖到结束点，记为到期不续。没有任何收费记录时无法判定，
    返回 unknown 交给上层按缺失处理，绝不臆测。
    """
    if spell['end'] is None:
        return None
    charge_times = sorted(row['effective_at'] for row in ledger_rows if row['kind'] == 'charge')
    if not charge_times:
        return 'unknown'
    last_charge = max(time for time in charge_times if time <= spell['end'])
    return 'expire' if last_charge >= spell['end'] else 'cancel'


def event_history(spells, ledger, cutoff, account):
    """在 cutoff 时点可知的、按服务期排列的事件序列。

    每个服务期先扫描其可见版本链：进入 paused 记暂停，paused 后出现
    active 新版本记重新激活（暂停恢复）。版本链的最终状态若已结束，
    再按账务覆盖区分为取消或到期不续；结束之后出现新服务期，另记一次
    跨服务期重新激活。旧版本与新版本不会被同时当作两条服务期。
    """
    timeline = account_spells(spells, cutoff, account)
    ledger_rows = [row for row in visible_rows(ledger, cutoff) if row['account'] == account]
    events = []
    for index, spell in enumerate(timeline):
        revisions = sorted(
            (row for row in visible_rows(spells, cutoff) if row['spell'] == spell['spell']),
            key=lambda row: row['known_at'],
        )
        saw_pause = False
        for revision in revisions:
            if revision['status'] == 'paused' and revision['end'] is None:
                events.append({'event': 'pause', 'at': revision['known_at'], 'spell': spell['spell']})
                saw_pause = True
            elif saw_pause and revision['status'] == 'active':
                events.append({'event': 'reactivation', 'at': revision['known_at'], 'spell': spell['spell']})
                saw_pause = False
        if spell['end'] is not None:
            events.append({'event': classify_end(spell, ledger_rows), 'at': spell['end'], 'spell': spell['spell']})
            next_row = timeline[index + 1] if index + 1 < len(timeline) else None
            if next_row is not None:
                events.append({'event': 'reactivation', 'at': next_row['start'], 'spell': next_row['spell']})
    events.sort(key=lambda row: (row['at'], row['spell']))
    return events


def charge_interval(ledger_rows):
    """推断账户的收费间隔；少于两次收费时返回 None。"""
    times = sorted({row['effective_at'] for row in ledger_rows if row['kind'] == 'charge'})
    if len(times) < 2:
        return None
    gaps = [b - a for a, b in zip(times, times[1:])]
    return median(gaps)


def account_features(data, cutoff, account_row):
    """构造 cutoff 时点的账户特征，所有输入均为当时可见信息。"""
    account = account_row['account']
    spells = data['spells']
    ledger = data['ledger']
    ledger_rows = [row for row in visible_rows(ledger, cutoff) if row['account'] == account]
    for row in ledger_rows:
        if row['kind'] not in LEDGER_KINDS or number(row['amount']) < 0:
            raise ValueError('账务类型或金额无效')
    totals = Counter()
    by_effective = defaultdict(Counter)
    for row in ledger_rows:
        totals[row['kind']] += row['amount']
        by_effective[row['effective_at']][row['kind']] += row['amount']
    spell_rows = account_spells(spells, cutoff, account)
    state, state_since, state_age = current_state(spell_rows, cutoff)
    events = event_history(spells, ledger, cutoff, account)
    event_counts = Counter(row['event'] for row in events)
    interval = charge_interval(ledger_rows)
    prices = [row['amount'] for row in ledger_rows if row['kind'] == 'charge']
    unit_price = median(prices) if prices else 0.0
    # 付款滞后：以已配对的收费-付款时点差估计，无配对时为 None。
    charges = sorted((row for row in ledger_rows if row['kind'] == 'charge'), key=lambda row: row['effective_at'])
    payments = sorted((row for row in ledger_rows if row['kind'] == 'payment'), key=lambda row: row['effective_at'])
    lags = [p['effective_at'] - c['effective_at'] for c, p in zip(charges, payments)]
    pay_lag = median(lags) if lags else None
    # 最近 HORIZON 期的实际净回款，作为短期水平参考。
    recent_cash = sum(
        rows['payment'] - rows['refund']
        for time, rows in by_effective.items()
        if cutoff - HORIZON < time <= cutoff
    )
    tenure = cutoff - account_row['acquired_at']
    return {
        'account': account,
        'cohort': account_row['cohort'],
        'cutoff': cutoff,
        'tenure': tenure,
        'state': state,
        'state_age': state_age,
        'state_since': state_since,
        'spells': len(spell_rows),
        'events': dict(event_counts),
        'reactivations': event_counts.get('reactivation', 0),
        'cancellations': event_counts.get('cancel', 0),
        'expiries': event_counts.get('expire', 0),
        'pauses': event_counts.get('pause', 0),
        'cash_to_date': totals['payment'] - totals['refund'],
        'charges_to_date': totals['charge'],
        'outstanding': totals['charge'] - totals['payment'],
        'refunds_to_date': totals['refund'],
        'unit_price': unit_price,
        'charge_interval': interval,
        'payment_lag': pay_lag,
        'recent_cash': recent_cash,
        'observation': tenure,
    }


def eligible_accounts(data, cutoff):
    """观察期不足的账户单独标出，不进入训练也不输出点预测。"""
    rows = []
    excluded = []
    for account_row in data['accounts']:
        if account_row['acquired_at'] > cutoff:
            continue
        features = account_features(data, cutoff, account_row)
        if features['observation'] < MIN_OBSERVATION:
            excluded.append({**features, 'reason': f'观察期不足{MIN_OBSERVATION}期'})
        else:
            rows.append(features)
    return rows, excluded


def realized_net_cash(data, account, start, end):
    """(start, end] 区间内实际归属的净回款；只统计区间内生效的账务。

    回测的真实标签。known_at 不限制标签本身——标签在事后评估时使用，
    训练特征仍然严格按各自 cutoff 的可见性构造。
    """
    totals = Counter()
    for row in data['ledger']:
        if row['account'] != account:
            continue
        if start < row['effective_at'] <= end:
            totals[row['kind']] += number(row['amount'])
    return totals['payment'] - totals['refund']
