/* Browsing state belongs to this tab; a Telegram deep link has its own state. */
const SITE = window.KA_SITE || {};
const HOME = SITE.center || null;
const RADIUS_KM = SITE.radius_km || 5;
if (SITE.label) {
  document.title = `Zu verschenken · ${SITE.label}`;
  document.getElementById('heading').textContent = `🎁 Zu verschenken · ${SITE.label}`;
}
const STYLES = {light: 'https://tiles.openfreemap.org/styles/liberty',
                dark: 'https://tiles.openfreemap.org/styles/dark'};
const PAGE_SIZE = 25;
const focusId = new URLSearchParams(location.search).get('ad');
const STATE_KEY = `ka-browse-v2:${focusId || 'all'}`;
const $ = id => document.getElementById(id);
const mobile = () => matchMedia('(max-width: 760px)').matches;
function readState() {
  try { return JSON.parse(sessionStorage.getItem(STATE_KEY)) || {}; } catch { return {}; }
}
const saved = readState();
let theme = 'light';
try { theme = localStorage.getItem('ka-theme') === 'dark' ? 'dark' : 'light'; } catch {}
document.body.classList.toggle('dark', theme === 'dark');
$('search').value = typeof saved.search === 'string' ? saved.search : '';
$('good').checked = saved.good !== false;
$('sort').value = ['new', 'score', 'dist'].includes(saved.sort) ? saved.sort : 'new';
let page = Number.isInteger(saved.page) && saved.page > 0 ? saved.page : 1;
let selected = saved.selected || null, popupId = saved.popup || null;
let view = saved.view === 'map' ? 'map' : 'list';
let ads = [], pendingAds = null, markers = {}, initialized = false, restoring = false;
let listPosition = saved.position || {scroll: 0};
let loading = false, map = null;
const feedbackPending = new Set(), feedbackErrors = new Map(), feedbackChanges = new Map();
let feedbackRevision = 0;
const hasCoords = a => Number.isFinite(a.lat) && Number.isFinite(a.lon);
const validCamera = c => c && Array.isArray(c.center) && c.center.length === 2
  && c.center.every(Number.isFinite) && Math.abs(c.center[1]) <= 90
  && Number.isFinite(c.zoom) && c.zoom >= 0 && c.zoom <= 22;
