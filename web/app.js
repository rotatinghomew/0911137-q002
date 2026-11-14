const statusNode = document.querySelector('#status');
const filters = document.querySelector('#filters');
let current = null;
let requestGeneration = 0;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}

function text(tag, value) {
  const node = document.createElement(tag);
  node.textContent = typeof value === 'object' ? JSON.stringify(value) : String(value ?? '未知');
  return node;
}

function table(container, rows) {
  container.replaceChildren();
  if (!rows.length) {
    container.append(text('p', '当前范围没有记录'));
    return;
  }
  const columns = Array.from(new Set(rows.flatMap(row => Object.keys(row))));
  const grid = document.createElement('table');
  const head = document.createElement('tr');
  for (const column of columns) head.append(text('th', column));
  const thead = document.createElement('thead');
  thead.append(head);
  grid.append(thead);
  const body = document.createElement('tbody');
  for (const row of rows) {
    const line = document.createElement('tr');
    for (const column of columns) line.append(text('td', row[column]));
    body.append(line);
  }
  grid.append(body);
  container.append(grid);
}

function summaryList(container, entries) {
  container.replaceChildren();
  for (const [key, amount] of Object.entries(entries)) {
    container.append(text('dt', key), text('dd', amount));
  }
}

function notesList(container, notes) {
  container.replaceChildren(...notes.map(note => text('li', note)));
}

function render(value, savedTitle = '') {
  current = value;
  summaryList(document.querySelector('#summary'), value.result.summary);
  notesList(document.querySelector('#notes'), value.result.notes);
  document.querySelector('#digest').textContent = `资料指纹：${value.digest}`;
  table(document.querySelector('#results'), value.result.rows);
  statusNode.textContent = savedTitle ? `正在查看保存的分析：${savedTitle}` : '分析已更新';
  filters.elements.cutoff.value = value.options.cutoff;
  filters.elements.group.value = value.options.group;
}

async function refresh() {
  const generation = ++requestGeneration;
  const query = new URLSearchParams(new FormData(filters));
  statusNode.textContent = '正在分析';
  try {
    const result = await api(`/api/view?${query}`);
    if (generation === requestGeneration) render(result);
  } catch (error) {
    if (generation === requestGeneration) {
      current = null;
      statusNode.textContent = error.message;
    }
  }
}

const accountColumns = ['account', 'cohort', 'state', 'tenure', 'expected_net_cash', 'p10', 'p50', 'p90', 'churn_probability', 'pause_probability', 'reactivation_probability', 'outstanding', 'contact_cost', 'decision_value', 'recommend_contact', 'causal_uplift'];
const cohortColumns = ['cohort', 'accounts', 'excluded_observation', 'expected_net_cash', 'p10_total', 'p90_total', 'downside_risk', 'mean_churn_probability', 'outstanding_total', 'contact_count', 'contact_cost_total', 'decision_value_total'];

function renderForecast(value) {
  const result = value.result;
  summaryList(document.querySelector('#forecast-summary'), result.summary);
  notesList(document.querySelector('#forecast-notes'), result.notes);
  table(document.querySelector('#forecast-accounts'), result.rows.map(row => Object.fromEntries(accountColumns.map(key => [key, row[key]]))));
  table(document.querySelector('#forecast-cohorts'), result.cohorts.map(row => Object.fromEntries(cohortColumns.map(key => [key, row[key]]))));
  table(document.querySelector('#forecast-excluded'), result.excluded);
  document.querySelector('#forecast-filters').elements.cutoff.value = value.options.cutoff;
  document.querySelector('#forecast-filters').elements.group.value = value.options.group;
  statusNode.textContent = `预测已更新：未来${result.horizon}期，训练样本 ${result.training_samples} 条`;
}

async function runForecast(event) {
  event.preventDefault();
  const generation = ++requestGeneration;
  const query = new URLSearchParams(new FormData(document.querySelector('#forecast-filters')));
  statusNode.textContent = '正在预测';
  try {
    const value = await api(`/api/forecast?${query}`);
    if (generation === requestGeneration) renderForecast(value);
  } catch (error) {
    statusNode.textContent = error.message;
  }
}

function renderBacktest(value) {
  const result = value.result;
  summaryList(document.querySelector('#backtest-summary'), result.summary);
  notesList(document.querySelector('#backtest-notes'), result.notes);
  const foldRows = result.folds.map(fold => ({
    cutoff: fold.cutoff,
    training_samples: fold.training_samples,
    evaluated: fold.evaluated,
    excluded: fold.excluded,
    mae: fold.mae,
    bias: fold.bias,
    coverage: fold.coverage,
  }));
  table(document.querySelector('#backtest-folds'), foldRows);
  statusNode.textContent = `回测完成：${result.summary.fold_count} 个滚动时点`;
}

