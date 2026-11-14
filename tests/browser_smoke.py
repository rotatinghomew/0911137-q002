import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as directory:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'renewal_value', '--port', str(port), '--state', str(Path(directory) / 'state.sqlite3')], cwd=ROOT)
        base = f'http://127.0.0.1:{port}'
        try:
            for attempt in range(100):
                try:
                    with urlopen(base + '/api/catalog', timeout=1) as response:
                        catalog = json.load(response)
                    break
                except OSError:
                    time.sleep(.05)
            else:
                raise RuntimeError('服务启动超时')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1280, 'height': 720})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base)
                page.wait_for_function("document.querySelector('#status').textContent === '分析已更新'")
                assert page.locator('#results tbody tr').count() > 0
                page.locator('#filters select').select_option(catalog['groups'][0])
                page.get_by_role('button', name='更新分析', exact=True).click()
                page.wait_for_function("document.querySelector('#status').textContent === '分析已更新'")
                page.locator('#save input').fill('浏览器业务核对')
                page.get_by_role('button', name='保存当前分析').click()
                page.wait_for_selector('#history button')
                page.reload()
                page.wait_for_selector('#history button')
                page.locator('#history button').click()
                page.wait_for_function("document.querySelector('#status').textContent.includes('浏览器业务核对')")
                assert page.locator('#filters select').input_value() == catalog['groups'][0]
                page.get_by_role('button', name='查看资料').click()
                page.wait_for_selector('#raw tbody tr')
                # 未来半年预测：客户页有行，成本变化改变建议名单但预测值不变。
                page.get_by_role('button', name='运行预测').click()
                page.wait_for_function("document.querySelector('#status').textContent.includes('预测已更新')")
                page.wait_for_selector('#forecast-accounts tbody tr')
                cheap_count = page.locator('#forecast-accounts tbody tr').count()
                cheap_first = page.locator('#forecast-accounts tbody tr td').first.inner_text()
                page.locator('#forecast-filters input[name="contact_cost"]').fill('500')
                page.get_by_role('button', name='运行预测').click()
                page.wait_for_function("document.querySelector('#status').textContent.includes('预测已更新')")
                assert page.locator('#forecast-accounts tbody tr').count() == cheap_count
                assert page.locator('#forecast-accounts tbody tr td').first.inner_text() == cheap_first
                # 批次页切换后显示批次汇总，总额与客户页同源。
                page.get_by_role('tab', name='获客批次页').click()
                page.wait_for_selector('#forecast-cohorts tbody tr')
                assert page.locator('#forecast-cohorts tbody tr').count() == len(catalog['groups'])
                # 因果增益必须显式标注为不可估计。
                assert '无法估计' in page.locator('#causal-banner').inner_text()
                # 回测。
                page.get_by_role('button', name='运行回测').click()
                page.wait_for_selector('#backtest-folds tbody tr')
                assert page.locator('#backtest-folds tbody tr').count() > 0
                assert not errors, errors
                print(json.dumps({'结果': '通过', '页面错误': errors, '保存范围': page.locator('#filters select').input_value(), '预测客户行': cheap_count}, ensure_ascii=False))
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    main()
