(() => {
  'use strict';
  window.createFirmwareUpdater = ({ call, hasSession, installedVersion, online, isDemo, setLocked }) => {
    const $ = id => document.getElementById(id);
    const phases = new Set(['idle', 'checking', 'available', 'current', 'preparing', 'ready', 'upgrading', 'error', 'unsupported']);
    let info = { phase: 'idle' }, pending = false, attempted = false, generation = 0, timer = 0, locked = false;
    let backupJob = '', requestError = '';
    const active = () => ['checking', 'preparing'].includes(info.phase);
    const version = value => typeof value === 'string' && /^\d+\.\d+\.\d+$/.test(value) ? value : '';
    const ready = () => info.phase === 'ready' && !!backupJob && backupJob === info.job_id;
    const canApply = () => ready() && $('update-backup-saved').checked && $('update-confirmation').value.trim().toUpperCase() === 'ОБНОВИТЬ';

    function clearConfirmation() {
      backupJob = ''; $('update-backup-saved').checked = false; $('update-confirmation').value = '';
    }
    function render() {
      const phase = info.phase, preparing = phase === 'preparing', upgrading = phase === 'upgrading';
      const latest = version(info.version), installed = version(info.installed_version) || version(installedVersion());
      const available = ['available', 'ready'].includes(phase) && latest && latest !== installed;
      $('update-installed').textContent = installed || '—';
      $('update-latest').textContent = latest || (phase === 'checking' ? 'Проверяем…' : '—');
      $('update-available').hidden = !available && !preparing;
      $('update-available').textContent = preparing ? 'Подготовка обновления…' : phase === 'ready' ? `Готова ${latest}` : `Доступна ${latest}`;
      const messages = {
        idle: 'Проверьте наличие новой стабильной версии на GitHub.',
        checking: 'Проверяем стабильный релиз на GitHub…',
        available: 'Доступна новая версия. Подготовка загрузит образ и проверит совместимость, без записи прошивки.',
        current: 'Установлена актуальная стабильная версия.',
        preparing: 'Загружаем и проверяем образ. Подготовка может продолжаться в фоне.',
        ready: 'Образ и резервная копия проверены. До подтверждения прошивка не записывается.',
        upgrading: 'Команда обновления принята. Сохраняйте питание роутера. После записи и перезагрузки откройте панель заново и проверьте версию.',
        error: 'Подготовка не завершена. Установка не запущена.',
        unsupported: 'Эта версия прошивки не поддерживает обновление из панели. Используйте терминальную утилиту для первого обновления.',
      };
      $('update-stage').textContent = typeof info.message === 'string' && phase !== 'upgrading' ? info.message.slice(0, 300) : messages[phase];
      $('update-error').textContent = requestError;
      $('update-error').hidden = !requestError;
      $('update-transfer').hidden = !preparing || !(Number(info.size) > 0);
      const percent = Math.min(100, Math.max(0, Math.floor(Number(info.received_bytes || 0) * 100 / (Number(info.size) || 1))));
      $('update-progress').value = percent; $('update-percent').textContent = percent + '%';
      $('update-check').hidden = upgrading;
      $('update-actions').hidden = phase === 'ready' || upgrading;
      $('update-check').disabled = pending || active() || phase === 'ready';
      $('update-prepare').hidden = phase !== 'available';
      $('update-prepare').disabled = pending || !latest;
      $('update-ready').hidden = phase !== 'ready';
      $('update-backup').disabled = pending;
      $('update-backup').textContent = ready() ? 'Скачать копию ещё раз' : 'Скачать резервную копию';
      $('update-backup-saved').disabled = pending || !ready();
      $('update-confirmation').disabled = pending || !ready();
      $('update-apply').disabled = pending || !canApply();
      $('update-close').disabled = locked;
      $('update-reload').hidden = !upgrading;
      $('update-reload').disabled = false;
    }
    function accept(next) {
      if (!next || !phases.has(next.phase)) throw new Error('Роутер вернул некорректное состояние обновления.');
      if (next.job_id !== info.job_id) clearConfirmation();
      if (next.phase === 'upgrading' && !locked) { locked = true; setLocked(true); }
      info = next; requestError = ''; render();
    }
    function fail(error) {
      const unsupported = [1, 3, 4].includes(error.code);
      if (unsupported) info = { phase: 'unsupported' };
      else requestError = info.phase === 'upgrading' ? 'Связь с роутером прервана во время обновления. Сохраняйте питание и проверьте панель после перезагрузки.' : typeof error.message === 'string' && error.message ? error.message.slice(0, 250) : 'Не удалось получить ответ. Проверьте подключение и повторите попытку.';
      render();
    }
    function stopPolling() { clearTimeout(timer); timer = 0; }
    async function poll(expectedGeneration = generation) {
      stopPolling();
      if (!hasSession() || expectedGeneration !== generation) return;
      try {
        const next = await call('update_status', {});
        if (!hasSession() || expectedGeneration !== generation) return;
        accept(next);
        if (active()) timer = setTimeout(() => poll(expectedGeneration), 1000);
      } catch (error) {
        if (!hasSession() || expectedGeneration !== generation) return;
        fail(error);
        if (active()) timer = setTimeout(() => poll(expectedGeneration), 5000);
      }
    }
    async function check() {
      if (!hasSession() || pending || active() || info.phase === 'ready' || info.phase === 'upgrading') return;
      if (isDemo()) { info = { phase: 'current', installed_version: installedVersion(), version: installedVersion(), message: 'Деморежим: прошивка не изменяется, запросы к роутеру отключены.' }; render(); return; }
      attempted = true; pending = true; requestError = ''; clearConfirmation();
      const current = ++generation; stopPolling(); info = { phase: 'checking' }; render();
      try {
        await call('update_check', {});
        if (hasSession() && current === generation) await poll(current);
      } catch (error) {
        if (hasSession() && current === generation) { info = { phase: 'idle' }; fail(error); }
      } finally { if (current === generation) { pending = false; render(); } }
    }
    async function open() {
      if (!hasSession()) return;
      render();
      if (!$('update-dialog').open) $('update-dialog').showModal();
      if (info.phase === 'idle' || info.phase === 'error') await check();
      else if (active()) poll();
    }
    $('firmware').addEventListener('click', open);
    $('update-available').addEventListener('click', open);
    $('update-close').addEventListener('click', () => $('update-dialog').close());
    $('update-dialog').addEventListener('cancel', event => { if (locked) event.preventDefault(); });
    $('update-check').addEventListener('click', check);
    $('update-reload').addEventListener('click', () => location.reload());
    $('update-prepare').addEventListener('click', async () => {
      if (!hasSession() || pending || info.phase !== 'available') return;
      const latest = version(info.version), current = ++generation;
      if (!latest) return;
      pending = true; requestError = ''; clearConfirmation(); stopPolling(); render();
      try {
        await call('update_prepare', { version: latest });
        if (hasSession() && current === generation) await poll(current);
      } catch (error) { if (hasSession() && current === generation) fail(error); }
      finally { if (current === generation) { pending = false; render(); } }
    });
    $('update-backup').addEventListener('click', async () => {
      if (!hasSession() || pending || info.phase !== 'ready') return;
      const job = info.job_id, current = generation;
      pending = true; requestError = ''; render();
      try {
        const result = await call('update_backup', { job_id: job });
        if (!hasSession() || current !== generation || job !== info.job_id) return;
        if (typeof result.base64 !== 'string' || !result.base64.length || result.base64.length > 350000) throw new Error('Некорректная резервная копия.');
        const bytes = Uint8Array.from(atob(result.base64), character => character.charCodeAt(0));
        if (bytes.length < 2 || bytes[0] !== 0x1f || bytes[1] !== 0x8b) throw new Error('Некорректный архив резервной копии.');
        const url = URL.createObjectURL(new Blob([bytes], { type: 'application/gzip' }));
        const link = document.createElement('a');
        link.href = url; link.download = typeof result.filename === 'string' && /^[A-Za-z0-9_.-]+\.tar\.gz$/.test(result.filename) ? result.filename : 'proxys22-backup.tar.gz';
        document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        backupJob = job;
      } catch (error) {
        if (!hasSession() || current !== generation) return;
        if (error.rejected) await poll(current);
        if (hasSession() && current === generation && info.phase !== 'error') requestError = 'Не удалось скачать резервную копию. Установка заблокирована до её сохранения.';
      }
      finally { if (current === generation) { pending = false; render(); } }
    });
    $('update-backup-saved').addEventListener('change', render);
    $('update-confirmation').addEventListener('input', render);
    $('update-apply-form').addEventListener('submit', async event => {
      event.preventDefault();
      if (!hasSession() || pending || !canApply()) return;
      const job = info.job_id, current = ++generation;
      pending = true; locked = true; requestError = ''; stopPolling(); setLocked(true); render();
      try {
        await call('update_apply', { job_id: job, confirmation: 'ОБНОВИТЬ', backup_saved: true }, 120000);
        if (!hasSession() || current !== generation) return;
        info = { ...info, phase: 'upgrading' }; clearConfirmation();
      } catch (error) {
        if (!hasSession() || current !== generation) return;
        if (error.rejected) {
          await poll(current);
          if (!hasSession() || current !== generation) return;
          if (info.phase !== 'error' && info.phase !== 'upgrading') requestError = typeof error.message === 'string' ? error.message.slice(0, 250) : 'Роутер отклонил обновление. Проверьте состояние операции.';
        } else requestError = 'Не удалось подтвердить запуск обновления. Команда могла быть отправлена: сохраняйте питание, проверьте доступность роутера перед повторной попыткой.';
        if (info.phase !== 'upgrading') { locked = false; setLocked(false); }
      } finally { if (current === generation) { pending = false; render(); } }
    });
    return {
      isLocked: () => locked,
      consider() {
        render();
        if (!attempted && hasSession() && online() && !isDemo()) check();
      },
      reset() {
        generation++; stopPolling(); attempted = false; pending = false; info = { phase: 'idle' }; requestError = ''; clearConfirmation();
        if (locked) { locked = false; setLocked(false); }
        if ($('update-dialog').open) $('update-dialog').close();
        render();
      },
    };
  };
})();