async function runBacktest(event) {
  event.preventDefault();
  const generation = ++requestGeneration;
  const query = new URLSearchParams(new FormData(document.querySelector('#backtest-filters')));
  statusNode.textContent = '正在回测';
  try {
    const value = await api(`/api/backtest?${query}`);
    if (generation === requestGeneration) renderBacktest(value);
  } catch (error) {
    statusNode.textContent = error.message;
  }
}

function wireTabs() {
  const tabs = [['#tab-accounts', '#forecast-accounts'], ['#tab-cohorts', '#forecast-cohorts']];
  for (const [buttonSelector, panelSelector] of tabs) {
    document.querySelector(buttonSelector).addEventListener('click', () => {
      for (const [otherButton, otherPanel] of tabs) {
        document.querySelector(otherButton).classList.toggle('active', otherButton === buttonSelector);
        document.querySelector(otherPanel).hidden = otherPanel !== panelSelector;
      }
    });
  }
}

async function history() {
  const entries = await api('/api/analyses');
  const list = document.querySelector('#history');
  list.replaceChildren();
  for (const entry of entries) {
    const item = document.createElement('li');
    const button = text('button', entry.title);
    button.type = 'button';
    button.addEventListener('click', async () => {
      const generation = ++requestGeneration;
      try {
        const saved = await api(`/api/analyses/${entry.id}`);
        if (generation === requestGeneration) render(saved, saved.title);
      } catch (error) {
        statusNode.textContent = error.message;
      }
    });
    item.append(button, text('small', entry.created_at));
    list.append(item);
  }
}

filters.addEventListener('submit', event => {
  event.preventDefault();
  refresh();
});

document.querySelector('#forecast-filters').addEventListener('submit', runForecast);
document.querySelector('#backtest-filters').addEventListener('submit', runBacktest);
wireTabs();

document.querySelector('#save').addEventListener('submit', async event => {
  event.preventDefault();
  if (!current) return;
  try {
    const saved = await api('/api/analyses', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: event.target.elements.title.value, options: current.options})});
    render(saved, saved.title);
    await history();
  } catch (error) {
    statusNode.textContent = error.message;
  }
});

document.querySelector('#source').addEventListener('click', async () => {
  try {
    const data = await api(`/api/source?table=${encodeURIComponent(document.querySelector('#tables').value)}`);
    table(document.querySelector('#raw'), data.rows);
  } catch (error) {
    statusNode.textContent = error.message;
  }
});

async function start() {
  const catalog = await api('/api/catalog');
  document.querySelector('#description').textContent = catalog.description;
  filters.elements.cutoff.min = catalog.min_cutoff;
  filters.elements.cutoff.max = catalog.max_cutoff;
  filters.elements.cutoff.value = catalog.default_cutoff;
  const groups = [['', '全部'], ...catalog.groups.map(group => [group, group])];
  for (const selectName of ['group']) {
    for (const [key, label] of groups) {
      const option = text('option', label);
      option.value = key;
      filters.elements[selectName].append(option);
    }
  }
  for (const formId of ['forecast-filters', 'backtest-filters']) {
    const form = document.querySelector(`#${formId}`);
    for (const [key, label] of groups) {
      const option = text('option', label);
      option.value = key;
      form.elements.group.append(option);
    }
  }
  const forecastForm = document.querySelector('#forecast-filters');
  forecastForm.elements.cutoff.min = catalog.min_cutoff;
  forecastForm.elements.cutoff.max = catalog.max_cutoff;
  forecastForm.elements.cutoff.value = catalog.default_cutoff;
  const backtestForm = document.querySelector('#backtest-filters');
  backtestForm.elements.start.min = catalog.min_cutoff;
  backtestForm.elements.start.value = catalog.min_cutoff + 6;
  backtestForm.elements.end.max = catalog.max_cutoff;
  backtestForm.elements.end.value = catalog.default_cutoff - 6;
  for (const name of Object.keys(catalog.tables)) {
    const option = text('option', `${name}（${catalog.tables[name]}）`);
    option.value = name;
    document.querySelector('#tables').append(option);
  }
  await refresh();
  await history();
}

start().catch(error => { statusNode.textContent = error.message; });
