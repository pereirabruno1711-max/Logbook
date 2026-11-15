/* App do motorista v2 — Jornada (4 estados, cronómetro, limites, notas), Relatórios (assinatura em 3 passos) e Perfil */
const $ = s => document.querySelector(s);
const esc = s => (s ?? '').toString().replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const STL = {driving: 'Condução', working: 'Trabalho', resting: 'Descanso', availability: 'Disponibilidade'};
const ICON = {
  driving: '<circle cx="24" cy="24" r="18"/><circle cx="24" cy="24" r="4"/><path d="M6.5 24H20M28 24h13.5M24 28v14"/>',
  working: '<path d="M31 7a11 11 0 0 0-10 14L7 35a4.2 4.2 0 0 0 6 6l14-14a11 11 0 0 0 14-10l-7 4-5-5z"/>',
  resting: '<path d="M6 10v28M6 31h36v7M6 31v-9h17v9"/><circle cx="14" cy="16" r="3"/>',
  availability: '<circle cx="24" cy="24" r="18"/><path d="M24 12v12l8 5"/>'};
const svg = k => `<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">${ICON[k]}</svg>`;
const hm = m => Math.floor((m || 0) / 60) + 'h' + String(Math.round(m || 0) % 60).padStart(2, '0');
const fmtDay = d => new Date(d + 'T12:00:00').toLocaleDateString('pt-PT', {weekday: 'long', day: 'numeric', month: 'long'});
const kmf = v => (v ?? 0).toLocaleString('pt-PT', {minimumFractionDigits: 1, maximumFractionDigits: 1});
const main = $('#main'), back = $('#back');
let ME = null, TAB = 'home', DAY = null, D = null, STEP = 1, tick = null, poll = null;

async function api(url, opts = {}) {
  const o = Object.assign({headers: {}}, opts);
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers['Content-Type'] = 'application/json'; delete o.json; }
  const r = await fetch(url, o);
  if (r.status === 401) { location.href = '/d'; throw new Error('Sessão terminada.'); }
  const data = r.headers.get('content-type')?.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && (data.erro || data.detail)) || r.statusText);
  return data;
}
function toast(m) { const t = document.createElement('div'); t.className = 'toast'; t.textContent = m; document.body.appendChild(t); setTimeout(() => t.remove(), 2600); }
const fail = e => { main.insertAdjacentHTML('afterbegin', `<div class="err">${esc(e.message)}</div>`); window.scrollTo(0, 0); };
const bar = b => `<div class="tl">${[...b].map(c => `<i class="${c === '-' ? 'f' : c}"></i>`).join('')}</div><div class="tlh"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>`;
const sumBoxes = s => `<div class="sum s4"><div class="d"><b>${hm(s.driving)}</b><span>Condução</span></div><div class="w"><b>${hm(s.working)}</b><span>Trabalho</span></div><div class="a"><b>${hm(s.availability)}</b><span>Disponib.</span></div><div class="r"><b>${hm(s.resting)}</b><span>Descanso</span></div></div>`;

function chrome(showBack, title, sub) {
  $('#top').style.display = showBack ? '' : 'none'; $('#tabs').style.display = showBack ? 'none' : '';
  if (showBack) { $('#hTitle').textContent = title; $('#hSub').textContent = sub; }
  main.className = showBack ? '' : (TAB === 'home' ? 'home' : '');
}
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => { TAB = b.dataset.tab; DAY = null; render(); });
back.onclick = () => { DAY = null; render(); };

async function render() {
  clearInterval(tick); clearInterval(poll);
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === TAB));
  try {
    ME = await api('/d/api/me');
    if (DAY) return renderDay();
    chrome(false);
    return ({home: renderHome, days: renderDays, profile: renderProfile})[TAB]();
  } catch (e) { fail(e); }
}

