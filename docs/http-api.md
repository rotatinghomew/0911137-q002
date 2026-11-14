# 本地接口

- `GET /api/catalog`：资料名称、可选业务范围、业务时点范围、表计数与资料指纹。
- `GET /api/view`：当前摘要，可选 `cutoff` 整数和 `group` 业务范围；未知筛选项返回 400。
- `GET /api/source?table=accounts`：按实际资料表名称查询原始记录；不存在的表返回 404。
- `POST /api/analyses`：JSON 对象包含 `title` 与 `options`，重新计算并保存，成功返回 201。`options` 与当前摘要的筛选结构相同。
- `GET /api/analyses`：只返回版本目录及资料指纹。
- `GET /api/analyses/{id}`：返回保存的完整结果，不重新计算。
- `GET /api/forecast`：未来六个业务期的净回款预测与回访名单，参数为整数 `cutoff`、`group`、非负数值 `contact_cost`（元）、可选非负整数 `capacity`。返回客户行 `rows`、批次汇总 `cohorts`、观察期不足名单 `excluded`、训练 cutoff 与样本量，以及每条客户行的预测依据 `basis` 和恒为 `null` 的 `causal_uplift`。
- `GET /api/backtest`：滚动回测，参数为整数 `start`、`end` 与可选 `group`。每个 fold 给出该 cutoff 的训练样本量、逐户期望与实际净回款、MAE、偏差与区间覆盖率；`start > end` 返回 400。

预测接口的训练 cutoff 全部严格早于请求 cutoff，训练特征只使用当时可见记录，因此预测不读取预测窗口内及其之后的信息。改变 `contact_cost` 或 `capacity` 只改变 `decision_value`、`recommend_contact` 与排序，不改变任何预测值。批次汇总的期望总额等于同范围客户行期望之和，客户页与批次页共享同一次预测结果。

API 的 `rows` 是领域行，`summary` 是当前汇总，`notes` 是业务口径提示，`options` 是实际采用的筛选值。无记录使用空列表，不能把无观测偷偷表示为有效的零值。错误 JSON 的 `error` 可直接在页面反馈。

这是本地单用户应用，不包含认证与外网部署设置。默认只监听回环地址，容器中可显式监听所有接口，但宿主机端口建议只绑定回环地址。
