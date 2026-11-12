const get = (id) => document.getElementById(id);
const variant = get('variant');
const time = get('time');
const status = get('status');
const context = get('map').getContext('2d');
const ru = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const num = (n) => ru.format(Number(n));
const states = {IDLE: 'свободно', UNAVAILABLE: 'недоступно', EMPTY_TRANSIT: 'перегон',
  LOADED_TRANSIT: 'везёт груз', WAITING: 'ожидание', PORT_QUEUE: 'очередь', LOADING: 'погрузка',
  UNLOADING: 'выгрузка', OPEN: 'свободен', OCCUPIED: 'занят', CLOSED: 'закрыт'};
const reason = {NO_DIRECTED_PATH: 'в графе нет пути', NO_COMPATIBLE_PAIR: 'нет совместимой пары',
  READY_AFTER_SEASON: 'заявка готова после закрытия сезона', LATE_UNLOADING: 'выгрузка после срока',
  EXPIRED_BEFORE_DISPATCH: 'срок годности истекает до отправления',
  EXPIRED_BY_UNLOADING: 'срок годности истёк к разгрузке',
  RESIDUAL_WITHOUT_PROVEN_CAUSE: 'причина недовоза не установлена'};
const certainty = {PROVEN: 'доказано', OBSERVED: 'наблюдается', UNDETERMINED: 'не установлено'};
const changes = {add_berth: 'Добавить причал', handling_tph: 'Ускорить обработку', close_berth: 'Закрыть причал'};
const layout = [[125, 220], [260, 145], [425, 70], [600, 170]];
let bundle, playback = null, request = 0, selectedPort = null, latestSnapshot;
const plan = () => bundle.plans[variant.value];
const list = (id, values) => {
  const items = values.map((value) => Object.assign(document.createElement('li'), {textContent: value}));
  get(id).replaceChildren(...items);
};
async function api(path, params = {}, method = 'GET') {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(query ? path + '?' + query : path, {method});
  const payload = await response.json();
  if (!response.ok) throw Error(payload.error || response.statusText);
  return payload;
}
function localDate(value) {
  return value.replace('T', ' ').slice(0, 16);
}
function pointOnEdge(edge, points, fraction) {
  const from = points[edge.origin_port], to = points[edge.destination_port];
  return [from[0] + (to[0] - from[0]) * fraction, from[1] + (to[1] - from[1]) * fraction];
}
function draw(result, vessels, ports) {
  const scenario = result.scenario;
  const points = Object.fromEntries(scenario.ports.map((port, index) => [port, layout[index]]));
  const edges = Object.fromEntries(scenario.edges.map((edge) => [edge.edge_id, edge]));
  const problem = bundle.diagnoses[variant.value].cargo[0];
  const cargo = scenario.cargo.find((item) => item.cargo_id === problem?.cargo_id);
  context.clearRect(0, 0, 720, 300);
  for (const edge of scenario.edges) {
    const [a, b] = [points[edge.origin_port], points[edge.destination_port]];
    const sea = edge.water_type === 'SEA' || edge.water_type === 'ESTUARY';
    context.strokeStyle = sea ? '#7256a2' : '#267987';
    context.lineWidth = 4;
    context.setLineDash(sea ? [8, 5] : []);
    context.beginPath();
    context.moveTo(...a);
    context.lineTo(...b);
    context.stroke();
    context.setLineDash([]);
    const [x, y] = pointOnEdge(edge, points, 0.65);
    const angle = Math.atan2(b[1] - a[1], b[0] - a[0]);
    context.save();
    context.translate(x, y);
    context.rotate(angle);
    context.beginPath();
    context.moveTo(-6, -5);
    context.lineTo(1, 0);
    context.lineTo(-6, 5);
    context.stroke();
    context.restore();
  }
  context.font = '600 17px Segoe UI';
  context.textAlign = 'center';
  for (const [port, [x, y]] of Object.entries(points)) {
    context.fillStyle = '#142c39';
    context.fillText(port, x, y + 27);
    const data = ports[port], berths = data.berths;
    const open = berths.filter((b) => b.status !== 'CLOSED');
    const occupied = open.filter((b) => b.status === 'OCCUPIED').length;
    if (!data.capacity_modelled) context.fillStyle = '#677d87';
    else if (!open.length) context.fillStyle = '#596070';
    else if (data.queued_tugs.length) context.fillStyle = '#ac3827';
    else if (occupied / open.length >= (scenario.port_display?.busy_threshold ?? 0.75)) context.fillStyle = '#a85120';
    else context.fillStyle = '#1c5c58';
    if (cargo?.destination_port === port) {
      context.strokeStyle = '#142c39';
      context.lineWidth = 2;
      context.beginPath();
      context.arc(x, y, 13, 0, 2 * Math.PI);
      context.stroke();
    }
    context.beginPath();
    context.arc(x, y, 8, 0, 2 * Math.PI);
    context.fill();
  }
  context.font = '600 14px Segoe UI';
  scenario.tugs.forEach(({vessel_id: id}, index) => {
    const vessel = vessels[id];
    const [x, y] = vessel.edge_id ? pointOnEdge(edges[vessel.edge_id], points, vessel.progress) : points[vessel.port];
    context.fillStyle = {EMPTY_TRANSIT: '#a85120', LOADED_TRANSIT: '#16713d', PORT_QUEUE: '#a85120'}[vessel.state] || '#15415a';
    context.fillRect(x - 7 + index * 14, y - 14, 12, 9);
    context.fillText(id, x, y - 24 - index * 15);
  });
}
async function updateState() {
  const result = plan(), key = variant.value, version = ++request;
  const date = new Date(Date.parse(result.scenario.start + 'Z') + 1000 * Number(time.value));
  const moment = date.toISOString().slice(0, 19);
  get('moment').value = localDate(moment);
  const snapshot = await api('/api/state', {variant: key, moment});
  if (version !== request) return;
  latestSnapshot = snapshot;
  draw(result, snapshot.vessels, snapshot.ports);
  list('fleet', Object.values(snapshot.vessels).map((v) =>
    v.vessel_id + ' · ' + (states[v.state] || v.state) + ' · ' + (v.port ?? v.edge_id)));
  get('ports').replaceChildren(...Object.entries(snapshot.ports).map(([port, data]) => {
    const li = document.createElement('li'), button = document.createElement('button');
    button.textContent = port;
    button.onclick = () => { selectedPort = port; renderPort(); };
    const occupied = data.berths.filter((b) => b.status === 'OCCUPIED').length;
    const open = data.berths.filter((b) => b.status !== 'CLOSED').length;
    const vessels = result.scenario.tugs.filter((v) => data.vessels.some((p) => p.vessel_id === v.vessel_id));
    li.append(button, ' · составов ' + vessels.length + ' · ' + (data.capacity_modelled
      ? 'занято ' + occupied + ' из ' + open + ' открытых · очередь ' + data.queued_tugs.length
      : 'вместимость не задана'));
    return li;
  }));
  renderPort();
}
function renderPort() {
  if (!latestSnapshot || !selectedPort) return;
  const result = plan(), data = latestSnapshot.ports[selectedPort];
  const period = result.port_operations?.[selectedPort];
  const infrastructure = result.scenario.port_operations?.[selectedPort]?.berths || [];
  const rows = [selectedPort + ' · ' + result.data_status + ' · схема без географических координат'];
  for (const berth of data.berths) {
    const definition = infrastructure.find((b) => b.berth_id === berth.berth_id);
    const active = berth.operation;
    rows.push(berth.berth_id + ' ' + states[berth.status]
      + (berth.expected_release ? ' до ' + localDate(berth.expected_release) : '')
      + (active ? ' · ' + states[active.state] + ' · ' + active.tug_id : '')
      + (definition ? ' · грузы ' + Object.keys(definition.handling_tph).join(', ') : ''));
  }
  if (period) {
    rows.push('За период · погружено ' + num(period.loaded_t) + ' т · выгружено ' + num(period.unloaded_t) + ' т');
    rows.push('Пиковая очередь ' + period.peak_queue + ' · среднее ожидание '
      + (period.mean_wait_seconds === null ? '—' : num(period.mean_wait_seconds / 60) + ' мин'));
    rows.push(...period.berths.map((b) => b.berth_id + ' · занятость за период '
      + (b.utilization === null ? '—' : num(b.utilization * 100) + '%')));
  }
  const portCost = result.financial_evaluation.cost_by_port[selectedPort];
  if (portCost !== undefined) rows.push('Расходы по порту ' + num(portCost) + ' DEMO');
  if (data.vessels.length) rows.push('Суда · ' + data.vessels.map((v) => v.vessel_id + ' (' + states[v.state] + ')').join(', '));
  const list = document.createElement('ul');
  for (const row of rows) list.append(Object.assign(document.createElement('li'), {textContent: row}));
  get('port-card').replaceChildren(list);
}
get('map').onclick = ({offsetX, offsetY}) => {
  if (!bundle) return;
  const canvas = get('map');
  const index = layout.findIndex(([x, y]) => Math.hypot(x - offsetX * canvas.width / canvas.clientWidth,
                                                        y - offsetY * canvas.height / canvas.clientHeight) < 24);
  if (index >= 0) { selectedPort = plan().scenario.ports[index]; renderPort(); }
};
function showError(error) { status.textContent = error.message; }
function renderVariant() {
  const result = plan();
  const [start, end] = [result.scenario.start, result.scenario.end].map((date) => Date.parse(date + 'Z'));
  time.max = (end - start) / 1000;
  latestSnapshot = null;
  get('port-card').replaceChildren();
  time.value = 0;
  get('explanation').textContent = '';
  for (const tr of get('comparison').rows) tr.classList.toggle('selected', tr.dataset.variant === variant.value);
  const report = bundle.diagnoses[variant.value];
  list('diagnosis', [
    ...report.queues.map((row) => row.port + ' · зафиксировано ' + num(row.seconds / 3600) + ' ч ожидания причала'),
    ...report.cargo.flatMap((row) => row.evidence.map((e) =>
      row.cargo_id + ' · ' + certainty[e.kind] + ' · ' + reason[e.code] + ' · остаток ' + num(row.remaining_t) + ' т')),
  ]);
  get('voyages').replaceChildren(...result.voyages.map((row, index) => {
    const li = document.createElement('li'), button = document.createElement('button');
    button.textContent = '#' + (index + 1) + ' · ' + row.tug_id + ' / ' + row.barge_id + ' · ' + num(row.volume_t) + ' т';
    const key = variant.value;
    button.onclick = async () => {
      try {
        const v = await api('/api/explain', {variant: key, voyage: index + 1});
        if (key !== variant.value) return;
        get('explanation').textContent = [
          v.departure_port + ' → ' + v.pickup + ' → ' + v.destination,
          v.on_time ? 'выгрузка в срок' : 'выгрузка после срока',
          v.empty.length ? 'перегон по ' + v.empty.map((leg) => leg.edge_id).join(', ') : 'перегон не потребовался',
          v.cost ? num(v.cost.total_cost) + ' DEMO за рейс' : '',
        ].filter(Boolean).join(' · ');
      } catch (error) { showError(error); }
    };
    li.append(button);
    return li;
  }));
  updateState().catch(showError);
}
function showBundle(value) {
  bundle = value;
  variant.replaceChildren(...Object.keys(value.plans).map((key) =>
    new Option(key + ' · ' + value.labels[key], key)));
  variant.value = 'C';
  get('comparison').replaceChildren(...Object.entries(value.plans).map(([key, result]) => {
    const row = document.createElement('tr'), m = result.metrics;
    row.dataset.variant = key;
    const waiting = Object.values(result.port_operations || {}).reduce((sum, p) => sum + p.waiting_seconds, 0);
    const cells = [key + ' · ' + value.labels[key], num(m.delivered_on_time_t), num(m.delivered_total_t),
      num(m.critical_on_time_t), num(m.social_deficit_t), num(waiting / 3600),
      num(result.financial_evaluation.total_cost), result.schedule_audit.status === 'PASSED' ? 'Пройден' : 'Ошибка'];
    cells.forEach((cell) => { row.insertCell().textContent = cell; });
    return row;
  }));
  list('improvements', value.improvements.alternatives.map((item) => {
    const title = changes[item.change.kind] + ' · ' + item.change.port;
    const chosen = JSON.stringify(value.chosen_change) === JSON.stringify(item.change);
    return title + ' · ' + (chosen ? 'выбрано' : item.improved ? 'улучшило план' : 'не улучшило план')
      + ' · ' + (item.delta.delivered_on_time_t > 0 ? '+' : '') + num(item.delta.delivered_on_time_t)
      + ' т в срок · ' + num(item.delta.cost) + ' DEMO';
  }));
  get('reproduction').textContent = 'Версия ядра ' + value.manifest.algorithm_version
    + ' · SHA-256 входа ' + value.manifest.scenario_hash
    + ' · Команда: python -m experiments.run_yakutia --output results/повтор';
  renderVariant();
}
function stop() {
  if (playback !== null) clearInterval(playback);
  playback = null;
  get('play').textContent = 'Показать решение';
}
variant.onchange = () => { stop(); renderVariant(); };
time.oninput = () => { stop(); updateState().catch(showError); };
get('recalculate').onclick = async () => {
  stop();
  get('recalculate').disabled = true;
  status.textContent = 'Идёт расчёт и аудит…';
  try { showBundle(await api('/api/plan', {}, 'POST')); status.textContent = 'Расчёт и аудит завершены'; }
  catch (error) { showError(error); }
  finally { get('recalculate').disabled = false; }
};
get('play').onclick = () => {
  if (!bundle) return;
  if (playback !== null) { stop(); return; }
  variant.value = 'C';
  renderVariant();
  status.textContent = 'Исходный флот и ограничение причала';
  get('play').textContent = 'Остановить';
  let tick = 0;
  playback = setInterval(() => {
    tick += 1;
    if (tick === 3 && bundle.plans.D) {
      variant.value = 'D';
      renderVariant();
      status.textContent = changes[bundle.chosen_change.kind] + ' · ' + bundle.chosen_change.port;
    } else if (tick > 3) {
      time.value = Math.min(Number(time.max), Number(time.value) + 1800);
      updateState().catch(showError);
      if (time.value === time.max) {
        stop();
        status.textContent = 'Сравните варианты C и D в таблице';
      }
    }
  }, 360);
};
api('/api/demo').then(showBundle).catch(showError);
