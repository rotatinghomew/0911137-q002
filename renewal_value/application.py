import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .domain import run
from .forecast import HORIZON, MIN_OBSERVATION, eligible_accounts
from .model import ValueModel
from .strategy import cohort_summary, rank_outreach, run_backtest
from .storage import AnalysisStore, read_dataset


ROOT = Path(__file__).resolve().parents[1]


class Application:
    def __init__(self, dataset, database, assets):
        self.dataset = Path(dataset)
        self.store = AnalysisStore(database)
        self.assets = Path(assets)

    def catalog(self):
        data, digest = read_dataset(self.dataset)
        return {**data['meta'], 'digest': digest, 'tables': {name: len(rows) for name, rows in data['tables'].items()}}

    def analyze(self, supplied):
        if not isinstance(supplied, dict) or set(supplied) - {'cutoff', 'group'}:
            raise ValueError('只接受 cutoff 和 group 筛选项')
        data, digest = read_dataset(self.dataset)
        cutoff = supplied.get('cutoff', data['meta']['default_cutoff'])
        try:
            if isinstance(cutoff, bool) or float(cutoff) != int(cutoff):
                raise ValueError()
            cutoff = int(cutoff)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('分析时点须为整数') from None
        group = supplied.get('group', '')
        if not isinstance(group, str) or group not in ['', *data['meta']['groups']]:
            raise ValueError('未知分析范围')
        if not data['meta']['min_cutoff'] <= cutoff <= data['meta']['max_cutoff']:
            raise ValueError('分析时点超出资料范围')
        options = {'cutoff': cutoff, 'group': group}
        result = run(data['tables'], options)
        return {'digest': digest, 'options': options, 'result': result}

    def _validated_cutoff(self, data, supplied):
        cutoff = supplied.get('cutoff', data['meta']['default_cutoff'])
        try:
            if isinstance(cutoff, bool) or float(cutoff) != int(cutoff):
                raise ValueError()
            cutoff = int(cutoff)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('分析时点须为整数') from None
        if not data['meta']['min_cutoff'] <= cutoff <= data['meta']['max_cutoff']:
            raise ValueError('分析时点超出资料范围')
        return cutoff

    def forecast(self, supplied):
        if not isinstance(supplied, dict) or set(supplied) - {'cutoff', 'group', 'contact_cost', 'capacity'}:
            raise ValueError('预测只接受 cutoff、group、contact_cost、capacity 参数')
        data, digest = read_dataset(self.dataset)
        cutoff = self._validated_cutoff(data, supplied)
        group = supplied.get('group', '')
        if not isinstance(group, str) or group not in ['', *data['meta']['groups']]:
            raise ValueError('未知分析范围')
        contact_cost = supplied.get('contact_cost', '0')
        try:
            contact_cost = float(contact_cost)
        except (TypeError, ValueError):
            raise ValueError('回访成本须为数值') from None
        if not math.isfinite(contact_cost) or contact_cost < 0:
            raise ValueError('回访成本须为非负有限数值')
        capacity = supplied.get('capacity')
        if capacity not in (None, ''):
            try:
                if float(capacity) != int(float(capacity)):
                    raise ValueError()
                capacity = int(float(capacity))
            except (TypeError, ValueError):
                raise ValueError('名单容量须为非负整数') from None
        else:
            capacity = None
        # 训练 cutoff 严格早于预测时点，保证不使用预测期及其之后的信息。
        train_cutoffs = list(range(data['meta']['min_cutoff'], cutoff))
        model, samples = ValueModel.train(data['tables'], train_cutoffs)
        features_rows, excluded = eligible_accounts(data['tables'], cutoff)
        if group:
            features_rows = [row for row in features_rows if row['cohort'] == group]
            excluded = [row for row in excluded if row['cohort'] == group]
        rows = rank_outreach(model, features_rows, contact_cost, capacity)
        cohorts = cohort_summary(rows, excluded)
        result = {
            'horizon': HORIZON,
            'min_observation': MIN_OBSERVATION,
            'training_samples': len(samples),
            'training_cutoffs': train_cutoffs,
            'rows': rows,
            'cohorts': cohorts,
            'excluded': [{'account': row['account'], 'cohort': row['cohort'], 'observation': row['observation'], 'reason': row['reason']} for row in excluded],
            'summary': {
                '可预测客户数': len(rows),
                '观察期不足客户数': len(excluded),
                '期望净回款合计': round(sum(row['expected_net_cash'] for row in rows), 4),
                '建议回访客户数': sum(1 for row in rows if row['recommend_contact']),
            },
            'notes': [
                '预测窗口为未来六个业务期；期望净回款为无回访干预基线，不含因果增益。',
                '暂停不是终止；取消、到期不续、重新激活按服务期版本与账务事件分别识别。',
                '未结清账单是应收差额，按付款滞后估计回收，不计为流失。',
                '退款在 known_at 之后才可见，只影响该时点之后的预测。',
                '回访名单按期望净回款减单次回访成本排序；现有记录无法估计回访的因果收益。',
            ],
        }
        options = {'cutoff': cutoff, 'group': group, 'contact_cost': contact_cost, 'capacity': capacity}
        return {'digest': digest, 'options': options, 'result': result}

    def backtest(self, supplied):
        if not isinstance(supplied, dict) or set(supplied) - {'start', 'end', 'group'}:
            raise ValueError('回测只接受 start、end、group 参数')
        data, digest = read_dataset(self.dataset)
        start = self._validated_cutoff(data, {'cutoff': supplied.get('start', data['meta']['min_cutoff'])})
        end = self._validated_cutoff(data, {'cutoff': supplied.get('end', data['meta']['default_cutoff'])})
        if start > end:
            raise ValueError('回测起点不能晚于终点')
        group = supplied.get('group', '')
        if not isinstance(group, str) or group not in ['', *data['meta']['groups']]:
            raise ValueError('未知分析范围')
        report = run_backtest(data['tables'], start, end)
        if group:
            fold_by_cutoff = {fold['cutoff']: fold for fold in report['folds']}
            for cutoff in report['cutoffs']:
                _, excluded = eligible_accounts(data['tables'], cutoff)
                fold_by_cutoff[cutoff]['excluded'] = sum(1 for row in excluded if row['cohort'] == group)
            for fold in report['folds']:
                fold['rows'] = [row for row in fold['rows'] if row['cohort'] == group]
                fold['evaluated'] = len(fold['rows'])
        return {'digest': digest, 'options': {'start': start, 'end': end, 'group': group}, 'result': report}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, application, **kwargs):
        self.application = application
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        return

    def send_json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        request = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(request.query, keep_blank_values=True).items()}
        try:
            if request.path == '/api/catalog':
                value = self.application.catalog()
            elif request.path == '/api/view':
                value = self.application.analyze(query)
            elif request.path == '/api/forecast':
                value = self.application.forecast(query)
            elif request.path == '/api/backtest':
                value = self.application.backtest(query)
            elif request.path == '/api/analyses':
                value = self.application.store.list()
            elif request.path.startswith('/api/analyses/'):
                value = self.application.store.get(request.path.rsplit('/', 1)[-1])
            elif request.path == '/api/source':
                data, digest = read_dataset(self.application.dataset)
                table = query.get('table', '')
                if table not in data['tables']:
                    raise KeyError('资料表不存在')
                value = {'digest': digest, 'rows': data['tables'][table]}
            elif request.path in {'/', '/app.js', '/style.css'}:
                name = 'index.html' if request.path == '/' else request.path[1:]
                payload = (self.application.assets / name).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css'}[name] + '; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            else:
                raise KeyError('路径不存在')
            self.send_json(200, value)
        except KeyError as error:
            self.send_json(404, {'error': str(error)})
        except (ValueError, TypeError) as error:
            self.send_json(400, {'error': str(error)})

    def do_POST(self):
        if urlsplit(self.path).path != '/api/analyses':
            self.send_json(404, {'error': '路径不存在'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError('请求大小不合适')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) - {'title', 'options'}:
                raise ValueError('保存请求字段无效')
            analyzed = self.application.analyze(payload.get('options', {}))
            value = self.application.store.save(payload.get('title'), analyzed['digest'], analyzed['options'], analyzed['result'])
            self.send_json(201, value)
        except (ValueError, TypeError) as error:
            self.send_json(400, {'error': str(error)})


def server(application, host='127.0.0.1', port=0):
    return ThreadingHTTPServer((host, port), partial(Handler, application=application))


def main():
    parser = argparse.ArgumentParser(description='启动本地分析工作台')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=8080, type=int)
    parser.add_argument('--data', type=Path, default=ROOT / 'fixtures/data.json')
    parser.add_argument('--state', type=Path, default=Path(os.environ.get('APP_STATE', '.state/analyses.sqlite3')))
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    application = Application(args.data, args.state, ROOT / 'web')
    httpd = server(application, args.host, 0 if args.demo else args.port)
    if args.demo:
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        base = f'http://{args.host}:{httpd.server_port}'
        try:
            with urlopen(base + '/api/view') as response:
                value = json.load(response)
            request = Request(base + '/api/analyses', data=json.dumps({'title': '当前资料摘要', 'options': value['options']}).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                saved = json.load(response)
            with urlopen(base + '/api/analyses/' + saved['id']) as response:
                restored = json.load(response)
            print(json.dumps({'当前摘要': value['result']['summary'], '资料指纹': value['digest'], '保存后相同': restored['result'] == value['result'], '说明': value['result']['notes']}, ensure_ascii=False, indent=2))
        finally:
            httpd.shutdown()
            worker.join()
            httpd.server_close()
    else:
        print(f'本地工作台：http://{args.host}:{httpd.server_port}', flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
