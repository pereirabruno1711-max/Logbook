/* App do motorista — estado (Trabalho/Descanso), lista de diários e assinatura em 3 passos */
const $ = s => document.querySelector(s);
const esc = s => (s ?? '').toString().replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const STL = {driving: 'Condução', working: 'Trabalho', resting: 'Descanso'};
const hm = m => Math.floor((m || 0) / 60) + 'h' + String((m || 0) % 60).padStart(2, '0');
const fmtDay = d => new Date(d + 'T12:00:00').toLocaleDateString('pt-PT', {weekday: 'long', day: 'numeric', month: 'long'});
const kmf = v => (v ?? 0).toLocaleString('pt-PT', {minimumFractionDigits: 1, maximumFractionDigits: 1});
const main = $('#main'), back = $('#back');
let ME = null, TAB = 'home', DAY = null, D = null, STEP = 1, busy = false;

async function api(url, opts = {}) {
  const o = Object.assign({headers: {}}, opts);
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers['Content-Type'] = 'application/json'; delete o.json; }
  const r = await fetch(url, o);
  if (r.status === 401) { location.href = '/d'; throw new Error('Sessão terminada.'); }
  const data = r.headers.get('content-type')?.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && (data.erro || data.detail)) || r.statusText);
  return data;
}
const fail = e => { main.insertAdjacentHTML('afterbegin', `<div class="err">${esc(e.message)}</div>`); window.scrollTo(0, 0); };
const bar = (b) => `<div class="tl">${[...b].map(c => `<i class="${c === '-' ? 'f' : c}"></i>`).join('')}</div>
  <div class="tlh"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>`;
const sumBoxes = s => `<div class="sum"><div class="d"><b>${hm(s.driving)}</b><span>Condução</span></div><div class="w"><b>${hm(s.working)}</b><span>Trabalho</span></div><div class="r"><b>${hm(s.resting)}</b><span>Descanso</span></div></div>`;

function setHeader(title, sub, showBack) {
  $('#hTitle').textContent = title; $('#hSub').textContent = sub; back.style.display = showBack ? '' : 'none';
  $('#tabs').style.display = showBack ? 'none' : '';
}
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => { TAB = b.dataset.tab; render(); });
back.onclick = () => { DAY = null; render(); };

async function render() {
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === TAB));
  try {
    if (!ME) ME = await api('/d/api/me');
    if (DAY) return renderDay();
    setHeader(ME.app, `${ME.driver.name} · ${ME.company}`, false);
    return TAB === 'home' ? renderHome() : renderDays();
  } catch (e) { fail(e); }
}

// ---------- início ----------
async function renderHome() {
  ME = await api('/d/api/me');
  const st = ME.state.state;
  const today = await api('/d/api/day/' + ME.today);
  const days = await api('/d/api/days');
  const pend = days.filter(d => d.can_sign && d.status !== 'signed').slice(0, 7).length;
  main.innerHTML = `
    <div class="card"><h2>O meu estado</h2>
      <div class="state">
        <button class="working ${st === 'working' ? 'on' : ''}" data-s="working">Trabalho<small>em serviço</small></button>
        <button class="resting ${st === 'resting' ? 'on' : ''}" data-s="resting">Descanso<small>fora de serviço</small></button>
      </div>
      <p class="small mute" style="margin:12px 0 0">${ME.has_vehicle ? 'A <b>condução</b> é detetada automaticamente pelo GPS do veículo atribuído.' : '<b>Sem veículo atribuído</b> — peça ao seu gestor para lhe atribuir um objeto GPS.'}</p></div>
    <div class="card"><h2>Hoje</h2>${sumBoxes(today.summary)}${bar(today.bar)}</div>
    ${pend ? `<div class="warn">Tem <b>${pend}</b> dia${pend > 1 ? 's' : ''} por assinar nos últimos 7 dias.</div><button class="btn" id="goDays">Ver diário</button>` : '<div class="info">Está tudo assinado. Bom trabalho!</div>'}`;
  main.querySelectorAll('.state button').forEach(b => b.onclick = async () => {
    try { await api('/d/api/state', {method: 'POST', json: {state: b.dataset.s}}); renderHome(); } catch (e) { fail(e); } });
  const g = $('#goDays'); if (g) g.onclick = () => { TAB = 'days'; render(); };
}

// ---------- lista ----------
const PILL = {none: 'Sem relatório', filling: 'Em preenchimento', signed: 'Assinado'};
async function renderDays() {
  const days = await api('/d/api/days');
  main.innerHTML = days.map(d => `<div class="day" data-d="${d.day}"><div class="t"><b>${d.today ? 'Hoje' : fmtDay(d.day)}</b><span>${d.today ? fmtDay(d.day) : d.day.split('-').reverse().join('/')}</span></div>
    <span class="pill ${d.status}">${PILL[d.status]}</span></div>`).join('');
  main.querySelectorAll('.day').forEach(el => el.onclick = () => { DAY = el.dataset.d; STEP = 0; render(); });
}

