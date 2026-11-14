# 订阅客户经营台

用于查阅订阅客户经营的业务记录、当前摘要、未来半年净回款预测与保存的分析版本。现有功能包括业务范围筛选、历史时点查阅、原始资料展开、滚动回测、按回访成本排序的名单以及不可变分析结果回看。统计口径见 `docs/data-contract.md`，HTTP 结构见 `docs/http-api.md`。

## 价值分析与回访名单

页面的"未来半年净回款预测"基于订阅历程与账务事件，区分暂停、取消、到期不续与重新激活，未结清账单按付款滞后估计回收而不计为流失；退款在 `known_at` 之后才进入判断。预测输出客户层面的期望净回款与 P10/P50/P90，并可回到获客批次查看总额与下行风险。

回访名单按"期望净回款减单次回访成本"排序，修改成本或名单容量后预测值不变、仅重新排序。现有记录不含任何回访动作与结果，回访的因果增益无法从资料识别，页面与接口将其显式标注为不可估计，与无干预基线回款分开表达。观察期不足六期的账户不输出点预测，单独列示。模型与回测口径见 `docs/data-contract.md`。

## 运行

需要 Python >=3.11,<3.12，不依赖特定补丁版本。应用运行只使用标准库，浏览器 JavaScript 无构建步骤。以下命令在项目根目录执行：

```sh
python3 -m renewal_value
```

浏览器打开本机 8080 端口即可操作。资料默认来自 `fixtures/data.json`；分析结果默认保存在 `.state/analyses.sqlite3`。可用 `--data` 指定同口径资料，`--state` 指定状态文件，`--port` 调整监听端口。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试涵盖领域口径、真实 HTTP 往返、错误筛选、跨进程存储及不可变保存。页面直接消费与测试相同的 API，不使用预录结果。

浏览器验证工具与产品运行依赖分开锁定；准备一次后可重复执行筛选、保存、刷新回看和资料查阅：

```sh
python3 -m pip install -r requirements-test.txt
python3 -m playwright install --with-deps chromium
python3 tests/browser_smoke.py
```

前两条是首次测试环境安装，完成后无需重复执行。基础容器只运行产品和标准库测试；浏览器测试在安装了 `requirements-test.txt` 与 Chromium 的开发环境运行。

也可使用无第三方 JavaScript 依赖的 npm 工作区入口：`npm ci --ignore-scripts` 后，`npm start -- --host 127.0.0.1 --port 8080` 启动同一个 Python HTTP 服务，`npm run test:browser` 执行上述浏览器回归。npm 仅负责命令编排，不是产品运行依赖；不要把 `web/` 单独作为静态站点运行，页面需要同源 API。

## 容器

```sh
docker build -t renewal-value:local .
docker run --rm -p 127.0.0.1:8080:8080 renewal-value:local
```

基础镜像以内容摘要固定，Python 打包依赖固定在 `requirements-build.txt`。容器构建完成后可断网运行；若要保留容器外分析版本，可将宿主目录挂载到 `/app/.state`。

## 快速业务核对

```sh
python3 -m renewal_value --demo
```

此入口启动真实本地 HTTP 服务，计算当前摘要，保存后重新读取再退出。输出依赖当前资料，不预先指定统计结论。初次阅读可先在页面调整时点或业务范围，再保存和回看分析；测试和该核对入口可在九十秒内完成，不包含镜像构建时间。
