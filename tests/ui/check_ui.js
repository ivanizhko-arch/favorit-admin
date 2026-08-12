// Проверка страницы админки без браузера: разбираем её скрипт, подсовываем
// заглушки DOM и ответов сервера и смотрим, что нарисовалось.
//
// Ловит то, чего не видят тесты Python: экранирование чужих данных в
// разметке, ленивую загрузку вкладок, подписи диапазонов, состояния строк.
//
// Запуск: node tests/ui/check_ui.js
const fs = require('fs'), vm = require('vm'), path = require('path');
const HTML = fs.readFileSync(
  path.join(__dirname, '..', '..', 'app', 'static', 'admin.html'), 'utf8');

const code = HTML.match(/<script>([\s\S]*?)<\/script>/)[1];
let ok = 0, fail = 0;
const check = (n, c, e = '') => c ? (ok++, console.log('  OK   ' + n))
                                  : (fail++, console.log('  FAIL ' + n + ' ' + e));

// --- минимальный DOM ---
const els = {};
const mkEl = (id) => (els[id] = {
  id, value: '', textContent: '', innerHTML: '', disabled: false, dataset: {},
  hiddenNow: false, attrs: {},
  classList: {
    add(){}, remove(){},
    toggle(cls, on){ if (cls === 'hidden') els[id].hiddenNow = !!on; },
  },
  setAttribute(k, v){ els[id].attrs[k] = v; },
  addEventListener(){}, focus(){},
});
// Кнопки вкладок ищутся через querySelectorAll — отдаём их отдельно.
const tabButtons = ['dashboard', 'clients', 'quality'].map(t => {
  const b = mkEl('tab-' + t); b.dataset = { tab: t }; return b;
});
const document = {
  getElementById: id => els[id] || mkEl(id),
  querySelectorAll: sel => sel.indexOf('.tabs button') === 0 ? tabButtons : [],
  querySelector: sel => {
    const m = /data-tab="([^"]+)"/.exec(sel);
    return m ? tabButtons.find(b => b.dataset.tab === m[1]) : null;
  },
  addEventListener(){},
};
const listeners = {};
const window = { addEventListener: (t, f) => (listeners[t] = f) };
// Как в песочнице с непрозрачным источником: history API недоступен.
// Страница обязана это пережить, а не терять данные вкладки.
const history = { replaceState(){ throw new Error('SecurityError'); } };
const location = { hash: '', reload(){} };

// --- мок API ---
const MOCK = {
  '/auth/config': { totp_required: true },
  '/stats': {
    total: 12, blocked: 2, active_7d: 7, logins: 40, active_30d: 10, new_7d: 3,
    collectors: { total: 5, confirmed: 3, pending: 2, whitelist: 1 },
    nps: { count: 4, avg_score: 9.25, reviews: 2, review_rate: 50.0, count_30d: 2, avg_score_30d: 9.5 },
  },
  '/nps/trend?months=6': [
    { month: '2026-02', count: 0, avg_score: 0, reviews: 0 },
    { month: '2026-03', count: 3, avg_score: 8.7, reviews: 1 },
    { month: '2026-04', count: 0, avg_score: 0, reviews: 0 },   // разрыв
    { month: '2026-05', count: 5, avg_score: 9.4, reviews: 3 },
    { month: '2026-06', count: 2, avg_score: 10, reviews: 0 },
    { month: '2026-07', count: 4, avg_score: 9.25, reviews: 2 },
  ],
  '/quality/status': {
    bitrix_configured: false, qc_head_set: false, recipients: 0,
    unlinked_scores: 4, stuck_tasks: 2, due_month: '2026-06',
    last_report: { year_month: '2026-06', sent_at: '2026-07-01T09:00:00+00:00' },
    grades: { low_max: 7, promoter_min: 9, low_label: '0-7',
              neutral_label: '8', top_label: '9-10', has_neutral: true },
  },
  '/collectors/whitelist': [{ phone: '4951111111', label: 'Суд & ФССП', at: '2026-07-01T10:00:00+00:00' }],
};
const listResp = (items, total, offset = 0) => ({ items, total, limit: 50, offset });
const TREND_ONLY_PROMOTERS = MOCK['/nps/trend?months=6'];

