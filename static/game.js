/* The server owns progress. This client only selects cells and presents feedback. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const tempers = {WO: 'Woe', FC: 'Frolic', DR: 'Dread', MA: 'Malice'};
  let game = null, csrf = '', busy = false, selected = new Set(), audio = null;
  let sound = false, lastComplete = null, focusedCell = 0;
  const field = $('number-field');

  function preference(key, fallback) {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  }
  function savePreference(key, value) {
    try { localStorage.setItem(key, value); } catch { /* Private browsing can disallow storage. */ }
  }
  let still = preference('mdr-still', matchMedia('(prefers-reduced-motion: reduce)').matches ? '1' : '0') === '1';
  function updateMotion() {
    document.body.classList.toggle('still', still);
    $('motion-button').textContent = still ? 'Motion off' : 'Motion on';
    $('motion-button').setAttribute('aria-pressed', String(still));
  }
  updateMotion();

  function say(message, kind = '') {
    $('message').textContent = message;
    $('message').className = kind;
  }

  function tone(success = true) {
    if (!sound || !audio) return;
    try {
      const oscillator = audio.createOscillator(), gain = audio.createGain();
      oscillator.type = 'sine';
      oscillator.frequency.value = success ? 392 : 147;
      gain.gain.setValueAtTime(.035, audio.currentTime);
      gain.gain.exponentialRampToValueAtTime(.001, audio.currentTime + .24);
      oscillator.connect(gain).connect(audio.destination);
      oscillator.start(); oscillator.stop(audio.currentTime + .25);
    } catch { /* Sound is optional. */ }
  }

  function signalForSelection() {
    return game?.cells.find((cell) => selected.has(cell.id))?.signal;
  }

  function reveal(signal) {
    field.querySelectorAll('.number').forEach((element) => {
      element.classList.toggle('revealed', Boolean(signal && element.dataset.cluster === String(signal.cluster)));
    });
    $('signal').textContent = signal ? `${tempers[signal.temper]} / route to bin ${String(signal.route).padStart(2, '0')}` : 'Awaiting recognition';
    $('signal-light').classList.toggle('active', Boolean(signal));
  }

  function updateSelection() {
    field.querySelectorAll('.number').forEach((element) => {
      const active = selected.has(Number(element.dataset.id));
      element.classList.toggle('selected', active);
      element.setAttribute('aria-pressed', String(active));
    });
    $('selection-count').hidden = selected.size === 0;
    $('selection-count').textContent = `${selected.size} / 4 SELECTED`;
    $('clear-button').disabled = selected.size === 0 || busy;
  }

  function toggleCell(cell) {
    if (busy || game.complete || cell.refined) return;
    if (selected.has(cell.id)) selected.delete(cell.id);
    else if (selected.size < 4) selected.add(cell.id);
    else { say('Four numbers at a time. Clear your selection to start again.', 'error'); return; }
    reveal(cell.signal || signalForSelection());
    updateSelection();
  }

  function selectCluster(cell) {
    if (busy || !cell.signal || cell.refined || game.complete) return;
    selected = new Set(game.cells.filter((item) => item.signal?.cluster === cell.signal.cluster).map((item) => item.id));
    reveal(cell.signal); updateSelection();
  }

  function render() {
    const previousFocus = document.activeElement?.dataset?.id;
    $('file-name').textContent = game.name;
    $('file-id').textContent = `FILE ${game.id.slice(0, 4)} : ${game.id.slice(4)}`;
    $('progress-label').replaceChildren(document.createTextNode(String(game.progress).padStart(2, '0')));
    const percent = document.createElement('span'); percent.textContent = '%';
    $('progress-label').append(percent); $('progress').value = game.progress;
    $('save-state').textContent = 'SAVED';
    $('terminal').setAttribute('aria-busy', String(busy));
    field.replaceChildren();
    game.cells.forEach((cell) => {
      const element = document.createElement('button');
      element.type = 'button';
      element.className = `number${cell.signal ? ' resonant' : ''}${cell.refined ? ' refined' : ''}`;
      element.dataset.id = cell.id;
      element.tabIndex = cell.id === focusedCell ? 0 : -1;
      element.setAttribute('aria-pressed', 'false');
      element.setAttribute('aria-label', `${cell.value}, row ${Math.floor(cell.id / game.cols) + 1}, column ${cell.id % game.cols + 1}${cell.signal ? `, ${tempers[cell.signal.temper]} signal, bin ${cell.signal.route}` : ''}`);
      if (cell.refined) { element.disabled = true; element.tabIndex = -1; }
      if (cell.signal) {
        element.dataset.cluster = cell.signal.cluster;
        element.style.setProperty('--delay', `${-(cell.signal.cluster % 5) * .6}s`);
      }
      const digit = document.createElement('span'); digit.textContent = cell.value; element.append(digit);
      element.addEventListener('click', () => toggleCell(cell));
      element.addEventListener('dblclick', () => selectCluster(cell));
      element.addEventListener('pointerenter', () => {
        reveal(cell.signal || signalForSelection());
        $('coordinates').textContent = `${String(cell.id % game.cols).padStart(3, '0')} : ${String(Math.floor(cell.id / game.cols)).padStart(3, '0')}`;
      });
      element.addEventListener('focus', () => {
        field.querySelector('[tabindex="0"]')?.setAttribute('tabindex', '-1');
        focusedCell = cell.id; element.tabIndex = 0; reveal(cell.signal || signalForSelection());
      });
      element.addEventListener('keydown', (event) => {
        const offset = {ArrowRight: 1, ArrowLeft: -1, ArrowUp: -game.cols, ArrowDown: game.cols}[event.key];
        if (!offset) return;
        event.preventDefault();
        let next = cell.id;
        for (let attempts = 0; attempts < game.cells.length; attempts++) {
          next = (next + offset + game.cells.length) % game.cells.length;
          if (!game.cells[next].refined) {
            field.querySelector(`[data-id="${next}"]`).focus(); break;
          }
        }
      });
      element.draggable = !cell.refined;
      element.addEventListener('dragstart', (event) => {
        if (busy || game.complete) { event.preventDefault(); return; }
        if (!selected.has(cell.id)) selectCluster(cell);
        if (selected.size !== 4) { event.preventDefault(); return; }
        event.dataTransfer.setData('text/plain', 'mdr-selection');
        event.dataTransfer.effectAllowed = 'move';
      });
      field.append(element);
    });
    if (!field.querySelector('[tabindex="0"]')) {
      const first = field.querySelector('.number:not(.refined)');
      if (first) { first.tabIndex = 0; focusedCell = Number(first.dataset.id); }
    }
    $('bins').replaceChildren();
    game.bins.forEach((bin) => {
      const button = document.createElement('button'); button.type = 'button';
      button.className = 'bin'; button.dataset.bin = bin.id;
      button.disabled = busy || game.complete;
      button.setAttribute('aria-label', `Bin ${String(bin.id).padStart(2, '0')}, ${bin.count * 25}% balanced`);
      // All interpolated values come from the server's fixed numeric/temper schema.
      button.innerHTML = `<div class="bin-top"><span class="bin-number">${String(bin.id).padStart(2, '0')}</span><span>${bin.count * 25}%</span></div><div class="bin-body"><div class="bin-meter"><div class="bin-fill"></div></div><div class="bin-tempers">${Object.entries(bin.tempers).map(([key, value]) => `<span class="${value ? 'balanced' : ''}">${key}</span>`).join('')}</div></div>`;
      button.querySelector('.bin-fill').style.width = `${bin.count * 25}%`;
      button.addEventListener('click', () => refine(bin.id));
      button.addEventListener('dragover', (event) => { if (selected.size === 4 && !busy) { event.preventDefault(); button.classList.add('drag-over'); } });
      button.addEventListener('dragleave', () => button.classList.remove('drag-over'));
      button.addEventListener('drop', (event) => { event.preventDefault(); button.classList.remove('drag-over'); refine(bin.id); });
      $('bins').append(button);
    });
    $('new-button').disabled = busy;
    if (previousFocus !== undefined) field.querySelector(`[data-id="${previousFocus}"]:not(.refined)`)?.focus();
    updateSelection(); reveal(signalForSelection()); updateClock();
    if (game.complete && lastComplete !== game.id) {
      lastComplete = game.id;
      $('completion-summary').textContent = `${game.name} is 100% refined. All five bins are balanced. ${game.mistakes} rejected submission${game.mistakes === 1 ? '' : 's'}. Your contribution has been acknowledged.`;
      $('complete-dialog').showModal();
    }
  }

  async function load() {
    $('terminal').setAttribute('aria-busy', 'true');
    try {
      const response = await fetch('/api/game', {cache: 'no-store'});
      if (!response.ok) throw new Error('Connection unavailable');
      const data = await response.json(); game = data.game; csrf = data.csrf;
      selected.clear(); busy = false; render(); $('loading').hidden = true;
      $('terminal').setAttribute('aria-busy', 'false');
      say(game.progress ? 'Your saved file is ready. Continue your important work.' : 'Welcome, refiner. Find a moving group, or open the field manual.');
      return true;
    } catch {
      $('loading').hidden = false;
      $('loading').replaceChildren();
      const text = document.createElement('p'); text.textContent = 'Your terminal is offline. Progress remains saved.';
      const retry = document.createElement('button'); retry.textContent = 'Reconnect'; retry.addEventListener('click', load);
      $('loading').append(text, retry); $('save-state').textContent = 'OFFLINE';
      $('terminal').setAttribute('aria-busy', 'false');
      return false;
    }
  }

  async function mutate(path, payload) {
    if (busy || !game) return;
    busy = true; $('save-state').textContent = 'SAVING';
    $('terminal').setAttribute('aria-busy', 'true');
    $('bins').querySelectorAll('button').forEach((button) => { button.disabled = true; });
    $('new-button').disabled = true; $('confirm-reset').disabled = true;
    $('clear-button').disabled = true;
    try {
      const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({...payload, game_id: game.id, revision: game.revision})});
      const data = await response.json();
      if (response.status === 401 || response.status === 403) {
        await load(); say(data.error, 'error'); return;
      }
      if (data.game) { game = data.game; selected.clear(); }
      busy = false; render();
      if (!response.ok) { say(data.error || 'Submission unavailable.', 'error'); tone(false); return; }
      say(data.message, data.accepted ? 'success' : 'error'); tone(data.accepted);
      if (data.accepted && payload.bin) $('bins').querySelector(`[data-bin="${payload.bin}"]`)?.classList.add('accepted');
    } catch {
      // A POST may have committed even if its response was lost. Reconcile; never replay it.
      await load(); say('Connection interrupted. Your terminal is resynchronized when online; check progress before trying again.', 'error');
    } finally {
      busy = false; $('confirm-reset').disabled = false;
      $('terminal').setAttribute('aria-busy', 'false');
    }
  }

  function refine(bin) {
    if (busy || !game || game.complete) return;
    if (selected.size !== 4) { say('Select the four numbers in a moving cluster first.', 'error'); return; }
    return mutate('/api/refine', {cells: [...selected], bin});
  }

  function updateClock() {
    if (!game) return;
    const seconds = Math.max(0, (game.finished || Math.floor(Date.now() / 1000)) - game.started);
    $('elapsed').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }
  field.addEventListener('pointerleave', () => reveal(signalForSelection()));
  $('clear-button').addEventListener('click', () => { if (!busy) { selected.clear(); updateSelection(); reveal(null); } });
  $('manual-button').addEventListener('click', () => $('manual').showModal());
  $('new-button').addEventListener('click', () => { if (!busy) $('reset-dialog').showModal(); });
  $('confirm-reset').addEventListener('click', () => { $('reset-dialog').close(); mutate('/api/reset', {}); });
  $('next-file').addEventListener('click', () => { $('complete-dialog').close(); mutate('/api/reset', {}); });
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => $(button.dataset.close).close()));
  $('motion-button').addEventListener('click', () => { still = !still; savePreference('mdr-still', still ? '1' : '0'); updateMotion(); });
  $('sound-button').addEventListener('click', async () => {
    try {
      if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
      await audio.resume(); sound = !sound;
      $('sound-button').textContent = sound ? 'Sound on' : 'Sound off';
      $('sound-button').setAttribute('aria-pressed', String(sound)); tone();
    } catch { say('Audio is unavailable in this browser. Your work can continue.'); }
  });
  document.addEventListener('keydown', (event) => {
    if (document.querySelector('dialog[open]') || busy || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === 'Escape') { selected.clear(); updateSelection(); reveal(null); }
    if (/^[1-5]$/.test(event.key)) { event.preventDefault(); refine(Number(event.key)); }
  });
  setInterval(updateClock, 1000);
  load();
})();