// ---------- jornada (início) ----------
const clock = ms => { const s = Math.max(0, Math.floor(ms / 1000)); return [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60].map(x => String(x).padStart(2, '0')).join(':'); };
function lim(label, v, max) {
  const pct = Math.min(100, v / max * 100), cls = v >= max ? 'over' : v >= max * .8 ? 'mid' : '';
  return `<div class="r"><b>${label}</b><span>${hm(v)} <span class="mute">/ máx. ${hm(max)}</span></span></div><div class="pb ${cls}"><i style="width:${pct}%"></i></div>`;
}
async function renderHome() {
  const today = await api('/d/api/day/' + ME.today);
  const st = ME.state, live = today.live === 'driving' ? 'driving' : (st.state || 'resting');
  const js = st.journey_start ? new Date(st.journey_start).getTime() : null;
  const tile = k => `<button class="tile ${k} ${live === k ? 'on' : ''}" data-s="${k}" ${k === 'driving' && ME.has_vehicle ? 'disabled' : ''}>${svg(k)}${STL[k]}${k === 'driving' && ME.has_vehicle ? '<small>automático (GPS)</small>' : ''}</button>`;
  const L = today.limits || {cont: 0, cont_max: 360, day: 0, day_max: 600};
  const pend = (await api('/d/api/days')).filter(d => d.can_sign && d.status !== 'signed').slice(0, 7).length;
  main.innerHTML = `
    <div class="card jour"><div><div class="tm" id="tm">${js ? clock(Date.now() - js) : '—'}</div><div class="lb">Tempo em jornada</div></div><div class="cur">${svg(live)}</div></div>
    ${ME.has_vehicle ? '' : '<div class="warn">Sem viatura atribuída: a <b>condução</b> é registada por si. Peça ao gestor um objeto GPS para ser automática.</div>'}
    <div class="tiles">${['driving', 'working', 'resting', 'availability'].map(tile).join('')}</div>
    ${js ? '<button class="btn pill" id="endJ">Encerrar jornada</button>' : '<p class="small mute" style="text-align:center;margin:0 0 16px">Escolha um estado para iniciar a jornada.</p>'}
    <div class="card lim">${lim('Horas contínuas em trabalho', L.cont, L.cont_max)}${lim('Horas diárias trabalhadas', L.day, L.day_max)}</div>
    <button class="btn line" id="note">✎ Escrever nota${today.notes && today.notes.length ? ` (${today.notes.length})` : ''}</button>
    ${pend ? `<div class="warn" style="margin-top:16px">Tem <b>${pend}</b> dia${pend > 1 ? 's' : ''} por assinar. <a href="#" id="goDays">Ver relatórios</a></div>` : ''}`;
  main.querySelectorAll('.tile:not([disabled])').forEach(b => b.onclick = async () => {
    try { await api('/d/api/state', {method: 'POST', json: {state: b.dataset.s}}); renderHome(); } catch (e) { fail(e); } });
  const e = $('#endJ'); if (e) e.onclick = async () => { if (!confirm('Encerrar a jornada de hoje?')) return;
    try { await api('/d/api/state', {method: 'POST', json: {state: 'end'}}); toast('Jornada encerrada'); renderHome(); } catch (x) { fail(x); } };
  $('#note').onclick = noteSheet;
  const g = $('#goDays'); if (g) g.onclick = ev => { ev.preventDefault(); TAB = 'days'; render(); };
  if (js) tick = setInterval(() => { const t = $('#tm'); if (t) t.textContent = clock(Date.now() - js); }, 1000);
  poll = setInterval(() => { if (!document.hidden) renderHome(); }, 60000);
}
function sheet(html) { const o = document.createElement('div'); o.className = 'ov'; o.innerHTML = `<div class="sheet">${html}</div>`;
  o.onclick = ev => { if (ev.target === o) o.remove(); }; document.body.appendChild(o); return o; }
function noteSheet() {
  const o = sheet(`<h2>Escrever nota</h2><textarea id="nt" rows="4" maxlength="500" placeholder="Ex.: avaria, espera na carga, desvio…"></textarea>
    <div class="btns two"><button class="btn sec" id="nc">Cancelar</button><button class="btn" id="ns">Guardar</button></div>`);
  $('#nc').onclick = () => o.remove();
  $('#ns').onclick = async () => { try { await api('/d/api/notes', {method: 'POST', json: {text: $('#nt').value}}); o.remove(); toast('Nota guardada'); renderHome(); } catch (e) { alert(e.message); } };
  setTimeout(() => $('#nt').focus(), 80);
}

