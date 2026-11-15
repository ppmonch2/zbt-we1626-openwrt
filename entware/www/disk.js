'use strict';
const el = id => document.getElementById(id);
const messages = {
  saved_not_started: 'Настройки сохранены. Теперь нажмите «Проверить запись и чтение».',
  write_read_delete_ok: 'Успешно: файл записан, прочитан и удалён. Можно запускать канал.',
  disk_started: 'Программа запущена через Диск. Проверьте её статус и отдельную команду в Алисе.',
  cloud_started: 'Программа запущена через прежнюю очередь.',
  stopped_until_restart: 'Программа остановлена. При перезапуске роутера снова включится выбранный канал.',
  invalid_credentials: 'Проверьте логин, пароль приложения без пробелов и название папки латиницей.',
  check_first: 'Сначала успешно выполните проверку записи и чтения с сохранёнными настройками.',
  busy: 'Другая операция ещё выполняется. Подождите и повторите.',
  stop_failed: 'Программа ещё завершает работу. Подождите и повторите.',
  start_failed: 'Программа не запустилась. Сохраните это сообщение и результат проверки связи.',
  cloud_not_configured: 'Настройки прежней очереди не найдены.',
  probe_cleanup_failed: 'Не удалось удалить тестовый файл. Проверка не завершена.',
  probe_read_failed: 'Не удалось прочитать записанный файл с тем же содержимым.',
  update_router_first: 'Сначала установите обновление программы на роутере.'
};
const statuses = {
  disk_connected_waiting: 'Диск отвечает, ожидаем команды.',
  disk_batch_processed: 'Пакет обработан, результаты записаны на Диск.',
  disk_connection_failed: 'Не удалось получить список команд с Диска.',
  disk_download_failed: 'Не удалось скачать пакет команд.',
  disk_ack_upload_failed: 'Не удалось передать подтверждение. Команда могла выполниться.',
  disk_invalid_batch_rejected: 'Некорректный пакет отклонён.',
  waiting_for_commands: 'Прежняя очередь: ожидание команд.',
  queue_connection_failed: 'Нет связи с прежней очередью.'
};
function explain(code) {
  if (messages[code]) return messages[code];
  const match = /^webdav_http_(\d{3})$/.exec(code || '');
  if (match) {
    const tips = {'000': 'Нет ответа. Проверьте сеть, DNS и время роутера.',
      '401': 'Проверьте логин и пароль приложения WebDAV.',
      '403': 'Проверьте доступ WebDAV для этого аккаунта и тарифа.',
      '409': 'Проверьте служебную папку: вместо папки может существовать файл.',
      '429': 'Сервис ограничил частоту запросов. Повторите позже.',
      '507': 'Недостаточно места на Диске.'};
    return `Ответ WebDAV: ${match[1]}. ${tips[match[1]] || 'Сохраните код ответа для диагностики.'}`;
  }
  return code ? `Ошибка: ${code}` : '';
}
async function request(action, fields = {}) {
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  el('notice').textContent = action === 'check' ? 'Проверка… Подождите до 30 секунд.' : 'Подождите…';
  try {
    const response = await fetch('cgi-bin/disk-api', action === 'status' ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, ...fields}), cache: 'no-store'
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data.ok) { el('notice').textContent = explain(data.error); return; }
    el('notice').textContent = explain(data.message) || 'Статус обновлён.';
    el('connection').textContent = `Канал: ${data.enabled ? 'Яндекс Диск' : 'прежняя очередь'}. ` +
      `Программа ${data.running ? 'запущена' : 'остановлена'}. ` +
      (data.running ? (statuses[data.status] || 'Свежий статус обмена ещё не получен.') : '');
    if (action === 'status' || action === 'save') {
      el('login').value = data.login; el('folder').value = data.folder;
    }
  } catch (_) {
    el('notice').textContent = 'Нет ответа от локального сервера. Проверьте подключение к Wi-Fi роутера и обновите статус.';
  } finally {
    document.querySelectorAll('button').forEach(button => { button.disabled = false; });
  }
}
el('settings').addEventListener('submit', event => {
  event.preventDefault();
  const fields = {login: el('login').value.trim(), password: el('password').value,
    folder: el('folder').value.trim()};
  el('password').value = '';
  request('save', fields);
});
for (const action of ['check', 'start', 'stop', 'use_cloud']) el(action).onclick = () => request(action);
el('refresh').onclick = () => request('status');
request('status');
