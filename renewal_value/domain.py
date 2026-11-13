from collections import Counter, defaultdict
import math
from statistics import mean, median


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('数值必须为有限实数')
    return value


def selected(value, group):
    return not group or value == group


def latest(rows, key, revision, visible, cutoff):
    result = {}
    for row in rows:
        if row[visible] > cutoff:
            continue
        previous = result.get(row[key])
        if previous is None or row[revision] > previous[revision]:
            result[row[key]] = row
    return list(result.values())


def output(rows, summary, notes):
    return {'rows': rows, 'summary': summary, 'notes': notes}


def run(data, options):
    cutoff, group = options['cutoff'], options['group']
    rows = []
    for account in data['accounts']:
        if account['acquired_at'] > cutoff or not selected(account['cohort'], group):
            continue
        entries = [row for row in data['ledger'] if row['account'] == account['account'] and row['known_at'] <= cutoff and row['effective_at'] <= cutoff]
        totals = Counter()
        for entry in entries:
            if entry['kind'] not in {'charge', 'payment', 'refund'} or number(entry['amount']) < 0:
                raise ValueError('账务类型或金额无效')
            totals[entry['kind']] += entry['amount']
        spells = [row for row in data['spells'] if row['account'] == account['account'] and row['known_at'] <= cutoff and row['start'] <= cutoff]
        active = any(row['end'] is None or row['end'] > cutoff for row in spells)
        rows.append(dict(account=account['account'], cohort=account['cohort'], cash=totals['payment'] - totals['refund'], charges=totals['charge'], payments=totals['payment'], refunds=totals['refund'], outstanding=totals['charge'] - totals['payment'], spells=len(spells), open_service=active))
    rows.sort(key=lambda row: (-row['cash'], row['account']))
    return output(rows, {'客户数': len(rows), '净回款': sum(row['cash'] for row in rows), '开放服务客户': sum(row['open_service'] for row in rows)}, ['未结清金额只是账务差额，不能单独判定流失。'])
