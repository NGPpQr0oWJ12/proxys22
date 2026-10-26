import { readfile, open, rename, unlink, mkdir, rmdir, stat, lstat, chmod, popen } from 'fs';

const root = '/tmp/proxys22-update';
const helper = '/usr/libexec/wifi-proxy/update';
const repo = 'NGPpQr0oWJ12/proxys22';
const maximum = 16121856;
const backup_maximum = 262144;
const required_paths = ['/etc/config/network', '/etc/config/wireless', '/etc/wifi-proxy/initialized', '/etc/wifi-proxy/client.json', '/etc/wifi-proxy/awg.json'];

function fail(message) { die('WIFI_PROXY_SAFE:' + message); }
function version_valid(value) { return type(value) == 'string' && !!match(value, /^\d{1,9}\.\d{1,9}\.\d{1,9}$/); }
function compare_versions(left, right) {
	if (!version_valid(left) || !version_valid(right)) fail('Не удалось проверить версии прошивки.');
	let a = split(left, '.'), b = split(right, '.');
	for (let i = 0; i < 3; i++) { if (+a[i] > +b[i]) return 1; if (+a[i] < +b[i]) return -1; }
	return 0;
}
function read_json(path, limit) {
	try { return json(readfile(path, limit || 65536) || '{}'); } catch (e) { return {}; }
}
function ensure_root() {
	let info = lstat(root);
	if (info && (info.type != 'directory' || info.uid != 0)) fail('Каталог обновления недоступен. Перезагрузите роутер перед повторной попыткой.');
	if (!info && !mkdir(root, 0o700)) fail('Не удалось создать закрытый каталог обновления.');
	if (!chmod(root, 0o700)) fail('Не удалось защитить каталог обновления.');
}
function save_state(state) {
	state.updated_at = time();
	let path = root + '/state.new', file = open(path, 'w', 0o600);
	if (!file) fail('Не удалось сохранить состояние обновления.');
	let body = sprintf('%J', state), count = file.write(body), closed = file.close();
	if (count != length(body) || !closed || !rename(path, root + '/state.json')) fail('Не удалось сохранить состояние обновления.');
}
function state_read() { return read_json(root + '/state.json'); }
function unlock() { rmdir(root + '/lock'); }
function lock() {
	if (!mkdir(root + '/lock', 0o700)) fail('Другая операция обновления ещё выполняется. Дождитесь её завершения.');
}
function marker() {
	let value = read_json('/etc/proxys22-release', 4096);
	if (value.name != 'proxys22' || !version_valid(value.version)) fail('Обновление доступно только для установленной proxys22.');
	return value.version;
}
function fixed_read(command, limit) {
	let process = popen(command, 'r');
	if (!process) return null;
	let output = process.read(limit), code = process.close();
	return code == 0 ? output : null;
}
function validate_board_info(board) {
	if (board?.board_name != 'keenetic,kn-1112' || board?.release?.target != 'ramips/mt76x8') fail('Модель или платформа устройства не соответствует Keenetic KN-1112. Обновление запрещено.');
}
function board_check() {
	let body = fixed_read('/bin/ubus -t 5 call system board', 8192), board;
	try { board = json(body || '{}'); } catch (e) { fail('Не удалось определить модель роутера.'); }
	validate_board_info(board);
	return marker();
}
function command(arguments, timeout, message) {
	if (system(arguments, timeout) != 0) fail(message);
}
function fetch(url, destination, limit, timeout) {
	command(['/usr/bin/curl', '--fail', '--location', '--silent', '--connect-timeout', '15', '--max-time', '' + timeout,
		'--proto', '=https', '--proto-redir', '=https', '--max-filesize', '' + limit,
		'--user-agent', 'proxys22-updater', '--header', 'Accept: application/vnd.github+json', '--output', destination, url],
		(timeout + 10) * 1000, 'Не удалось загрузить данные с GitHub. Проверьте доступ к интернету.');
	let info = stat(destination);
	if (!info || info.type != 'file' || info.size > limit || !chmod(destination, 0o600)) fail('Загруженные данные превышают допустимый размер или недоступны.');
}
function asset(assets, name, tag, size) {
	let found = [];
	for (let item in assets || []) if (type(item) == 'object' && item.name == name) push(found, item);
	let url = 'https://github.com/' + repo + '/releases/download/' + tag + '/' + name;
	if (length(found) != 1 || found[0].browser_download_url != url ||
		(size != null && (type(found[0].size) != 'int' || found[0].size != size))) fail('Файл выпуска отсутствует или не соответствует проверенному адресу и размеру.');
	return url;
}
function validate_release(metadata, manifest, installed, pinned) {
	if (type(metadata) != 'object' || metadata.draft !== false || metadata.prerelease !== false ||
		type(metadata.assets) != 'array' || type(metadata.tag_name) != 'string' || !match(metadata.tag_name, /^v\d{1,9}\.\d{1,9}\.\d{1,9}$/) ||
		(pinned && metadata.tag_name != pinned)) fail('GitHub не вернул запрошенный стабильный выпуск.');
	if (type(manifest) != 'object' || type(manifest.schema) != 'int' || manifest.schema != 1 || manifest.brand != 'proxys22' ||
		manifest.profile != 'keenetic_kn-1112' || manifest.target != 'ramips/mt76x8' || !version_valid(manifest.version) ||
		metadata.tag_name != 'v' + manifest.version || type(manifest.images) != 'object') fail('Описание выпуска не соответствует proxys22 для Keenetic KN-1112.');
	let image;
	for (let kind in ['factory', 'sysupgrade']) {
		let item = manifest.images[kind], name = 'proxys22-' + manifest.version + '-keenetic_kn-1112-' + kind + '.bin';
		if (type(item) != 'object' || item.name != name || type(item.size) != 'int' || item.size <= 0 || item.size > maximum ||
			type(item.sha256) != 'string' || !match(item.sha256, /^[a-f0-9]{64}$/)) fail('Образ выпуска указан некорректно или превышает память устройства.');
		let url = asset(metadata.assets, name, metadata.tag_name, item.size);
		if (kind == 'sysupgrade') image = { name, size: item.size, sha256: item.sha256, url };
	}
	if (compare_versions(manifest.version, installed) < 0) fail('Понижение версии прошивки запрещено.');
	return { version: manifest.version, tag: metadata.tag_name, image };
}
function load_candidate(installed, tag) {
	let endpoint = tag ? 'tags/' + tag : 'latest';
	fetch('https://api.github.com/repos/' + repo + '/releases/' + endpoint, root + '/release.json', 524288, 45);
	let metadata = read_json(root + '/release.json', 524289);
	if (type(metadata.assets) != 'array' || type(metadata.tag_name) != 'string' || !match(metadata.tag_name, /^v\d{1,9}\.\d{1,9}\.\d{1,9}$/)) fail('Не удалось прочитать список файлов стабильного выпуска.');
	let url = asset(metadata.assets, 'proxys22-release.json', metadata.tag_name, null);
	fetch(url, root + '/manifest.json', 65536, 45);
	return validate_release(metadata, read_json(root + '/manifest.json', 65537), installed, tag);
}
function stage(state, message) { state.stage = message; state.message = message; save_state(state); }
function image_check(state) {
	let info = stat(root + '/image.bin');
	if (!info || info.type != 'file' || info.size != state.image.size) fail('Размер загруженного образа не совпадает с описанием выпуска.');
	let digest = split(trim(fixed_read('/usr/bin/sha256sum /tmp/proxys22-update/image.bin', 256) || ''), /\s+/)[0];
	if (digest != state.image.sha256) fail('Контрольная сумма SHA-256 прошивки не совпадает. Запись запрещена.');
}
function backup_check(state, timeout) {
	let info = stat(root + '/backup.tar.gz');
	if (!info || info.type != 'file' || info.size <= 0 || info.size > backup_maximum || !chmod(root + '/backup.tar.gz', 0o600))
		fail('Резервная копия недоступна или превышает 256 КиБ. Сохраните настройки через SSH перед обновлением.');
	command([helper, 'verify-backup'], timeout || 90000, 'Резервная копия повреждена. Обновление запрещено.');
	let rows = split(readfile(root + '/backup.list', 65536) || '', '\n'), names = {};
	for (let row in rows) {
		if (substr(row, 0, 1) != '-') continue;
		for (let path in state.required || []) {
			let name = substr(path, 1);
			if (substr(row, -length(name) - 1) == ' ' + name || substr(row, -length(name) - 3) == ' ./' + name) names[path] = true;
		}
	}
	for (let path in state.required || []) if (!names[path]) fail('Резервная копия содержит не все действующие настройки сети, профили и маркер инициализации.');
}
function settings_hash() {
	let body = fixed_read('/usr/libexec/wifi-proxy/update settings-hash', 2048);
	if (!body) fail('Не удалось проверить сохранность действующих настроек.');
	return body;
}
function public_state(state) {
	let installed;
	try { installed = marker(); } catch (e) { installed = ''; }
	let result = { ok: true, phase: state.phase || 'idle', job_id: state.job_id || '', installed_version: installed,
		version: state.version || '', stage: state.stage || '', message: state.message || '', received_bytes: 0,
		size: state.image?.size || 0, backup_downloaded: state.backup_downloaded === true };
	if (state.image && index(['preparing', 'ready', 'upgrading'], state.phase) >= 0) result.received_bytes = stat(root + '/image.bin')?.size || 0;
	return result;
}
function start_worker(mode, state) {
	try { command([helper, 'start-' + mode], 1000, 'Не удалось запустить фоновую подготовку обновления.'); }
	catch (e) { state.phase = 'error'; state.message = 'Не удалось запустить фоновую подготовку обновления.'; state.stage = state.message; save_state(state); unlock(); die(e.message); }
	return { ok: true, job_id: state.job_id };
}
function update_dispatch(method, args) {
	ensure_root();
	let state = state_read();
	if (index(['checking', 'preparing'], state.phase) >= 0 && time() - (state.updated_at || 0) > 1200) {
		state.phase = 'error'; state.message = 'Операция обновления прервана. Повторите проверку выпуска.'; state.stage = state.message; save_state(state); unlock();
	}
	if (method == 'update_status') return public_state(state);
	if (method == 'update_check') {
		if (state.job_id && (index(['checking', 'preparing', 'upgrading'], state.phase) >= 0 ||
			(index(['available', 'current', 'ready'], state.phase) >= 0 && time() - (state.checked_at || 0) < 600))) return { ok: true, job_id: state.job_id };
		lock();
		try {
			let job = trim(readfile('/proc/sys/kernel/random/uuid', 64) || '');
			if (!match(job, /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/)) fail('Не удалось создать номер операции обновления.');
			state = { job_id: job, phase: 'checking', stage: 'Проверка последнего стабильного выпуска', message: 'Проверяем GitHub.', backup_downloaded: false };
			for (let name in ['image.bin', 'backup.tar.gz', 'backup.list']) unlink(root + '/' + name);
			save_state(state);
		} catch (e) { unlock(); die(e.message); }
		return start_worker('check', state);
	}
	if (method == 'update_prepare') {
		if (!version_valid(args.version) || args.version != state.version) fail('Версия выпуска изменилась. Повторите проверку обновлений.');
		if (state.phase == 'preparing' || state.phase == 'ready') return { ok: true, job_id: state.job_id };
		if (state.phase != 'available') fail('Сначала проверьте наличие новой версии прошивки.');
		lock();
		try {
			state = state_read();
			if (state.phase != 'available' || args.version != state.version) fail('Операция обновления изменилась. Повторите проверку.');
			state.phase = 'preparing'; state.backup_downloaded = false;
			stage(state, 'Подготовка обновления');
		} catch (e) { unlock(); die(e.message); }
		return start_worker('prepare', state);
	}
	if (method != 'update_backup' && method != 'update_apply') fail('Команда обновления не поддерживается.');
	if (method == 'update_apply' && state.phase == 'upgrading' && args.job_id == state.job_id &&
		args.confirmation == 'ОБНОВИТЬ' && args.backup_saved === true && state.backup_downloaded === true && state.approved === true)
		return { ok: true, job_id: state.job_id };
	if (type(args.job_id) != 'string' || args.job_id != state.job_id || state.phase != 'ready') fail('Подготовленная операция недоступна. Повторите проверку обновлений.');
	if (method == 'update_apply' && (args.confirmation != 'ОБНОВИТЬ' || args.backup_saved !== true || state.backup_downloaded !== true))
		fail('Для записи введите ОБНОВИТЬ и подтвердите сохранение скачанной резервной копии.');
	lock();
	let fatal = false;
	try {
		state = state_read();
		if (state.phase != 'ready' || state.job_id != args.job_id) fail('Операция обновления изменилась. Повторите проверку.');
		if (method == 'update_apply' && (args.confirmation != 'ОБНОВИТЬ' || args.backup_saved !== true || state.backup_downloaded !== true))
			fail('Для записи введите ОБНОВИТЬ и подтвердите сохранение скачанной резервной копии.');
		fatal = true;
		if (method == 'update_backup') {
			backup_check(state);
			let body = readfile(root + '/backup.tar.gz', backup_maximum + 1);
			if (!body || length(body) > backup_maximum) fail('Не удалось прочитать закрытую резервную копию.');
			let encoded = b64enc(body);
			if (!encoded) fail('Не удалось подготовить резервную копию для скачивания.');
			let filename = 'proxys22-backup-' + marker() + '-' + state.job_id + '.tar.gz';
			state.backup_downloaded = true; save_state(state); unlock();
			return { ok: true, filename, base64: encoded };
		}
		let installed = board_check();
		if (compare_versions(state.version, installed) <= 0) fail('Этот выпуск больше не подходит для обновления установленной версии.');
		if (settings_hash() != state.settings_hash) fail('Настройки изменились после подготовки. Подготовьте обновление заново и сохраните новую резервную копию.');
		image_check(state); backup_check(state, 15000);
		command([helper, 'test-image'], 60000, 'Повторная проверка sysupgrade -T завершилась ошибкой.');
		state.approved = true;
		state.phase = 'upgrading'; stage(state, 'Повторная проверка перед записью прошивки');
	} catch (e) {
		if (fatal && state.phase == 'ready') {
			let raw = e.message || '', prefix = 'WIFI_PROXY_SAFE:';
			let message = index(raw, prefix) == 0 ? substr(raw, length(prefix)) : '';
			state.phase = 'error'; state.backup_downloaded = false; state.approved = false;
			state.message = message && length(message) <= 280 ? message : 'Проверка подготовленного обновления прервана. Повторите проверку выпуска.';
			state.stage = state.message;
			try { save_state(state); } catch (ignored) {}
		}
		unlock(); die(e.message);
	}
	return start_worker('apply', state);
}
function run_update(arguments) {
	let state = state_read(), trusted = false;
	try {
		ensure_root();
		trusted = true;
		if (!state.job_id || !lstat(root + '/lock')) fail('Операция обновления не была подтверждена.');
		let installed = board_check(), mode = arguments[0];
		if (mode == 'check') {
			let candidate = load_candidate(installed, null);
			state.version = candidate.version; state.tag = candidate.tag; state.image = candidate.image; state.checked_at = time();
			state.phase = compare_versions(state.version, installed) == 0 ? 'current' : 'available';
			stage(state, state.phase == 'current' ? 'Установлена последняя стабильная версия.' : 'Доступна новая версия прошивки.');
		}
		else if (mode == 'prepare') {
			if (state.phase != 'preparing' || compare_versions(state.version, installed) <= 0) fail('Этот выпуск не подходит для обновления установленной версии.');
			stage(state, 'Повторная проверка выбранного выпуска');
			let candidate = load_candidate(installed, state.tag);
			if (candidate.image.sha256 != state.image.sha256 || candidate.image.size != state.image.size) fail('Файлы выпуска изменились. Повторите проверку обновлений.');
			state.required = [];
			for (let path in required_paths) if (stat(path)?.type == 'file') push(state.required, path);
			if (index(state.required, '/etc/config/network') < 0 || index(state.required, '/etc/wifi-proxy/initialized') < 0)
				fail('Отсутствуют настройки сети или маркер инициализации. Подготовка обновления запрещена.');
			stage(state, 'Загрузка образа прошивки');
			fetch(state.image.url, root + '/image.bin', state.image.size, 600);
			stage(state, 'Проверка размера и SHA-256 прошивки'); image_check(state);
			stage(state, 'Проверка совместимости: sysupgrade -T');
			command([helper, 'test-image'], 120000, 'Роутер отклонил образ при проверке sysupgrade -T.');
			stage(state, 'Создание и проверка резервной копии настроек');
			state.settings_hash = settings_hash();
			command([helper, 'backup'], 90000, 'Не удалось создать резервную копию настроек.'); backup_check(state);
			if (settings_hash() != state.settings_hash) fail('Настройки изменились во время резервного копирования. Повторите подготовку.');
			state.phase = 'ready'; stage(state, 'Образ и резервная копия проверены. Скачайте копию и подтвердите запись.');
		}
		else if (mode == 'apply') {
			if (state.phase != 'upgrading' || state.approved !== true || state.backup_downloaded !== true || compare_versions(state.version, installed) <= 0) fail('Обновление не было подтверждено или версия больше не подходит.');
			image_check(state);
			stage(state, 'Команда записи принята. Сохраняйте питание до перезагрузки.');
			if (system([helper, 'flash']) != 0) fail('Запись прошивки не запустилась. Проверьте состояние роутера.');
		}
		else fail('Команда фонового обновления не поддерживается.');
		unlock(); return 0;
	} catch (e) {
		if (!trusted) return 1;
		let raw = e.message || '', prefix = 'WIFI_PROXY_SAFE:';
		if (state.phase != 'upgrading') unlink(root + '/image.bin');
		state.phase = 'error'; state.backup_downloaded = false;
		state.message = index(raw, prefix) == 0 ? substr(raw, length(prefix)) : 'Подготовка обновления прервана. Проверьте подключение и повторите попытку.';
		state.stage = state.message;
		try { save_state(state); } catch (ignored) {}
		unlock(); return 1;
	}
}

export { update_dispatch, run_update, validate_release, validate_board_info, compare_versions };