function mockFetch(url) {
  const path = url.replace('/admin/api', '').split('?')[0];
  const full = url.replace('/admin/api', '');
  let body;
  if (MOCK[full] !== undefined) body = MOCK[full];
  else if (path === '/quality/rating') body = {
      year_month: '2026-07',
      leader: { manager_id: 101, manager_name: 'Анна <b>Смирнова</b>',
                top: 4, low: 1, neutral: 1, scores: 6, net: 3 },
      managers: [
        { manager_id: 101, manager_name: 'Анна <b>Смирнова</b>',
          top: 4, low: 1, neutral: 1, scores: 6, net: 3 },
        { manager_id: 102, manager_name: 'Борис Козлов',
          top: 1, low: 1, neutral: 2, scores: 4, net: 0 },
        { manager_id: 0, manager_name: 'Не определён',
          top: 0, low: 2, neutral: 0, scores: 2, net: -2 },
      ],
      totals: { top: 5, neutral: 3, low: 4, net: 1, scores: 12 },
    };
  else if (path === '/nps') body = Object.assign({ grades: {
      low_max: 7, promoter_min: 9, low_label: '0-7',
      neutral_label: '8', top_label: '9-10', has_neutral: true } }, listResp([
      { id: 5, email: 'sad@ex.ru', score: 3, left_review: 0, category: 'low',
        created_at: '2026-07-28T10:00:00+00:00', manager_id: 102,
        manager_name: 'Борис Козлов', qc_task_id: 9001, attempts: 0, last_error: '' },
      { id: 4, email: 'stuck@ex.ru', score: 1, left_review: 0, category: 'low',
        created_at: '2026-07-27T10:00:00+00:00', manager_id: 0,
        manager_name: '', qc_task_id: 0, attempts: 3, last_error: '500 Internal' },
      { id: 3, email: 'fresh@ex.ru', score: 5, left_review: 0, category: 'low',
        created_at: '2026-07-27T09:00:00+00:00', manager_id: 101,
        manager_name: 'Анна Смирнова', qc_task_id: 0, attempts: 0, last_error: '' },
      { id: 7, email: 'seven@ex.ru', score: 7, left_review: 0, category: 'low',
        created_at: '2026-07-29T08:00:00+00:00', manager_id: 101,
        manager_name: 'Анна Смирнова', qc_task_id: 8899, attempts: 0, last_error: '' },
      { id: 2, email: 'meh@ex.ru', score: 8, left_review: 0, category: 'neutral',
        created_at: '2026-07-26T10:00:00+00:00', manager_id: 101,
        manager_name: 'Анна Смирнова', qc_task_id: 0, attempts: 0, last_error: '' },
      { id: 1, email: '<img src=x>@ex.ru', score: 10, left_review: 1, category: 'top',
        created_at: '2026-07-25T10:00:00+00:00', manager_id: 101,
        manager_name: 'Анна Смирнова', qc_task_id: 0, attempts: 0, last_error: '' },
    ], 6, 0));
  else if (path === '/users') body = listResp([
      { email: "o'brien@ex.ru", name: '<img src=x onerror=alert(1)>', phone: '9990001122',
        last_seen: '2026-07-20T09:30:00+00:00', blocked: 0, consent_at: null },
      { email: 'anna@ex.ru', name: 'Анна', phone: '', last_seen: null, blocked: 1,
        consent_at: '2026-01-01T00:00:00+00:00' },
    ], 2);
  else if (path === '/collectors') body = listResp([
      { phone: '9991112233', raw: '+7 (999) 111-22-33', category: 'collector',
        status: 'confirmed', reports: 7, last_reported: '2026-07-25T12:00:00+00:00' },
      { phone: '9260000001', raw: '8926...', category: 'scammer', status: 'pending',
        reports: 1, last_reported: '2026-07-26T12:00:00+00:00' },
    ], 2);
  else if (path === '/complaints') body = Object.assign(listResp([
      { id: 31, email: 'angry@ex.ru', category: 'manager',
        category_label: 'Работа менеджера', text: 'Менеджер <b>не</b> отвечает',
        status: 'open', status_label: 'Новая', qc_task_id: 0, attempts: 0,
        last_error: '', answer_due: '2026-07-20T10:00:00+00:00', overdue: true,
        resolution: '', created_at: '2026-07-10T10:00:00+00:00' },
      { id: 30, email: 'calm@ex.ru', category: 'money',
        category_label: 'Деньги и платежи', text: 'Списали дважды',
        status: 'in_progress', status_label: 'В работе', qc_task_id: 8801,
        attempts: 0, last_error: '', answer_due: '2026-08-05T10:00:00+00:00',
        overdue: false, resolution: '', created_at: '2026-07-26T10:00:00+00:00' },
      { id: 29, email: 'done@ex.ru', category: 'app',
        category_label: 'Мобильное приложение', text: 'Не открывается',
        status: 'resolved', status_label: 'Решена', qc_task_id: 8790,
        attempts: 0, last_error: '', answer_due: '2026-07-30T10:00:00+00:00',
        overdue: false, resolution: 'Обновили сборку', created_at: '2026-07-20T10:00:00+00:00' },
      { id: 28, email: 'stuck@ex.ru', category: 'other',
        category_label: 'Другое', text: 'Прочее', status: 'open',
        status_label: 'Новая', qc_task_id: 0, attempts: 4,
        last_error: 'tasks.task.add: HTTP 401', answer_due: '2026-08-02T10:00:00+00:00',
        overdue: false, resolution: '', created_at: '2026-07-23T10:00:00+00:00' },
    ], 4, 0), { categories: { manager:'Работа менеджера', money:'Деньги и платежи',
        deadlines:'Сроки по делу', app:'Мобильное приложение', other:'Другое' },
        statuses: { open:'Новая', in_progress:'В работе', resolved:'Решена', rejected:'Отклонена' } });
  else if (path === '/supervision') body = {
      status: { refusals: 2, reason_unknown: 1, stuck: 3,
                qc_head_set: true, support_head_set: false,
                bitrix_configured: true },
      managers: {
        managers: [
          { manager_id: 102, manager_name: 'Борис Козлов', position: 'Менеджер',
            deals: 2, refusals: 1, in_progress: 1, done: 0, manager_fault: 0,
            reason_unknown: 1, refusal_rate: 50.0 },
          { manager_id: 101, manager_name: 'Анна <b>Смирнова</b>',
            position: 'Менеджер сопровождения', deals: 8,
            refusals: 2, in_progress: 4, done: 2, manager_fault: 1,
            reason_unknown: 0, refusal_rate: 25.0 },
          { manager_id: 103, manager_name: 'Кирилл Тихий', position: '',
            deals: 5, refusals: 0, in_progress: 3, done: 2, manager_fault: 0,
            reason_unknown: 0, refusal_rate: 0.0 },
        ],
        hidden: [
          { manager_id: 200, manager_name: 'Робот <b>Фаворит</b>', deals: 10,
            refusals: 7, hidden_reason: 'не сотрудник: робот или внешняя учётка' },
          { manager_id: 201, manager_name: 'Пётр Уволенный', deals: 4,
            refusals: 2, hidden_reason: 'уволен' },
        ],
        hidden_totals: { people: 2, deals: 14, refusals: 9 },
        totals: { deals: 15, refusals: 3, refusal_rate: 20.0, reason_unknown: 1 },
        reasons: { manager: 'Не устроил менеджер', price: 'Цена или нет денег',
                   changed_mind: 'Передумал банкротиться', other: 'Другое' },
        sources: { client: 'со слов клиента', qc: 'контроль качества',
                   manager: 'со слов менеджера', bitrix: 'из карточки Битрикса' },
        manager_fault_codes: ['manager'],
      },
    };
  else if (path === '/supervision/refusals') body = Object.assign(listResp([
      { deal_id: 41, manager_id: 101, manager_name: 'Анна Смирнова',
        stage_id: 'C15:LOSE', stage_name: 'Сделка провалена',
        reason: 'manager', reason_label: 'Не устроил менеджер',
        manager_fault: true, comment: 'Не <b>брал</b> трубку',
        source: 'client', source_label: 'со слов клиента', stated_by: 'admin',
        qc_task_id: 0, refused_at: '2026-07-20T10:00:00+00:00', days_lived: 120.0 },
      { deal_id: 42, manager_id: 102, manager_name: 'Борис Козлов',
        stage_id: 'C15:EXECUTING', stage_name: 'Отказ от работы',
        reason: '', reason_label: '', manager_fault: false, comment: '',
        source: '', source_label: '', stated_by: '', qc_task_id: 7701,
        refused_at: '2026-07-22T10:00:00+00:00', days_lived: 50.0 },
      { deal_id: 43, manager_id: 102, manager_name: 'Борис Козлов',
        stage_id: 'C15:LOSE', stage_name: 'Сделка провалена',
        reason: '', reason_label: '', manager_fault: false, comment: '',
        source: '', source_label: '', stated_by: '', qc_task_id: 0,
        refused_at: '2026-07-25T10:00:00+00:00', days_lived: null },
    ], 3, 0), { reasons: { manager: 'Не устроил менеджер' },
                sources: { client: 'со слов клиента' } });
  else if (path === '/supervision/stuck') body = {
      total: 3, thresholds: { 'C15:UC_3T0KG4': 62.0 },
      items: [
        { deal_id: 51, manager_id: 101, manager_name: 'Анна Смирнова',
          stage_id: 'C15:UC_3T0KG4', stage_name: 'Сбор документов',
          days_on_stage: 240.0, limit_days: 62.0, median_days: 31.0,
          over_days: 178.0, alerted: true, entered_at: '2026-01-01T00:00:00+00:00' },
        { deal_id: 52, manager_id: 102, manager_name: 'Борис Козлов',
          stage_id: 'C15:UC_T28ZLJ', stage_name: 'Поданы',
          days_on_stage: 95.0, limit_days: 58.0, median_days: 29.0,
          over_days: 37.0, alerted: false, entered_at: '2026-05-01T00:00:00+00:00' },
      ]};
  else if (path === '/stages') body = {
      overall: { done: { count: 3, avg_days: 300.0, median_days: 300.0,
                         max_days: 400.0, enough: true },
                 total_deals: 6, in_progress: 2, failed: 1,
                 done_stage_name: 'Долг списан' },
      status: { configured: true, category_id: 15, last_sync: '2026-07-31T09:00:00+00:00',
                deals: 6, history_rows: 24, stages: 17, min_sample: 3 },
      funnel: { total_on_stages: 6, stages: [
        { stage_id:'C15:FINAL_INVOICE', name:'Договор заключен', sort:20, kind:'work',
          alternative:false, current:0, longest_wait_days:null,
          count:6, avg_days:12.5, median_days:11.0, max_days:20.0, enough:true },
        { stage_id:'C15:UC_T28ZLJ', name:'Поданы', sort:50, kind:'work',
          alternative:false, current:1, longest_wait_days:60.0,
          count:3, avg_days:88.0, median_days:80.0, max_days:110.0, enough:true },
        { stage_id:'C15:UC_ZTR9AW', name:'Пауза', sort:120, kind:'stuck',
          alternative:false, current:1, longest_wait_days:240.0,
          count:0, avg_days:null, median_days:null, max_days:null, enough:false },
        { stage_id:'C15:UC_LWCW5Y', name:'Реструктуризация', sort:90, kind:'work',
          alternative:true, current:0, longest_wait_days:null,
          count:1, avg_days:280.0, median_days:280.0, max_days:280.0, enough:false },
        { stage_id:'C15:UC_UJI8T9', name:'Долг списан', sort:130, kind:'done',
          alternative:false, current:2, longest_wait_days:20.0,
          count:1, avg_days:5.0, median_days:5.0, max_days:5.0, enough:false },
        { stage_id:'C15:LOSE', name:'Сделка провалена', sort:170, kind:'failed',
          alternative:false, current:1, longest_wait_days:30.0,
          count:0, avg_days:null, median_days:null, max_days:null, enough:false },
      ]},
    };
  else body = {};
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body),
                           headers: { get: () => null } });
}