// ---------- relatórios (lista) ----------
const PILL = {none: 'Sem relatório', filling: 'Em preenchimento', signed: 'Assinado'};
async function renderDays() {
  const days = await api('/d/api/days');
  main.innerHTML = `<div class="card"><h2>Mostrar à fiscalização</h2><p class="small mute" style="margin-top:0">Um só PDF com os dias mais recentes. Os dias por assinar aparecem assinalados.</p>
      <div class="fisc"><a class="btn sec" target="_blank" href="/d/pdf/fiscalizacao?days=1">Hoje</a><a class="btn sec" target="_blank" href="/d/pdf/fiscalizacao?days=7">7 dias</a><a class="btn sec" target="_blank" href="/d/pdf/fiscalizacao?days=28">28 dias</a></div></div>` +
    days.map(d => `<div class="day" data-d="${d.day}"><div class="t"><b>${d.today ? 'Hoje' : fmtDay(d.day)}</b><span>${d.today ? fmtDay(d.day) : d.day.split('-').reverse().join('/')}</span></div><span class="pill ${d.status}">${PILL[d.status]}</span></div>`).join('');
  main.querySelectorAll('.day').forEach(el => el.onclick = () => { DAY = el.dataset.d; STEP = 0; render(); });
}

// ---------- perfil ----------
async function renderProfile() {
  const v = ME.vehicle;
  main.innerHTML = `<div class="card" style="text-align:center"><div class="avatar">${esc(ME.driver.name.split(' ').map(x => x[0]).slice(0, 2).join('').toUpperCase())}</div><b style="font-size:18px">${esc(ME.driver.name)}</b><div class="mute small">${esc(ME.company)}</div></div>
    <div class="card"><h2>Dados</h2><div class="kv"><span>Telemóvel</span><b>${esc(ME.driver.phone) || '—'}</b></div><div class="kv"><span>Carta de condução</span><b>${esc(ME.driver.licence) || '—'}</b></div></div>
    <div class="card"><h2>Viatura</h2><div class="kv"><span>Atual</span><b>${v ? esc(v.name) + (v.plate ? ' · ' + esc(v.plate) : '') : 'Nenhuma'}</b></div>
      ${ME.can_pick_vehicle ? '<button class="btn sec" id="chg" style="margin-top:10px">Mudar de viatura</button>' : '<p class="small mute" style="margin:8px 0 0">A viatura é atribuída pelo seu gestor.</p>'}</div>
    <div class="card"><h2>Privacidade</h2><p class="small mute" style="margin:0">Esta app regista os estados de trabalho que indicar e usa a posição GPS da viatura atribuída para detetar a condução, as viagens e a distância. Os dados pertencem à sua empresa (${esc(ME.company)}) e servem para o registo dos tempos de trabalho. Para saber mais ou exercer os seus direitos, fale com o seu gestor.</p></div>
    <a class="btn sec" href="/d/sair">Terminar sessão neste telemóvel</a>`;
  const c = $('#chg'); if (c) c.onclick = vehicleSheet;
}
async function vehicleSheet() {
  try {
    const list = await api('/d/api/vehicles');
    const o = sheet(`<h2>Escolher viatura</h2>${list.map(x => `<div class="opt ${ME.vehicle && ME.vehicle.name === x.name ? 'on' : ''}" data-p="${esc(x.provider)}" data-i="${esc(x.device_id)}"><b>${esc(x.name)}</b><span class="mute small">${esc(x.plate || '')}</span></div>`).join('') || '<p class="mute">Sem viaturas.</p>'}
      <button class="btn sec" id="vc">Fechar</button>`);
    $('#vc').onclick = () => o.remove();
    o.querySelectorAll('.opt').forEach(el => el.onclick = async () => { try { await api('/d/api/vehicle', {method: 'POST', json: {provider: el.dataset.p, device_id: el.dataset.i}}); o.remove(); toast('Viatura atualizada'); render(); } catch (e) { alert(e.message); } });
  } catch (e) { alert(e.message); }
}