try {
  const camera = validCamera(saved.camera) ? saved.camera
    : HOME ? {center: HOME, zoom: 12.5 - Math.log2(RADIUS_KM / 5)} : {center: [10.45, 51.16], zoom: 5};
  map = new maplibregl.Map({container: 'map', style: STYLES[theme], ...camera,
                           attributionControl: {compact: true}});
  map.addControl(new maplibregl.NavigationControl({showCompass: false}), 'top-right');
  map.addControl(new maplibregl.GeolocateControl({trackUserLocation: true}), 'top-right');
  if (HOME) map.on('style.load', () => {
    const pts = [];
    for (let i = 0; i <= 64; i++) {
      const a = i / 64 * 2 * Math.PI;
      pts.push([HOME[0] + RADIUS_KM / (111.32 * Math.cos(HOME[1] * Math.PI / 180)) * Math.cos(a),
                HOME[1] + RADIUS_KM / 110.57 * Math.sin(a)]);
    }
    if (map.getSource('radius')) return;
    map.addSource('radius', {type: 'geojson', data: {type: 'Feature', geometry: {type: 'LineString', coordinates: pts}}});
    map.addLayer({id: 'radius', type: 'line', source: 'radius', paint: {
      'line-color': '#1a73e8', 'line-width': 1.5, 'line-dasharray': [2, 3], 'line-opacity': .6}});
  });
  if (HOME) {
    const homeEl = document.createElement('div'); homeEl.className = 'home';
    new maplibregl.Marker({element: homeEl}).setLngLat(HOME)
      .setPopup(new maplibregl.Popup({offset: 12}).setText(`Suchzentrum (${SITE.label})`)).addTo(map);
  }
  map.on('moveend', saveState);
} catch {
  $('notice').textContent = 'Karte nicht verfügbar. Die Anzeigen kannst du weiter in der Liste ansehen.';
}
function color(score) {
  return score == null ? '#9e9e9e' : score >= 9 ? '#d93025' : score >= 7 ? '#f57c00' : score >= 5 ? '#f9ab00' : '#80868b';
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function safeUrl(s) {
  try { const u = new URL(s); return ['http:', 'https:'].includes(u.protocol) ? esc(u.href) : ''; } catch { return ''; }
}
function link(a, content, cls = '') {
  return `<a class="${cls}" href="${safeUrl(a.url)}" target="_blank" rel="noopener noreferrer">${content}</a>`;
}
function feedbackStatus(a) {
  if (feedbackPending.has(a.id)) return 'Speichert …';
  if (feedbackErrors.has(a.id)) return 'Fehler – bitte erneut versuchen.';
  return a.feedback_vote === 1 ? 'Gefällt dir' : a.feedback_vote === -1 ? 'Nicht interessant' : 'Dein Urteil';
}
function feedbackControls(a) {
  return `<div class="feedback" data-feedback-id="${esc(a.id)}" role="group" aria-label="Dein Urteil zu ${esc(a.title)}">
    <button type="button" data-vote="1" aria-label="Gefällt mir" aria-pressed="${a.feedback_vote === 1}" ${feedbackPending.has(a.id) ? 'disabled' : ''}>👍 Gut</button>
    <button type="button" data-vote="-1" aria-label="Nicht interessant" aria-pressed="${a.feedback_vote === -1}" ${feedbackPending.has(a.id) ? 'disabled' : ''}>👎 Nein</button>
    <span class="feedback-status${feedbackErrors.has(a.id) ? ' error' : ''}" role="status">${feedbackStatus(a)}</span>
  </div>`;
}
function syncFeedback(id) {
  const ad = ads.find(a => a.id === id);
  if (!ad) return;
  // Update controls in place: don't recreate the list or popup, move the map, or lose focus.
  document.querySelectorAll('[data-feedback-id]').forEach(group => {
    if (group.dataset.feedbackId !== id) return;
    group.querySelectorAll('[data-vote]').forEach(button => {
      button.setAttribute('aria-pressed', String(ad.feedback_vote === Number(button.dataset.vote)));
      button.disabled = feedbackPending.has(id);
    });
    const status = group.querySelector('.feedback-status');
    status.textContent = feedbackStatus(ad);
    status.classList.toggle('error', feedbackErrors.has(id));
  });
}
async function submitFeedback(id, vote) {
  const ad = ads.find(a => a.id === id);
  if (!ad || feedbackPending.has(id)) return;
  // A second click on the selected thumb clears the vote.
  const value = ad.feedback_vote === vote ? 0 : vote;
  feedbackPending.add(id); feedbackErrors.delete(id); syncFeedback(id);
  try {
    const response = await fetch('/feedback', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-KA-Feedback': '1'},
      body: JSON.stringify({id, vote: value}), signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    if (result.id !== id || ![null, -1, 1].includes(result.vote)) throw new Error('Invalid feedback');
    feedbackChanges.set(id, {revision: ++feedbackRevision, vote: result.vote});
    // A background response or staged refresh must not undo the just-saved vote.
    for (const rows of [ads, pendingAds || []]) {
      const current = rows.find(a => a.id === id);
      if (current) current.feedback_vote = result.vote;
    }
  } catch {
    feedbackErrors.set(id, true);
  } finally {
    feedbackPending.delete(id); syncFeedback(id);
  }
}
document.addEventListener('click', e => {
  const button = e.target.closest('button[data-vote]');
  if (button) submitFeedback(button.closest('[data-feedback-id]').dataset.feedbackId, Number(button.dataset.vote));
});
function km(a) { const m = /([\d,.]+)\s*km/.exec(a.distance || ''); return m ? parseFloat(m[1].replace(',', '.')) : Infinity; }
// Preserve separation of listings that share a postcode centre; all pins are approximate.
function pos(a) {
  const h = [...a.id].reduce((x, c) => (x * 31 + c.charCodeAt(0)) >>> 0, 7);
  return [a.lon + (((h / 1000 | 0) % 1000) / 1000 - 0.5) * 0.006,
          a.lat + ((h % 1000) / 1000 - 0.5) * 0.004];
}
function ago(ts) {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 60 ? `vor ${m} Min` : m < 1440 ? `vor ${Math.round(m / 60)} Std` : `vor ${Math.round(m / 1440)} Tg`;
}
function normalize(s) { return String(s || '').toLocaleLowerCase('de').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ß/g, 'ss'); }
function visibleAds() {
  const terms = normalize($('search').value).trim().split(/\s+/).filter(Boolean);
  const list = ads.filter(a => (!$('good').checked || a.score >= 7)
    && terms.every(t => normalize(`${a.title} ${a.description || ''} ${a.location}`).includes(t)));
  const tie = (x, y) => y.first_seen - x.first_seen || String(y.id).localeCompare(String(x.id));
  if ($('sort').value === 'score') list.sort((x, y) => (y.score ?? -1) - (x.score ?? -1) || tie(x, y));
  else if ($('sort').value === 'dist') list.sort((x, y) => (km(x) - km(y)) || tie(x, y));
  else list.sort(tie);
  return list;
}
function capturePosition() {
  const ul = $('list');
  if (!ul.clientHeight) return listPosition; // Hidden mobile list still has a saved position.
  const top = ul.getBoundingClientRect().top;
  const row = [...ul.querySelectorAll('li[data-id]')].find(li => li.getBoundingClientRect().bottom > top);
  listPosition = {scroll: ul.scrollTop, anchor: row?.dataset.id,
                  offset: row ? row.getBoundingClientRect().top - top : 0};
  return listPosition;
}
function restorePosition(position) {
  listPosition = position || {scroll: 0};
  const ul = $('list');
  if (!ul.clientHeight) return;
  ul.scrollTop = listPosition.scroll || 0;
  const row = [...ul.querySelectorAll('li[data-id]')].find(li => li.dataset.id === listPosition.anchor);
  if (row) ul.scrollTop += row.getBoundingClientRect().top - ul.getBoundingClientRect().top - (listPosition.offset || 0);
}
function saveState() {
  if (!initialized || restoring) return;
  const camera = map ? {center: map.getCenter().toArray(), zoom: map.getZoom(),
                        bearing: map.getBearing(), pitch: map.getPitch()} : saved.camera;
  try {
    sessionStorage.setItem(STATE_KEY, JSON.stringify({search: $('search').value, good: $('good').checked,
      sort: $('sort').value, page, selected, popup: popupId, view, position: capturePosition(), camera}));
  } catch {} // Browsing must also work with storage disabled.
}
function renderList(position = {scroll: 0}) {
  const list = visibleAds();
  const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  page = Math.max(1, Math.min(page, pages));
  const start = (page - 1) * PAGE_SIZE;
  $('list').innerHTML = list.length ? list.slice(start, start + PAGE_SIZE).map(a => `
    <li data-id="${esc(a.id)}" class="${a.id === selected ? 'sel' : ''}">
      ${link(a, a.image ? `<img class="thumb" loading="lazy" src="${safeUrl(a.image)}" alt="${esc(a.title)}">` : '<div class="thumb" aria-label="Anzeige öffnen">🎁</div>', 'photo')}
      <div class="info">
        <div class="top">${link(a, esc(a.title), 'title')}
          <span class="badge" style="background:${color(a.score)}">${a.score ?? '?'}</span></div>
        <div class="meta">${esc(a.location)}${a.distance ? ' · ' + esc(a.distance) : ''} · ${ago(a.first_seen)}</div>
        <div class="reason">${esc(a.reason)}</div>
        <div class="links">${link(a, 'Anzeige ↗')}${hasCoords(a) && map ? `<button class="map-action" data-map="${esc(a.id)}">Auf Karte</button>` : ''}</div>
        ${feedbackControls(a)}
      </div>
    </li>`).join('') : '<li class="empty">Keine passenden Anzeigen in den letzten 3 Tagen. Suche oder Score-Filter anpassen.</li>';
  $('count').textContent = `${list.length} Anzeigen`;
  $('page-label').textContent = `Seite ${page} / ${pages}`;
  $('prev').disabled = page <= 1; $('next').disabled = page >= pages;
  restorePosition(position);
}
function renderMarkers() {
  if (!map) return;
  const list = visibleAds().filter(hasCoords);
  const ids = new Set(list.map(a => a.id));
  Object.entries(markers).forEach(([id, m]) => { if (!ids.has(id)) { m.remove(); delete markers[id]; } });
  for (const a of list) {
    let m = markers[a.id];
    if (!m) {
      const el = document.createElement('button'); el.type = 'button';
      el.setAttribute('aria-label', `${a.title}, Score ${a.score ?? '?'}`);
      const popup = new maplibregl.Popup({offset: 18, maxWidth: '260px'});
      m = new maplibregl.Marker({element: el}).setLngLat(pos(a)).setPopup(popup).addTo(map);
      markers[a.id] = m;
      popup.on('open', () => {
        popupId = a.id;
        if (!restoring) select(a.id, true);
        syncFeedback(a.id);
      });
      popup.on('close', () => { if (popupId === a.id) popupId = null; saveState(); });
    }
    m.setLngLat(pos(a));
    const el = m.getElement();
    el.className = 'pin' + (a.id === selected ? ' sel' : '');
    el.style.background = color(a.score); el.textContent = a.score ?? '?';
    const content = `<div class="popup">${a.image ? link(a, `<img src="${safeUrl(a.image)}" alt="${esc(a.title)}">`) : ''}<div class="body">
      ${link(a, `<b>${esc(a.title)}</b>`)}<small>${esc(a.location)} · Standort ungefähr</small>
      <p>${esc(a.reason)}</p>${link(a, 'Auf Kleinanzeigen öffnen ↗')}${feedbackControls(a)}</div></div>`;
    if (m.content !== content) { m.getPopup().setHTML(content); m.content = content; }
  }
}
function select(id, fromMap = false) {
  const list = visibleAds(), index = list.findIndex(a => a.id === id);
  if (index < 0) return;
  selected = id;
  const nextPage = Math.floor(index / PAGE_SIZE) + 1;
  const position = nextPage === page ? capturePosition() : {scroll: 0};
  page = nextPage;
  renderList(position);
  Object.entries(markers).forEach(([key, m]) => m.getElement().classList.toggle('sel', key === id));
  if (fromMap) {
    if (!mobile() || view === 'list') $('list').querySelector(`li[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({block: 'nearest'});
  } else if (markers[id]) {
    setView('map');
    map.flyTo({center: markers[id].getLngLat(), zoom: Math.max(map.getZoom(), 14.5), duration: 500});
    Object.entries(markers).forEach(([key, m]) => { if (key !== id && m.getPopup().isOpen()) m.togglePopup(); });
    if (!markers[id].getPopup().isOpen()) markers[id].togglePopup();
  }
  saveState();
}
function setView(v) {
  capturePosition();
  view = v;
  document.body.classList.toggle('view-map', v === 'map');
  document.body.classList.toggle('view-list', v === 'list');
  document.querySelectorAll('#switch button').forEach(b => b.classList.toggle('on', b.dataset.view === v));
  if (v === 'map') map?.resize();
  else restorePosition(listPosition);
  saveState();
}
function render(position = {scroll: 0}) { renderList(position); renderMarkers(); }
function fitAll() {
  if (!map) return;
  const points = visibleAds().filter(hasCoords).map(a => [a.lon, a.lat]);
  if (!points.length) return;  // Ohne Anzeigen bleibt der Blick auf den Suchumkreis.
  const bounds = new maplibregl.LngLatBounds();
  if (HOME) bounds.extend(HOME);
  points.forEach(p => bounds.extend(p));
  map.fitBounds(bounds, {padding: 50, maxZoom: 15, duration: 0});
}
function applyAds(next, position) {
  ads = next;
  const index = visibleAds().findIndex(a => a.id === position?.anchor);
  if (index >= 0) page = Math.floor(index / PAGE_SIZE) + 1;
  render(position);
}
async function load(first = false) {
  if (loading) return;
  loading = true;
  const requestedRevision = feedbackRevision;
  try {
    const response = await fetch('/ads.json');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const next = await response.json();
    if (!Array.isArray(next)) throw new Error('Invalid listings');
    next.forEach(a => {
      const changed = feedbackChanges.get(a.id);
      if (changed && changed.revision > requestedRevision) a.feedback_vote = changed.vote;
    });
    $('notice').textContent = map ? '' : 'Karte nicht verfügbar. Die Liste funktioniert weiterhin.';
    if (initialized && !first) {
      pendingAds = JSON.stringify(next) === JSON.stringify(ads) ? null : next;
      $('refresh').hidden = !pendingAds;
      if (pendingAds) {
        const ids = new Set(ads.map(a => a.id));
        const count = next.filter(a => !ids.has(a.id)).length;
        $('refresh').textContent = count ? `${count} neue Anzeigen · Aktualisieren` : 'Ergebnisse aktualisieren';
      }
      return;
    }
    restoring = true;
    applyAds(next, listPosition);
    setView(view);
    const focus = focusId && ads.find(a => a.id === focusId);
    if (focus && !saved.position) {
      $('search').value = '';
      if (focus.score < 7 || focus.score == null) $('good').checked = false;
      render();
      select(focus.id);
    } else {
      if (!validCamera(saved.camera)) fitAll();
      if (popupId && markers[popupId]) markers[popupId].togglePopup();
      restorePosition(listPosition);
    }
    if (focusId && !focus) $('notice').textContent = 'Diese Anzeige ist nicht mehr in der Übersicht. Hier sind die aktuellen Anzeigen.';
    initialized = true;
    restoring = false;
    saveState();
  } catch {
    restoring = false;
    $('notice').textContent = 'Anzeigen konnten nicht geladen werden. Bitte erneut versuchen.';
    $('refresh').textContent = 'Erneut versuchen'; $('refresh').hidden = false;
  } finally { loading = false; }
}
$('refresh').onclick = () => {
  if (pendingAds) {
    const position = capturePosition();
    applyAds(pendingAds, position); pendingAds = null;
    $('refresh').hidden = true; $('notice').textContent = ''; saveState();
  } else load(!initialized);
};
$('list').addEventListener('click', e => {
  const button = e.target.closest('[data-map]');
  if (button) select(button.dataset.map);
  if (e.target.closest('a')) saveState();
});
$('list').addEventListener('scroll', saveState, {passive: true});
function changeFilters() { page = 1; selected = null; render(); saveState(); }
$('search').addEventListener('input', changeFilters);
$('good').addEventListener('change', changeFilters);
$('sort').addEventListener('change', changeFilters);
$('prev').onclick = () => { page--; renderList(); saveState(); };
$('next').onclick = () => { page++; renderList(); saveState(); };
document.querySelectorAll('#switch button').forEach(b => b.onclick = () => setView(b.dataset.view));
$('theme').textContent = theme === 'light' ? '🌙' : '☀️';
$('theme').onclick = () => {
  theme = theme === 'light' ? 'dark' : 'light';
  try { localStorage.setItem('ka-theme', theme); } catch {}
  document.body.classList.toggle('dark', theme === 'dark');
  $('theme').textContent = theme === 'light' ? '🌙' : '☀️';
  map?.setStyle(STYLES[theme]);
};
window.addEventListener('pagehide', saveState);
document.addEventListener('visibilitychange', () => { if (document.hidden) saveState(); });
load(true); // Search and list work even if the map tiles cannot load.
setInterval(() => load(), 5 * 60 * 1000);