// Считаем запросы — ленивая загрузка должна экономить их, а не делать вид.
let requests = [];
const countingFetch = url => { requests.push(String(url)); return mockFetch(url); };

const ctx = {
  document, window, history, location, fetch: countingFetch, console,
  localStorage: { getItem: () => 'tok', setItem(){}, removeItem(){} },
  setTimeout, clearTimeout, alert: () => {}, confirm: () => true,
  URLSearchParams,
};
ctx.globalThis = ctx;

console.log('== Синтаксис ==');
try { new vm.Script(code); check('скрипт парсится', true); }
catch (e) { check('скрипт парсится', false, e.message); process.exit(1); }

vm.createContext(ctx);
vm.runInContext(code, ctx);

(async () => {
  console.log('\n== Вкладки ==');
  // При загрузке страницы скрипт сам открыл дашборд — смотрим, что ушло.
  await new Promise(r => setTimeout(r, 40));
  check('при входе грузится только дашборд',
        requests.some(u => /stats/.test(u)) && requests.some(u => /trend/.test(u)) &&
        !requests.some(u => /users|collectors|quality/.test(u)), requests);
  // Дашборд: сводка, стадии, тренд. Плюс auth/config при загрузке страницы.
  check('лишних запросов при входе нет', requests.length === 4, requests);
  check('панель клиентов скрыта', els['p-clients'].hiddenNow === true);
  check('панель качества скрыта', els['p-quality'].hiddenNow === true);
  check('панель дашборда видна', els['p-dashboard'].hiddenNow === false);
  check('вкладка помечена выбранной',
        tabButtons[0].attrs['aria-selected'] === 'true' &&
        tabButtons[1].attrs['aria-selected'] === 'false');

  requests = [];
  ctx.openTab('clients');
  await new Promise(r => setTimeout(r, 30));
  check('клиенты грузят только свои данные',
        requests.every(u => /users|collectors/.test(u)), requests);
  check('панель клиентов открылась', els['p-clients'].hiddenNow === false);
  check('дашборд скрылся', els['p-dashboard'].hiddenNow === true);

  requests = [];
  ctx.openTab('clients');
  await new Promise(r => setTimeout(r, 20));
  check('повторное открытие не перезапрашивает', requests.length === 0, requests);

  requests = [];
  ctx.openTab('quality');
  await new Promise(r => setTimeout(r, 30));
  check('качество грузит свои данные',
        requests.some(u => /quality/.test(u)) && requests.some(u => /nps/.test(u)), requests);

  // Блокировка клиента меняет сводку на другой вкладке.
  requests = [];
  ctx.invalidate('dashboard');
  await new Promise(r => setTimeout(r, 20));
  check('чужая вкладка не дёргает запрос сразу', requests.length === 0, requests);
  requests = [];
  ctx.openTab('dashboard');
  await new Promise(r => setTimeout(r, 30));
  check('устаревшая вкладка перезагружается при открытии',
        requests.length === 3, requests);

  requests = [];
  ctx.invalidate('dashboard');
  await new Promise(r => setTimeout(r, 20));
  check('открытая вкладка обновляется сразу', requests.length === 3, requests);

  ctx.openTab('нет-такой');
  check('неизвестная вкладка → дашборд', els['p-dashboard'].hiddenNow === false);

  console.log('\n== Экранирование ==');
  check('esc гасит теги', ctx.esc('<img src=x>') === '&lt;img src=x&gt;', ctx.esc('<img src=x>'));
  check('esc гасит кавычки', ctx.esc(`"'&`) === '&quot;&#39;&amp;');
  check('esc терпит null', ctx.esc(null) === '' && ctx.esc(undefined) === '');
  check("jsq экранирует апостроф", ctx.jsq("o'brien") === "o\\&#39;brien", ctx.jsq("o'brien"));
  check('when режет ISO', ctx.when('2026-07-20T09:30:00+00:00') === '2026-07-20 09:30');
  check('when терпит пустоту', ctx.when(null) === '—');

  console.log('\n== Рендер ==');
  await ctx.loadStats();
  const kpi = els['kpi'].innerHTML;
  check('KPI: три группы', (kpi.match(/kpi-group/g) || []).length === 3);
  check('KPI: 11 плиток', (kpi.match(/class="tile"/g) || []).length === 11);
  check('KPI: значения на месте', kpi.includes('>12<') && kpi.includes('>9.25<') && kpi.includes('>50%<'));
  check('KPI: подпись "за 30 дней"', kpi.includes('за 30 дней: 10'));
  check('KPI: плитки аудита нет', !kpi.includes('Действий админа'));

  console.log('\n== Стадии банкротства ==');
  await ctx.loadStages();
  const ovh = els['st-overall'].innerHTML;
  check('средний срок дела показан', ovh.includes('300 дн.'), ovh.slice(0, 200));
  check('медиана рядом со средним', ovh.includes('медиана 300 дн.'));
  check('дошедшие до списания посчитаны', ovh.includes('>3<'));
  check('название стадии завершения подставлено', ovh.includes('Долг списан'));
  check('в работе и провалы разведены',
        ovh.includes('В работе сейчас') && ovh.includes('Отказы и провалы'));
  check('оговорка, что провалы не в среднем',
        ovh.includes('в средний срок не входят'));

  const str = els['st-rows'].innerHTML;
  check('стадии: 6 строк', (str.match(/<tr>/g) || []).length === 6);
  check('служебных стадий нет', !str.includes('тех.этап') && !str.includes('Гараджи'));
  check('«Пауза» помечена как ожидание',
        str.includes('ожидание, не работа'), '');
  check('провальная стадия помечена', str.includes('дело не дошло до списания'));
  check('альтернативные процедуры помечены',
        (str.match(/одна из двух/g) || []).length === 1, '');
  check('мало данных вместо цифры',
        str.includes('мало данных'), '');
  check('при одном прохождении среднее не показывается',
        !str.includes('280 дн.'), '');
  check('долгое ожидание подсвечено',
        str.includes('due-late">240 дн.'), '');
  check('короткое ожидание не подсвечено', str.includes('muted">60 дн.'), '');
  check('методика подписана под таблицей',
        els['st-note'].innerHTML.includes('только по дошедшим') &&
        els['st-note'].innerHTML.includes('мало данных'));
  check('в методике объяснена медиана',
        els['st-note'].innerHTML.includes('сдвигает среднее'));
  check('статус снимка показан',
        els['st-status'].innerHTML.includes('воронка 15') &&
        els['st-status'].innerHTML.includes('сделок 6'));
  check('предупреждений нет, когда всё настроено',
        els['st-warn'].innerHTML === '');

  await ctx.loadTrend();
  const svg = els['nps-chart'].innerHTML;
  check('график: svg отрисован', svg.startsWith('<svg'));
  check('график: 6 столбцов', (svg.match(/<rect/g) || []).length === 6);
  check('график: точка на каждый непустой месяц', (svg.match(/<circle/g) || []).length === 4);
  check('график: линия разорвана на пустом месяце',
        (svg.match(/<polyline/g) || []).length === 2, (svg.match(/<polyline/g) || []).length);
  check('график: нет NaN', !svg.includes('NaN'), svg.slice(0, 200));
  check('график: подписи месяцев', svg.includes('>03.26<') && svg.includes('>07.26<'));
  check('график: пока только промоутеры — шкала 8-10',
        svg.includes('>8<') && svg.includes('>9<') && !svg.includes('>5<'));
  check('подпись объясняет отсутствие детракторов',
        els['trend-note'].innerHTML.includes('8-10') &&
        els['trend-note'].innerHTML.includes('основной сервис'));
  check('легенда уточняет диапазон',
        els['trend-legend'].textContent === 'Средняя оценка (8-10)');

  // Тот же график, когда основной backend начнёт сохранять низкие оценки.
  MOCK['/nps/trend?months=6'] = [
    { month:'2026-02', count:4, avg_score:9.1, reviews:1 },
    { month:'2026-03', count:6, avg_score:8.5, reviews:2 },
    { month:'2026-04', count:5, avg_score:6.4, reviews:0 },   // провал
    { month:'2026-05', count:7, avg_score:3.2, reviews:0 },   // сильный провал
    { month:'2026-06', count:5, avg_score:7.8, reviews:1 },
    { month:'2026-07', count:9, avg_score:9.0, reviews:4 },
  ];
  await ctx.loadTrend();
  const svg2 = els['nps-chart'].innerHTML;
  check('шкала развернулась на 0-10',
        svg2.includes('>0<') && svg2.includes('>5<') && svg2.includes('>10<'), '');
  check('провал до 3.2 не упёрся в нижнюю границу', (() => {
    // Точки: y=низ соответствует 0. Значение 3.2 должно быть заметно выше низа.
    const ys = [...svg2.matchAll(/<circle cx="[\d.]+" cy="([\d.]+)"/g)].map(m => +m[1]);
    return ys.length === 6 && Math.max(...ys) < 140 && new Set(ys).size === 6;
  })(), '');
  check('подпись сменилась на общую',
        els['trend-note'].innerHTML.includes('0-10') &&
        !els['trend-note'].innerHTML.includes('основной сервис'));
  check('легенда без диапазона', els['trend-legend'].textContent === 'Средняя оценка');
  MOCK['/nps/trend?months=6'] = TREND_ONLY_PROMOTERS;
  await ctx.loadTrend();

  await ctx.loadUsers();
  const rows = els['rows'].innerHTML;
  check('юзеры: 2 строки', (rows.match(/<tr>/g) || []).length === 2);
  check('юзеры: XSS в имени обезврежен', !rows.includes('<img src=x'), rows.slice(0, 300));
  check('юзеры: апостроф в onclick экранирован', rows.includes("block('o\\&#39;brien@ex.ru'"), '');
  check('юзеры: метка "нет согласия"', rows.includes('нет согласия'));
  check('юзеры: пустой last_seen → тире', rows.includes('>—<'));
  check('пагинация: счётчик', els['u-count'].textContent === '1–2 из 2', els['u-count'].textContent);
  check('пагинация: "назад" выключена', els['u-prev'].disabled === true);
  check('пагинация: "вперёд" выключена', els['u-next'].disabled === true);

  await ctx.loadCollectors();
  const cr = els['col-rows'].innerHTML;
  check('коллекторы: 2 строки', (cr.match(/<tr>/g) || []).length === 2);
  check('коллекторы: формат номера', cr.includes('+7 9991112233'));
  check('коллекторы: бейдж confirmed', cr.includes('Заблокирован'));
  check('коллекторы: бейдж pending', cr.includes('На проверке'));

  check('функций аудита в скрипте нет',
        ctx.loadAudit === undefined && ctx.purgeAudit === undefined);
  check('страница не запрашивает /audit', !code.includes("'/audit"));

  await ctx.loadWhitelist();
  check('белый список: амперсанд экранирован',
        els['wl-rows'].innerHTML.includes('Суд &amp; ФССП'));

  console.log('\n== Оценки и контроль качества ==');
  await ctx.loadNps();
  const np = els['nps-rows'].innerHTML;
  check('оценки: 6 строк', (np.match(/<tr/g) || []).length === 6);
  check('низкие выделены строкой', (np.match(/class="row-low"/g) || []).length === 4,
        (np.match(/class="row-low"/g) || []).length);
  check('семёрка среди низких, с задачей',
        np.includes('badge b-low">7/10') && np.includes('Задача №8899'), '');
  // Подписи диапазонов строятся из ответа сервера, а не зашиты в разметку.
  check('заголовок «лучших» подписан диапазоном',
        els['q-th-top'].textContent === 'Лучших (9-10)', els['q-th-top'].textContent);
  check('заголовок «низких» подписан диапазоном',
        els['q-th-low'].textContent === 'Низких (0-7)', els['q-th-low'].textContent);
  check('правило рейтинга подписано теми же диапазонами',
        els['q-rule'].innerHTML.includes('(9-10) минус количество низких (0-7)'),
        els['q-rule'].innerHTML);
  check('единственная нейтральная подписана без диапазона',
        els['q-rule'].innerHTML.includes('Нейтральные (8) не учитываются'),
        els['q-rule'].innerHTML);
  check('в разметке диапазонов не осталось',
        !HTML.includes('(0-6)') && !HTML.includes('(7-8)'), '');
  check('промоутер не выделен', !np.includes('row-low">\n          <td><img'));
  check('низкая помечена красным бейджем', np.includes('badge b-low'));
  check('нейтральная 8 своим бейджем — не «лучшая»',
        np.includes('badge b-neutral">8/10'), np.slice(0, 100));
  check('номер задачи показан', np.includes('Задача №9001'));
  check('ожидающая задача помечена', np.includes('В очереди'));
  check('застрявшая показывает число попыток', np.includes('Не создана (3)'));
  check('текст ошибки уходит в title', np.includes('title="500 Internal"'));
  check('неизвестный менеджер подписан', np.includes('не определён'));
  check('у промоутера колонка контроля пуста',
        (np.match(/>—</g) || []).length >= 2);
  check('XSS в e-mail обезврежен', !np.includes('<img src=x>@'));
  check('пагинация оценок', els['n-count'].textContent === '1–6 из 6',
        els['n-count'].textContent);

  await ctx.loadQuality();
  const warn = els['q-warn'].innerHTML;
  check('предупреждение про вебхук', warn.includes('BITRIX_WEBHOOK_URL'));
  check('предупреждение про руководителя КК', warn.includes('BITRIX_QC_HEAD_ID'));
  check('предупреждение про получателей отчёта', warn.includes('никому не уйдёт'));
  check('застрявшие задачи в предупреждении', warn.includes('2</b> задач'));
  check('статус: неразобранные оценки', els['q-status'].innerHTML.includes('не разобрано оценок: 4'));
  check('статус: последний отчёт', els['q-status'].innerHTML.includes('2026-06'));

  const lead = els['q-leader'].innerHTML;
  check('лидер отрисован', lead.includes('leader'));
  check('лидер: итог со знаком плюс', lead.includes('+3'), lead);
  check('лидер: лучшие и низкие', lead.includes('лучших 4') && lead.includes('низких 1'));
  check('лидер: подпись «итог», не «баллов»',
        lead.includes('итог') && !lead.includes('баллов'));
  check('XSS в имени менеджера обезврежен', !lead.includes('<b>Смирнова'));

  const qr = els['q-rows'].innerHTML;
  check('рейтинг: 3 строки', (qr.match(/<tr>/g) || []).length === 3);
  check('рейтинг: положительный итог зелёный',
        qr.includes('var(--ok)') && qr.includes('+3'));
  check('рейтинг: отрицательный итог красный и с минусом',
        qr.includes('var(--danger)') && qr.includes('−2'), qr);
  check('рейтинг: нулевой итог нейтральный', qr.includes('var(--muted)">0<'), qr);
  check('рейтинг: лучшие зелёным бейджем', qr.includes('badge b-ok'));
  check('рейтинг: низкие красным бейджем', qr.includes('badge b-low'));
  check('рейтинг: правило подписано под таблицей',
        HTML.includes('минус количество низких'));
  check('оговорка про порог лидера',
        els['q-rule'].innerHTML.includes('хотя бы одну оценку 9-10'),
        els['q-rule'].innerHTML);

  els['q-month'].value = '';
  check('месяц по умолчанию — текущий',
        /^\d{4}-\d{2}$/.test(ctx.currentMonth()), ctx.currentMonth());

  console.log('\n== Отказы по менеджерам ==');
  await ctx.loadSupervision();
  const sv = els['sv-rows'].innerHTML;
  check('менеджеры: 3 строки', (sv.match(/<tr>/g) || []).length === 3);
  check('доля отказов показана', sv.includes('50%') && sv.includes('25%'));
  check('худшая доля первой', sv.indexOf('Борис') < sv.indexOf('Анна'), '');
  check('высокая доля красным', sv.includes('var(--danger)'));
  check('нулевая доля зелёным', sv.includes('var(--ok)'));
  check('колонка «из-за менеджера» отдельно', sv.includes('badge b-low">1<'), '');
  check('XSS в имени менеджера обезврежен', !sv.includes('<b>Смирнова'));
  check('должность показана', sv.includes('Менеджер сопровождения'));
  check('пустая должность не ломает строку', sv.includes('>—<'));

  const hid = els['sv-hidden'].innerHTML;
  check('скрытые посчитаны', hid.includes('2</b> — 14 дел, 9 отказов'), hid);
  check('скрытые названы поимённо', hid.includes('Робот') && hid.includes('Пётр Уволенный'));
  check('причина скрытия указана',
        hid.includes('робот или внешняя учётка') && hid.includes('уволен'));
  check('XSS в имени скрытого обезврежен', !hid.includes('<b>Фаворит'));
  check('подпись объясняет, что доля от всех дел',
        els['sv-note'].innerHTML.includes('от всех дел менеджера'));
  check('подпись объясняет колонку вины',
        els['sv-note'].innerHTML.includes('к его работе не относятся'));
  check('предупреждение про руководителя сопровождения',
        els['sv-warn'].innerHTML.includes('BITRIX_SUPPORT_HEAD_ID'));
  check('предупреждение про невыясненные причины',
        els['sv-warn'].innerHTML.includes('причина не выяснена'));

  await ctx.loadRefusals();
  const rf = els['rf-rows'].innerHTML;
  check('отказы: 3 строки', (rf.match(/<tr/g) || []).length === 3);
  check('отказ по вине менеджера выделен',
        (rf.match(/class="row-low"/g) || []).length === 1);
  check('источник причины показан рядом', rf.includes('со слов клиента'));
  check('XSS в комментарии обезврежен', !rf.includes('<b>брал'));
  check('без причины, но с задачей — «выясняется»',
        rf.includes('Выясняется, задача №7701'));
  check('без причины и без задачи — «не выяснена»', rf.includes('Не выяснена'));
  check('неизвестный срок жизни не ломает строку', rf.includes('>—<'));
  check('кнопка указания причины есть', rf.includes('askReason(43)'));

  console.log('\n== Дела в зоне риска ==');
  await ctx.loadStuck();
  const rk = els['risk-rows'].innerHTML;
  check('риски: 2 строки', (rk.match(/<tr/g) || []).length === 2);
  check('срок стояния подсвечен', rk.includes('due-late'));
  check('норма стадии показана', rk.includes('31 дн.'));
  check('превышение показано', rk.includes('+178 дн.'));
  check('уже перехваченное помечено', rk.includes('Задача поставлена'));
  check('ожидающее помечено', rk.includes('В очереди'));
  check('объяснение, что норма своя у каждой стадии',
        els['st-risk-note'].innerHTML.includes('медиана') &&
        els['st-risk-note'].innerHTML.includes('Реализация'));

  console.log('\n== Жалобы ==');
  await ctx.loadComplaints();
  const cm = els['cm-rows'].innerHTML;
  check('жалобы: 4 строки', (cm.match(/<tr/g) || []).length === 4);
  check('просроченная выделена строкой',
        (cm.match(/class="row-overdue"/g) || []).length === 1, cm.slice(0, 120));
  check('дата просрочки красным', cm.includes('class="due-late"'));
  check('непросроченные не выделены', !cm.includes('row-overdue">\n          <td>calm@'));
  check('XSS в тексте жалобы обезврежен', !cm.includes('<b>не</b>'), '');
  check('номер задачи показан', cm.includes('№8801'));
  check('ожидающая задача помечена', cm.includes('В очереди'));
  check('застрявшая показывает попытки', cm.includes('Не создана (4)'));
  check('ошибка уходит в подсказку', cm.includes('title="tasks.task.add: HTTP 401"'));
  check('новой доступна кнопка «В работу»', cm.includes("cmStatus(31,'in_progress')"));
  check('взятой в работу кнопки «В работу» нет', !cm.includes("cmStatus(30,'in_progress')"));
  check('у закрытой показан ответ вместо кнопок',
        cm.includes('Обновили сборку') && !cm.includes('cmResolve(29)'));
  check('темы подставлены в фильтр',
        els['cm-category'].innerHTML.includes('Сроки по делу'));
  check('предупреждение о просрочке со ссылкой на закон',
        els['cm-warn'].innerHTML.includes('1</b> жалоб просрочены') &&
        els['cm-warn'].innerHTML.includes('защите прав потребителей'),
        els['cm-warn'].innerHTML);
  check('пагинация жалоб', els['cm-count'].textContent === '1–4 из 4',
        els['cm-count'].textContent);
  check('вкладка «Качество» грузит все свои разделы',
        ['loadQuality()', 'loadNps()', 'loadSupervision()', 'loadRefusals()',
         'loadStuck()', 'loadComplaints()'].every(fn =>
          /quality:\s*\(\)\s*=>\s*\{[^}]*\}/.exec(HTML)[0].includes(fn)), '');

  console.log(`\n${'='.repeat(46)}\n  Пройдено: ${ok}   Провалено: ${fail}\n${'='.repeat(46)}`);
  process.exit(fail ? 1 : 0);
})();