// ---------- dia (revisão, viatura, assinatura) ----------
async function renderDay() {
  main.innerHTML = '<div class="card mute">A carregar…</div>';
  D = await api('/d/api/day/' + DAY);
  if (!STEP) STEP = D.status === 'signed' || !D.editable ? 0 : (D.step || 1);
  drawDay();
}
async function save(patch) { D = await api('/d/api/day/' + DAY, {method: 'POST', json: patch}); }
function stepper() { return `<div class="steps">${['Dia', 'Viatura', 'Assinatura'].map((t, i) => `<div class="${STEP === i + 1 ? 'on' : STEP > i + 1 ? 'done' : ''}">${i + 1}. ${t}</div>`).join('')}</div>`; }
function drawDay() {
  chrome(true, D.is_today ? 'Hoje' : fmtDay(DAY), `${D.driver.name} · ${PILL[D.status]}`);
  if (D.status === 'signed') return drawSigned();
  if (!D.editable) return drawToday();
  main.innerHTML = stepper() + (D.gps_ok ? '' : `<div class="warn">${esc(D.gps_error)} Os valores abaixo podem estar incompletos.</div>`) + '<div id="step"></div>';
  ({1: step1, 2: step2, 3: step3})[STEP]();
}
const notesBox = () => D.notes && D.notes.length ? `<div class="card"><h2>Notas</h2>${D.notes.map(n => `<div class="kv"><span>${n.time}</span><b style="font-weight:500;text-align:right">${esc(n.text)}</b></div>`).join('')}</div>` : '';
function drawToday() {
  main.innerHTML = `<div class="info">O dia ainda não terminou. Só pode assinar depois da meia-noite.</div>${sumBoxes(D.summary)}<div class="card">${bar(D.bar)}</div>
    <div class="card"><h2>Estados</h2>${segs()}</div>${notesBox()}<a class="btn sec" href="/d/pdf/${DAY}" target="_blank">Pré-visualizar PDF</a>`;
}
function segs() {
  return D.segments.map(s => `<div class="seg"><span class="dot ${s.state}"></span><div class="t"><b>${STL[s.state]}</b><span>${s.start} – ${s.end}${s.locked ? ' · automático' : ''}</span></div><span class="du">${hm(s.minutes)}</span></div>`).join('') || '<span class="mute">Sem dados.</span>';
}
function step1() {
  const ed = D.edits || [];
  const opts = ['working', 'resting', 'availability'].concat(D.has_vehicle ? [] : ['driving']);
  $('#step').innerHTML = `${sumBoxes(D.summary)}<div class="card">${bar(D.bar)}</div>
    <div class="card"><h2>Estados do dia</h2>${segs()}</div>${notesBox()}
    <div class="card"><h2>Corrigir um período</h2>
      <p class="small mute" style="margin-top:0">${D.has_vehicle ? 'Pode alterar períodos de Trabalho, Descanso e Disponibilidade. A condução (a azul) é detetada pelo GPS e não se altera.' : 'Sem viatura atribuída, pode indicar também períodos de condução.'}</p>
      <div class="grid3"><label class="f"><span>De</span><input type="time" id="eFrom" value="08:00"></label><label class="f"><span>Até</span><input type="time" id="eTo" value="09:00"></label>
        <label class="f"><span>Estado</span><select id="eSt">${opts.map(o => `<option value="${o}">${STL[o]}</option>`).join('')}</select></label></div>
      <button class="btn sec" id="eAdd">Aplicar correção</button>
      ${ed.length ? '<div style="margin-top:12px">' + ed.map((e, i) => `<div class="edit"><span>${e.from} – ${e.to} → <b>${STL[e.state]}</b></span><button data-i="${i}" aria-label="Remover">✕</button></div>`).join('') + '</div>' : ''}</div>
    <div class="btns"><button class="btn" id="next">Guardar e continuar</button><div class="btns two"><button class="btn sec" id="later">Guardar para depois</button><a class="btn sec" href="/d/pdf/${DAY}" target="_blank">Pré-visualizar PDF</a></div></div>`;
  $('#eAdd').onclick = async () => { try { await save({edits: [...ed, {from: $('#eFrom').value, to: $('#eTo').value, state: $('#eSt').value}], step: 1}); drawDay(); } catch (e) { fail(e); } };
  main.querySelectorAll('.edit button').forEach(b => b.onclick = async () => { try { await save({edits: ed.filter((_, i) => i !== +b.dataset.i), step: 1}); drawDay(); } catch (e) { fail(e); } });
  $('#next').onclick = async () => { try { await save({step: 2, edits: ed}); STEP = 2; drawDay(); window.scrollTo(0, 0); } catch (e) { fail(e); } };
  $('#later').onclick = async () => { try { await save({step: 1, edits: ed}); DAY = null; TAB = 'days'; render(); } catch (e) { fail(e); } };
}
function step2() {
  const v = D.vehicle;
  const trips = D.trips.map(t => `<div class="trip"><div class="h"><span>Viagem ${t.n}</span><span>${t.start} – ${t.end}</span></div>
    <div class="p">${hm(t.minutes)} · ${kmf(t.km)} km${t.max_kmh ? ' · máx. ' + t.max_kmh + ' km/h' : ''}${t.from ? `<br>${t.from.lat.toFixed(4)}, ${t.from.lon.toFixed(4)} → ${t.to.lat.toFixed(4)}, ${t.to.lon.toFixed(4)}` : ''}</div>
    <input type="text" class="tnote" data-n="${t.n}" placeholder="Nota / motivo da viagem (opcional)" maxlength="200" value="${esc(t.note)}"></div>`).join('');
  $('#step').innerHTML = `<div class="card"><h2>Viatura</h2>
      ${v.auto ? '<p class="small mute" style="margin-top:0">Preenchido automaticamente com o veículo que tem atribuído.</p>' : '<div class="warn">Não tem veículo atribuído. Indique a matrícula.</div>'}
      <label class="f"><span>Matrícula</span><input type="text" id="vPlate" value="${esc(v.plate)}" autocapitalize="characters" maxlength="20"></label>
      <div class="grid2"><label class="f"><span>Conta-km inicial</span><input type="text" inputmode="decimal" id="vO1" value="${esc(v.odo_start || v.odo_start_hint)}"></label>
        <label class="f"><span>Conta-km final</span><input type="text" inputmode="decimal" id="vO2" value="${esc(v.odo_end || v.odo_end_hint)}"></label></div>
      <p class="small mute" style="margin:-4px 0 0">Distância medida pelo GPS: <b>${kmf(D.summary.km)} km</b>. ${v.odo_src === 'estimate' ? 'Valores estimados a partir do odómetro atual da viatura e da distância GPS desde esse dia (pode haver pequenas diferenças) — confirme com o painel.' : v.odo_src === 'gps' ? 'Valores lidos do odómetro da plataforma GPS — confirme com o painel da viatura.' : v.odo_src === 'previous' ? 'O inicial vem do último relatório assinado e o final é sugerido a partir do GPS — confirme com o painel da viatura.' : 'O final é sugerido a partir do inicial mais a distância GPS.'}</p></div>
    <div class="card"><h2>Viagens (${D.trips.length})</h2>${trips || '<span class="mute">Sem viagens neste dia.</span>'}</div>
    <div class="card"><label class="f" style="margin:0"><span>Observações (opcional)</span><textarea id="vNotes" rows="3" maxlength="400">${esc(v.notes)}</textarea></label></div>
    <label class="chk"><input type="checkbox" id="vOk" ${v.confirmed ? 'checked' : ''}><span>Confirmo que os dados da viatura e das viagens estão corretos.</span></label>
    <div class="btns two"><button class="btn sec" id="prev">Voltar</button><button class="btn" id="next">Continuar</button></div>
    <button class="btn sec" id="later" style="margin-top:10px">Guardar para depois</button>`;
  const num = x => parseFloat(String(x).replace(',', '.'));
  const sugg = () => { const a = num($('#vO1').value); if (!isNaN(a) && (!$('#vO2').value || $('#vO2').dataset.auto)) { $('#vO2').value = String(Math.round(a + D.summary.km)); $('#vO2').dataset.auto = 1; } };
  $('#vO1').oninput = sugg; $('#vO2').oninput = () => { delete $('#vO2').dataset.auto; }; if (!v.odo_end && !v.odo_end_hint) sugg();
  const collect = step => ({step, vehicle: {plate: $('#vPlate').value, odo_start: $('#vO1').value, odo_end: $('#vO2').value, notes: $('#vNotes').value, confirmed: $('#vOk').checked},
    trip_notes: Object.fromEntries([...document.querySelectorAll('.tnote')].map(i => [i.dataset.n, i.value]))});
  $('#prev').onclick = async () => { try { await save(collect(1)); STEP = 1; drawDay(); } catch (e) { fail(e); } };
  $('#later').onclick = async () => { try { await save(collect(2)); DAY = null; TAB = 'days'; render(); } catch (e) { fail(e); } };
  $('#next').onclick = async () => {
    if (!$('#vOk').checked) return fail(new Error('Marque a confirmação para continuar.'));
    try { await save(collect(3)); STEP = 3; drawDay(); window.scrollTo(0, 0); } catch (e) { fail(e); } };
}
function step3() {
  $('#step').innerHTML = `<div class="card"><h2>Resumo</h2>${sumBoxes(D.summary)}
      <p class="small mute" style="margin:0">${D.vehicle.plate ? 'Viatura <b>' + esc(D.vehicle.plate) + '</b> · ' : ''}${D.trips.length} viagem${D.trips.length === 1 ? '' : 's'} · ${kmf(D.summary.km)} km</p>
      <a href="/d/pdf/${DAY}" target="_blank" class="small" style="display:inline-block;margin-top:8px">Pré-visualizar PDF</a></div>
    <div class="card"><h2>Assinatura</h2><canvas class="sig" id="sig"></canvas>
      <div class="row between" style="margin-top:8px"><span class="small mute">Assine com o dedo no quadrado.</span><button class="btn sec sm" id="clr">Limpar</button></div></div>
    <p class="small mute">Ao assinar, o relatório fica fechado e já não pode ser alterado.</p>
    <div class="btns two"><button class="btn sec" id="prev">Voltar</button><button class="btn" id="sign">Assinar e concluir</button></div>`;
  const cv = $('#sig'), ratio = Math.max(window.devicePixelRatio || 1, 1);
  cv.width = cv.clientWidth * ratio; cv.height = cv.clientHeight * ratio;
  const ctx = cv.getContext('2d'); ctx.scale(ratio, ratio); ctx.lineWidth = 2.6; ctx.lineCap = ctx.lineJoin = 'round'; ctx.strokeStyle = '#0f172a';
  let drawing = false, drawn = false, last = null;
  const pos = e => { const r = cv.getBoundingClientRect(); return {x: e.clientX - r.left, y: e.clientY - r.top}; };
  cv.onpointerdown = e => { drawing = true; last = pos(e); cv.setPointerCapture(e.pointerId); ctx.beginPath(); ctx.arc(last.x, last.y, 1.2, 0, 6.3); ctx.fill(); drawn = true; };
  cv.onpointermove = e => { if (!drawing) return; const p = pos(e); ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke(); last = p; };
  cv.onpointerup = cv.onpointercancel = () => { drawing = false; };
  $('#clr').onclick = () => { ctx.clearRect(0, 0, cv.width, cv.height); drawn = false; };
  $('#prev').onclick = async () => { try { await save({step: 2}); STEP = 2; drawDay(); } catch (e) { fail(e); } };
  $('#sign').onclick = async () => {
    if (!drawn) return fail(new Error('Assine no quadrado antes de concluir.'));
    const b = $('#sign'); b.disabled = true; b.textContent = 'A assinar…';
    try { D = await api(`/d/api/day/${DAY}/sign`, {method: 'POST', json: {signature: cv.toDataURL('image/png')}}); STEP = 0; drawDay(); window.scrollTo(0, 0); }
    catch (e) { b.disabled = false; b.textContent = 'Assinar e concluir'; fail(e); }
  };
}
function drawSigned() {
  main.innerHTML = `<div class="ok"><div class="big">✓</div><h2 style="margin:0">Relatório assinado</h2><p class="mute small">${new Date(D.signed_at).toLocaleString('pt-PT')}</p></div>
    ${sumBoxes(D.summary)}<div class="card">${bar(D.bar)}</div><div class="card"><h2>Estados do dia</h2>${segs()}</div>${notesBox()}
    <a class="btn" href="/d/pdf/${DAY}" target="_blank">Descarregar PDF</a>`;
}
render();
