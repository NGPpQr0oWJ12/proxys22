(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const query = new URLSearchParams(location.search);
  const demo = query.get('demo') === '1';
  const demoFallback = demo && query.get('fallback') === '1';
  const zeroSession = '00000000000000000000000000000000';
  const sessionKey = 'proxys22.session';
  let session = '', requestId = 0, refreshId = 0, busy = false, seeded = false, scanning = false, status = null;
  let networks = [], history = [], deviceButtons = [], demoStep = 0, demoEpoch = Math.floor(Date.now() / 1000);
  const demoStatus = {
    system: { model: 'KN-1112', version: '0.4.0', firmware: 'proxys22 0.4.0', uptime: 98124 },
    tunnel: { protocol: 'amneziawg', configured: true, enabled: true, running: true, mode: demoFallback ? 'direct' : 'proxy', healthy: !demoFallback, reason: demoFallback ? 'unavailable' : 'healthy' },
    uplink: { mode: 'ethernet', connected: true }, wifi: { ssid: 'Home Network' },
    devices: [
      { mac: '02:00:00:00:00:11', name: 'Телефон', ip: '192.0.2.11', connection: 'wifi', blocked: false, connected: true },
      { mac: '02:00:00:00:00:22', name: 'Ноутбук', ip: '192.0.2.22', connection: 'ethernet', blocked: false, connected: true },
      { mac: '02:00:00:00:00:33', name: 'Телевизор', ip: '192.0.2.33', connection: 'wifi', blocked: true, connected: false }
    ]
  };
  const demoNetworks = [
    { ssid: 'Home Wi-Fi', signal: 85, security: 'WPA2' },
    { ssid: 'Guest Network', signal: 68, security: 'WPA2' },
    { ssid: 'Studio', signal: 51, security: 'WPA3' }
  ];

  function errorAt(id, message = '') {
    $(id).textContent = message;
    $(id).hidden = !message;
  }
  function clearSecret(id) {
    const field = $(id); field.value = '';
    if (field.tagName === 'INPUT') field.type = 'password';
  }
  function resetEye() {
    $('admin-password').type = 'password';
    $('password-eye').setAttribute('aria-pressed', 'false');
    $('password-eye').setAttribute('aria-label', 'Показать пароль');
    $('eye-slash').hidden = true;
  }
  function clearSecrets() {
    document.querySelectorAll('[data-secret]').forEach(field => clearSecret(field.id));
    resetEye();
  }
  function closePaste() {
    clearSecret('profile-uri');
    if ($('paste-dialog').open) $('paste-dialog').close();
  }
  function setSession(token) {
    session = token;
    if (demo) return;
    try {
      if (token) sessionStorage.setItem(sessionKey, token);
      else sessionStorage.removeItem(sessionKey);
    } catch (_) {}
  }
  function showLogin() {
    setSession(''); status = null; seeded = false; scanning = false; history = []; networks = [];
    clearSecrets(); closePaste();
    for (const id of ['login-error', 'status-error', 'proxy-error', 'internet-error', 'wifi-error', 'paste-error', 'devices-error']) errorAt(id);
    $('upstream-network').value = ''; $('upstream-fields').hidden = true;
    $('topbar').hidden = true;
    $('dashboard').hidden = true;
    $('login-view').hidden = false;
    $('admin-password').focus();
  }
  function showDashboard() {
    $('login-view').hidden = true;
    $('topbar').hidden = false;
    $('dashboard').hidden = false;
    $('demo-badge').hidden = !demo;
    scanNetworks();
  }
  function safeMessage(error, secrets = []) {
    let message = error.message || 'Не удалось выполнить запрос.';
    for (const secret of secrets.filter(Boolean)) {
      message = message.split(secret).join('•••');
      try {
        const uri = new URL(secret);
        for (const part of [uri.username, uri.password]) {
          if (part) message = message.split(decodeURIComponent(part)).join('•••');
        }
      } catch (_) {}
    }
    return message.replace(/(?:hy2|hysteria2):\/\/\S+/gi, '[скрытая ссылка]').slice(0, 250);
  }
  async function rpc(object, method, args, token = session) {
    if (demo) throw new Error('Сетевые запросы отключены в деморежиме.');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/ubus', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ jsonrpc: '2.0', id: ++requestId, method: 'call', params: [token, object, method, args] })
      });
      if (response.status === 401 || response.status === 403) {
        if (token === session || (token === zeroSession && !session)) showLogin();
        throw new Error('Сессия завершена. Войдите снова.');
      }
      if (!response.ok) throw new Error('Роутер вернул ошибку. Попробуйте ещё раз.');
      const data = await response.json();
      if (data.error) throw new Error(data.error.message || 'Ошибка запроса к роутеру.');
      if (!Array.isArray(data.result)) throw new Error('Некорректный ответ роутера.');
      if (data.result[0] !== 0) {
        if (data.result[0] === 6) {
          const loggedIn = !!session;
          if (token === session || (token === zeroSession && !session)) showLogin();
          throw new Error(loggedIn ? 'Сессия завершена. Войдите снова.' : 'Неверный пароль или доступ запрещён.');
        }
        throw Object.assign(new Error('Команда недоступна на этой версии роутера.'), { code: data.result[0] });
      }
      const result = data.result[1] || {};
      if (result.ok === false || result.error) throw new Error(result.message || 'Настройки не применены.');
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Роутер не ответил. Проверьте подключение к его сети.');
      if (error instanceof TypeError) throw new Error('Нет связи с роутером.');
      if (error instanceof SyntaxError) throw new Error('Некорректный ответ роутера.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function selectedNetwork() { return networks.find((network, index) => String(index) === $('upstream-network').value); }
  function secured(network) { return network && !['', 'open', 'none'].includes(String(network.security || '').toLowerCase()); }
  function updateNetworkFields() {
    const network = selectedNetwork();
    $('upstream-fields').hidden = !network;
    $('upstream-password-field').hidden = !secured(network);
    $('upstream-password').required = !!secured(network);
    $('upstream-password').disabled = busy || !secured(network);
  }
  function setBusy(value) {
    busy = value;
    document.querySelectorAll('button:not(#logout), input, select, textarea').forEach(control => { control.disabled = value; });
    $('dashboard').setAttribute('aria-busy', String(value));
    $('login-form').setAttribute('aria-busy', String(value));
    if (!value) {
      $('toggle').disabled = !status || !(status.tunnel || {}).configured;
      $('scan-refresh').disabled = scanning;
      $('upstream-network').disabled = scanning || !networks.length;
      updateNetworkFields();
      updateDeviceControls();
    }
  }
  function routeState(tunnel, uplink) {
    const known = tunnel.mode === 'proxy' || tunnel.mode === 'direct';
    const mode = known ? tunnel.mode : tunnel.enabled && tunnel.running ? 'proxy' : 'direct';
    const fallback = known && mode === 'direct' && tunnel.enabled && tunnel.configured;
    let title = mode === 'proxy' ? 'Через прокси' : fallback ? 'Резервное подключение' : 'Обычный интернет';
    let description = mode === 'proxy' ? `Маршрут через ${tunnel.protocol === 'amneziawg' ? 'AmneziaWG' : 'Hysteria 2'}.` : 'Прямое подключение к провайдеру.';
    if (!tunnel.configured) description = 'Прокси не настроен.';
    else if (tunnel.reason === 'checking') description = 'Проверяем доступность прокси.';
    else if (fallback) description = 'Прокси недоступен. Используется прямое подключение.';
    if (!known) {
      title = tunnel.enabled ? 'Прокси включён' : 'Прокси выключен';
      description = 'Маршрут и доступность ещё не подтверждены.';
    }
    if (uplink.connected === false) { title = 'Нет внешнего подключения'; description = 'Проверьте подключение к внешней сети.'; }
    return { known, mode, fallback, title, description };
  }
  function formatUptime(seconds) {
    seconds = Math.max(0, Number(seconds) || 0);
    const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return (days ? days + ' д ' : '') + (hours ? hours + ' ч ' : '') + minutes + ' мин';
  }
  function metric(value) { return value !== null && value !== '' && value !== undefined && Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : null; }
  function rate(value) { return value === null ? '—' : (value / 1000000).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' Мбит/с'; }
  function chartPath(key) {
    if (!history.length) return '';
    const peak = Math.max(1, ...history.flatMap(point => [point.rx, point.tx]));
    const points = history.map((point, index) => [history.length > 1 ? index * 600 / (history.length - 1) : 0, 129 - point[key] / peak * 115]);
    let path = 'M' + points[0].join(',');
    for (let index = 1; index < points.length; index++) {
      const previous = points[index - 1], next = points[index];
      path += ' Q' + previous.join(',') + ' ' + [(previous[0] + next[0]) / 2, (previous[1] + next[1]) / 2].join(',');
    }
    return path + ' L' + points[points.length - 1].join(',');
  }
  function renderMetrics() {
    const metrics = (status || {}).metrics || {}, system = (status || {}).system || {};
    const cpu = metric(metrics.cpu_percent ?? system.cpu_percent), rx = metric(metrics.rx_bps), tx = metric(metrics.tx_bps);
    $('cpu-value').textContent = cpu === null ? '—' : Math.min(100, cpu).toFixed(0) + '%';
    $('cpu-meter').hidden = cpu === null;
    $('cpu-meter').value = cpu === null ? 0 : Math.min(100, cpu);
    $('download-rate').textContent = rate(rx); $('upload-rate').textContent = rate(tx);
    if (rx !== null && tx !== null) {
      const time = metric(metrics.sampled_at) ?? Date.now() / 1000;
      if (!history.length || history[history.length - 1].time !== time) history.push({ rx, tx, time });
      else history[history.length - 1] = { rx, tx, time };
      if (history.length > 60) history.splice(0, history.length - 60);
    } else history = [];
    $('traffic-empty').hidden = !!history.length;
    $('download-path').setAttribute('d', chartPath('rx'));
    $('upload-path').setAttribute('d', chartPath('tx'));
  }
  function syntheticMetrics(step) {
    return { cpu_percent: 18 + Math.sin(step / 6) * 5, rx_bps: (9 + Math.sin(step / 3) * 3 + Math.cos(step / 7)) * 1000000, tx_bps: (1.7 + Math.sin(step / 4) * .6) * 1000000, sampled_at: demoEpoch + step * 5 };
  }
  function seedDemo() {
    history = []; demoStep = 0; demoEpoch = Math.floor(Date.now() / 1000);
    for (let step = -19; step <= 0; step++) {
      const sample = syntheticMetrics(step);
      history.push({ rx: sample.rx_bps, tx: sample.tx_bps, time: sample.sampled_at });
    }
    demoStatus.metrics = syntheticMetrics(demoStep);
  }
  function reportedVersion(system) {
    for (const value of [system.version, system.firmware]) {
      if (typeof value !== 'string') continue;
      const match = value.match(/(?:^|[^0-9.])v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?=$|[^0-9A-Za-z.+-])/);
      if (match) return match[1];
    }
    return '—';
  }
  function canManageDevices() { return !!session && (demo || !!(status && status.capabilities && status.capabilities.set_device_blocked === true)); }
  function validMac(mac) { return typeof mac === 'string' && /^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(mac); }
  function updateDeviceControls() {
    deviceButtons.forEach(button => { button.disabled = busy || !canManageDevices() || !validMac(button.dataset.mac); });
  }
  function renderDevices() {
    const devices = Array.isArray(status.devices) ? status.devices.filter(device => device && typeof device === 'object').slice(0, 200) : [];
    const focusedMac = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.mac : '';
    deviceButtons = [];
    const rows = devices.map(device => {
      const row = document.createElement('div'); row.className = 'device-row'; row.setAttribute('role', 'row');
      const cell = (text, className) => {
        const element = document.createElement('div'); element.className = className; element.setAttribute('role', 'cell'); element.textContent = text; row.append(element); return element;
      };
      const name = typeof device.name === 'string' && device.name.trim() ? device.name : 'Устройство';
      cell(name, 'device-name');
      const address = cell(typeof device.ip === 'string' && device.ip ? device.ip : 'IP не назначен', 'device-address');
      const mac = document.createElement('span'); mac.className = 'device-mac small-label'; mac.textContent = validMac(device.mac) ? device.mac : 'MAC неизвестен'; address.append(mac);
      cell(device.connection === 'wifi' ? 'Wi-Fi' : ['ethernet', 'lan'].includes(device.connection) ? 'LAN' : '—', 'device-connection');
      cell(device.blocked === true ? 'Заблокировано' : device.connected === true ? 'Подключено' : device.connected === false ? 'Не в сети' : 'Неизвестно', 'device-state' + (device.blocked === true ? ' blocked' : device.connected === true ? ' connected' : ''));
      const control = cell('', 'device-action');
      const button = document.createElement('button'); button.type = 'button'; button.className = 'button'; button.dataset.mac = typeof device.mac === 'string' ? device.mac : '';
      button.textContent = device.blocked === true ? 'Разблокировать' : 'Блокировать';
      button.setAttribute('aria-label', button.textContent + ': ' + name);
      button.addEventListener('click', () => {
        if (canManageDevices() && validMac(device.mac)) action('set_device_blocked', { mac: device.mac, blocked: device.blocked !== true }, 'devices-error');
      });
      deviceButtons.push(button); control.append(button); return row;
    });
    $('device-list').replaceChildren(...rows);
    $('device-count').textContent = devices.length ? String(devices.length) : '';
    $('devices-empty').hidden = !!devices.length; $('device-table-head').hidden = !devices.length;
    $('devices-note').textContent = demo || canManageDevices() ? '' : 'Блокировка недоступна в этой версии.';
    $('devices-note').hidden = !$('devices-note').textContent;
    updateDeviceControls();
    const focused = deviceButtons.find(button => button.dataset.mac === focusedMac);
    if (focused) focused.focus({ preventScroll: true });
  }
  function render() {
    const system = status.system || {}, tunnel = status.tunnel || {}, uplink = status.uplink || {};
    const route = routeState(tunnel, uplink);
    $('firmware').textContent = 'proxys22 ' + reportedVersion(system);
    $('uptime').textContent = system.uptime == null ? '—' : formatUptime(system.uptime);
    $('tunnel-title').textContent = route.title;
    $('tunnel-description').textContent = route.description;
    $('toggle').setAttribute('aria-checked', String(!!tunnel.enabled));
    $('toggle').disabled = busy || !tunnel.configured;
    $('tunnel-dot').className = 'dot ' + (uplink.connected === false ? 'off' : route.known && route.mode === 'proxy' && tunnel.healthy === true ? 'on' : route.fallback ? 'off' : '');
    $('proxy-state').textContent = !tunnel.configured ? 'Профиль не добавлен' : route.fallback ? 'Резервное подключение' : !route.known ? 'Доступность не проверена' : !tunnel.enabled ? 'Выключен' : tunnel.healthy === true ? 'Подключён' : tunnel.reason === 'checking' ? 'Проверяем подключение' : 'Недоступен';
    $('active-uplink').hidden = uplink.mode !== 'wifi';
    $('active-uplink-name').textContent = uplink.ssid || (uplink.connected === false ? 'Wi-Fi не подключён' : 'Подключено по Wi-Fi');
    if (!seeded) { $('downstream-ssid').value = (status.wifi || {}).ssid || ''; seeded = true; }
    renderMetrics();
    renderDevices();
  }
  function staleStatus(error) {
    if (!session) { errorAt('login-error', safeMessage(error)); return; }
    status = null;
    $('tunnel-title').textContent = 'Состояние неизвестно';
    $('tunnel-description').textContent = safeMessage(error);
    $('tunnel-dot').className = 'dot';
    $('proxy-state').textContent = 'Состояние неизвестно';
    $('active-uplink').hidden = true;
    $('toggle').disabled = true;
    renderMetrics();
    updateDeviceControls();
  }
  async function refresh() {
    const token = session, current = ++refreshId;
    const next = demo ? demoStatus : await rpc('wifi_proxy', 'status', {});
    if (!session || token !== session || current !== refreshId) return;
    status = next; render();
  }
  async function restoreSession() {
    try { session = sessionStorage.getItem(sessionKey) || ''; } catch (_) {}
    if (!session) { showLogin(); return; }
    $('login-view').hidden = true;
    setBusy(true);
    try {
      await refresh(); if (session) showDashboard();
    } catch (error) {
      if (session) showDashboard();
      staleStatus(error);
    } finally { setBusy(false); }
  }
  async function action(method, args, target, secrets = []) {
    if (busy || !session) return false;
    if (method === 'set_device_blocked' && !canManageDevices()) { errorAt(target, 'Блокировка недоступна в этой версии.'); return false; }
    const token = session;
    errorAt(target); refreshId++; setBusy(true);
    try {
      if (demo) {
        if (method === 'set_enabled') Object.assign(demoStatus.tunnel, { enabled: args.enabled, running: args.enabled, mode: args.enabled && !demoFallback ? 'proxy' : 'direct', healthy: args.enabled && !demoFallback, reason: !args.enabled ? 'disabled' : demoFallback ? 'unavailable' : 'healthy' });
        if (method === 'import_profile' || method === 'import_awg') Object.assign(demoStatus.tunnel, { configured: true, protocol: method === 'import_awg' ? 'amneziawg' : 'hysteria' });
        if (method === 'set_uplink') demoStatus.uplink = { mode: args.mode, connected: true, ssid: args.ssid || '' };
        if (method === 'set_wifi') demoStatus.wifi.ssid = args.ssid;
        if (method === 'set_device_blocked') {
          const device = demoStatus.devices.find(item => item.mac === args.mac);
          if (device) device.blocked = args.blocked === true;
        }
      } else {
        const result = await rpc('wifi_proxy', method, args);
        if (result.ok !== true) throw new Error('Роутер не подтвердил применение настроек.');
      }
      if (session !== token) return false;
      try { await refresh(); }
      catch (error) { staleStatus(error); errorAt(session ? target : 'login-error', 'Настройки отправлены. ' + safeMessage(error, secrets)); }
      return true;
    } catch (error) { errorAt(session ? target : 'login-error', safeMessage(error, secrets)); return false; }
    finally { setBusy(false); }
  }
  async function scanNetworks() {
    if (!session || scanning) return;
    const token = session, selected = selectedNetwork()?.ssid;
    scanning = true; $('scan-refresh').disabled = true; $('upstream-network').disabled = true; errorAt('internet-error');
    try {
      const result = demo ? { networks: demoNetworks } : await rpc('wifi_proxy', 'scan_wifi', {});
      if (!session || session !== token) return;
      if (!Array.isArray(result.networks)) throw new Error('Роутер не вернул список сетей.');
      networks = result.networks.filter(network => typeof network.ssid === 'string' && network.ssid.length > 0).slice(0, 100);
      $('upstream-network').replaceChildren();
      const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = networks.length ? 'Выберите сеть' : 'Сети не найдены';
      $('upstream-network').append(placeholder);
      networks.forEach((network, index) => {
        const option = document.createElement('option'); option.value = String(index); option.textContent = network.ssid;
        $('upstream-network').append(option);
      });
      $('upstream-network').value = selected ? String(networks.findIndex(network => network.ssid === selected)) : '';
      updateNetworkFields();
    } catch (error) {
      if (!session) errorAt('login-error', safeMessage(error));
      else if (session === token) errorAt('internet-error', [1, 3, 4].includes(error.code) ? 'Поиск Wi-Fi сетей недоступен на этой версии роутера.' : safeMessage(error));
    } finally { scanning = false; $('scan-refresh').disabled = busy; $('upstream-network').disabled = busy || !networks.length; }
  }
  function openPaste() {
    errorAt('paste-error'); clearSecret('profile-uri');
    $('paste-dialog').showModal(); $('profile-uri').focus();
  }
  async function importProfile(uri, target) {
    uri = uri.trim();
    if (/^\s*\[Interface\]\s*(?:#.*)?$/m.test(uri)) {
      if (uri.length > 8192) { errorAt(target, 'Конфигурация слишком большая.'); return false; }
      return action('import_awg', { config: uri }, target, [uri]);
    }
    try {
      const parsed = new URL(uri);
      if (uri.length > 8192 || !['hy2:', 'hysteria2:'].includes(parsed.protocol) || !parsed.hostname) throw new Error();
    } catch (_) { errorAt(target, 'Вставьте конфиг AmneziaWG (.conf) или ссылку Hysteria 2.'); return false; }
    return action('import_profile', { uri }, target, [uri]);
  }

  $('password-eye').addEventListener('click', () => {
    const reveal = $('admin-password').type === 'password';
    $('admin-password').type = reveal ? 'text' : 'password';
    $('password-eye').setAttribute('aria-pressed', String(reveal));
    $('password-eye').setAttribute('aria-label', reveal ? 'Скрыть пароль' : 'Показать пароль');
    $('eye-slash').hidden = !reveal;
  });
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    let password = $('admin-password').value;
    clearSecret('admin-password'); resetEye(); errorAt('login-error');
    if (!password) { errorAt('login-error', 'Введите пароль администратора.'); return; }
    setBusy(true);
    try {
      const result = demo ? { ubus_rpc_session: 'demo' } : await rpc('session', 'login', { username: 'root', password }, zeroSession);
      if (!result.ubus_rpc_session) throw new Error('Не удалось создать сессию.');
      setSession(result.ubus_rpc_session);
      if (demo) seedDemo();
      await refresh(); if (session) showDashboard();
    } catch (error) { showLogin(); errorAt('login-error', safeMessage(error, [password])); }
    finally { password = ''; setBusy(false); }
  });
  $('logout').addEventListener('click', () => {
    const token = session; showLogin(); errorAt('login-error'); setBusy(false);
    if (!demo && token) rpc('session', 'destroy', {}, token).catch(() => {});
  });
  $('toggle').addEventListener('click', () => { if (status) action('set_enabled', { enabled: !(status.tunnel || {}).enabled }, 'status-error'); });
  $('config-import').addEventListener('click', () => {
    if (busy || !session) return;
    errorAt('proxy-error'); openPaste();
  });
  $('clipboard-import').addEventListener('click', async () => {
    if (busy || !session) return;
    const token = session; errorAt('proxy-error');
    if (!navigator.clipboard || typeof navigator.clipboard.readText !== 'function') { openPaste(); return; }
    let uri = '';
    try { uri = await navigator.clipboard.readText(); }
    catch (_) { if (session === token) openPaste(); return; }
    if (session === token) await importProfile(uri, 'proxy-error');
    uri = '';
  });
  $('profile-form').addEventListener('submit', async event => {
    event.preventDefault(); let uri = $('profile-uri').value; clearSecret('profile-uri');
    if (await importProfile(uri, 'paste-error')) closePaste();
    uri = '';
  });
  $('paste-close').addEventListener('click', closePaste);
  $('paste-dialog').addEventListener('close', () => clearSecret('profile-uri'));
  $('paste-dialog').addEventListener('cancel', () => clearSecret('profile-uri'));
  $('scan-refresh').addEventListener('click', scanNetworks);
  $('upstream-network').addEventListener('change', () => { clearSecret('upstream-password'); errorAt('internet-error'); updateNetworkFields(); });
  $('uplink-form').addEventListener('submit', event => {
    event.preventDefault(); const network = selectedNetwork();
    if (!network) { errorAt('internet-error', 'Выберите Wi-Fi сеть.'); return; }
    const password = $('upstream-password').value; clearSecret('upstream-password');
    if (secured(network) && password.length < 8) { errorAt('internet-error', 'Пароль должен содержать не менее 8 символов.'); return; }
    action('set_uplink', { mode: 'wifi', ssid: network.ssid, security: network.security, password: secured(network) ? password : '' }, 'internet-error', [password]);
  });
  $('uplink-disconnect').addEventListener('click', () => { clearSecret('upstream-password'); action('set_uplink', { mode: 'ethernet' }, 'internet-error'); });
  $('wifi-form').addEventListener('submit', event => {
    event.preventDefault(); const ssid = $('downstream-ssid').value, password = $('downstream-password').value; clearSecret('downstream-password');
    if (!ssid.trim() || password.length < 8) { errorAt('wifi-error', 'Укажите название сети и пароль от 8 символов.'); return; }
    action('set_wifi', { ssid, password }, 'wifi-error', [password]);
  });
  setInterval(async () => {
    if (!session || busy || document.hidden) return;
    if (demo) { demoStatus.metrics = syntheticMetrics(++demoStep); demoStatus.system.uptime += 5; }
    try { await refresh(); } catch (error) { staleStatus(error); }
  }, 5000);
  window.addEventListener('pagehide', () => { clearSecrets(); closePaste(); });
  updateNetworkFields();
  if (demo && query.get('view') !== 'login') {
    session = 'demo'; status = demoStatus; seedDemo(); render(); showDashboard();
  } else if (demo) showLogin();
  else restoreSession();
})();