// ---------- dia ----------
async function renderDay() {
  main.innerHTML = '<div class="card mute">A carregar…</div>';
  D = await api('/d/api/day/' + DAY);
  if (!STEP) STEP = D.status === 'signed' || !D.editable ? 0 : (D.step || 1);
  drawDay();
}
async function save(patch) { D = await api('/d/api/day/' + DAY, {method: 'POST', json: patch}); }
function head() {
  setHeader(D.is_today ? 'Hoje' : fmtDay(DAY), `${D.driver.name} · ${PILL[D.status]}`, true);
}
function stepper() {
  return `<div class="steps">${['Dia', 'Viatura', 'Assinatura'].map((t, i) => `<div class="${STEP === i + 1 ? 'on' : STEP > i + 1 ? 'done' : ''}">${i + 1}. ${t}</div>`).join('')}</div>`;
}
function drawDay() {
  head();
  if (D.status === 'signed') return drawSigned();
  if (!D.editable) return drawToday();
  main.innerHTML = stepper() + (D.gps_ok ? '' : `<div class="warn">${esc(D.gps_error)} Os valores abaixo podem estar incompletos.</div>`) + '<div id="step"></div>';
  ({1: step1, 2: step2, 3: step3})[STEP]();
}
function drawToday() {
  main.innerHTML = `<div class="info">O dia ainda não terminou. Só pode assinar depois da meia-noite.</div>${sumBoxes(D.summary)}<div class="card">${bar(D.bar)}</div>
    <div class="card"><h2>Estados</h2>${segs()}</div><a class="btn sec" href="/d/pdf/${DAY}" target="_blank">Pré-visualizar PDF</a>`;
}
function segs() {
  return D.segments.map(s => `<div class="seg"><span class="dot ${s.state}"></span><div class="t"><b>${STL[s.state]}</b><span>${s.start} – ${s.end}${s.locked ? ' · automático' : ''}</span></div><span class="du">${hm(s.minutes)}</span></div>`).join('') || '<span class="mute">Sem dados.</span>';
}

function step1() {
  const ed = D.edits || [];
  $('#step').innerHTML = `${sumBoxes(D.summary)}<div class="card">${bar(D.bar)}</div>
    <div class="card"><h2>Estados do dia</h2>${segs()}</div>
    <div class="card"><h2>Corrigir um período</h2>
      <p class="small mute" style="margin-top:0">Pode alterar períodos de <b>Trabalho</b> e <b>Descanso</b>. A condução (a azul) é detetada pelo GPS e não se altera.</p>
      <div class="grid3"><label class="f"><span>De</span><input type="time" id="eFrom" value="08:00"></label><label class="f"><span>Até</span><input type="time" id="eTo" value="09:00"></label>
        <label class="f"><span>Estado</span><select id="eSt"><option value="working">Trabalho</option><option value="resting">Descanso</option></select></label></div>
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
      <div class="grid2"><label class="f"><span>Conta-km inicial</span><input type="text" inputmode="decimal" id="vO1" value="${esc(v.odo_start)}"></label>
        <label class="f"><span>Conta-km final</span><input type="text" inputmode="decimal" id="vO2" value="${esc(v.odo_end)}"></label></div>
      <p class="small mute" style="margin:-4px 0 0">Distância medida pelo GPS: <b>${kmf(D.summary.km)} km</b></p></div>
    <div class="card"><h2>Viagens (${D.trips.length})</h2>${trips || '<span class="mute">Sem viagens neste dia.</span>'}</div>
    <div class="card"><label class="f" style="margin:0"><span>Observações (opcional)</span><textarea id="vNotes" rows="3" maxlength="400">${esc(v.notes)}</textarea></label></div>
    <label class="chk"><input type="checkbox" id="vOk" ${v.confirmed ? 'checked' : ''}><span>Confirmo que os dados da viatura e das viagens estão corretos.</span></label>
    <div class="btns two"><button class="btn sec" id="prev">Voltar</button><button class="btn" id="next">Continuar</button></div>
    <button class="btn sec" id="later" style="margin-top:10px">Guardar para depois</button>`;
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
    ${sumBoxes(D.summary)}<div class="card">${bar(D.bar)}</div><div class="card"><h2>Estados do dia</h2>${segs()}</div>
    <a class="btn" href="/d/pdf/${DAY}" target="_blank">Descarregar PDF</a>`;
}

render();
