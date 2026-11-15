// Legion orchestrator UI. Served with a CSP that forbids inline script.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Event handlers are never inlined (the page's CSP forbids inline script): elements carry a handler
// name and JSON-encoded arguments, dispatched by one delegated listener (see bottom of file).
const on = (name, ...args) => `data-on="${esc(name)}" data-args="${esc(JSON.stringify(args))}"`;
const STATUSES = ['PENDING','READY','RUNNING','VERIFYING','REVIEWING','MERGING','REWORK','AWAITING_HUMAN','ESCALATED','ACCEPTED','FAILED'];
const color = s => getComputedStyle(document.documentElement).getPropertyValue('--'+s).trim() || '#888';
let S = null, selected = null, tab = 'gates', docSel = null;
$('#approver').value = localStorage.approver || '';
$('#optoken').value = sessionStorage.optoken || '';   // not persisted across browser sessions
$('#approver').onchange = e => localStorage.approver = e.target.value;
$('#optoken').onchange = e => sessionStorage.optoken = e.target.value;

async function api(path, body) {
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json','X-Operator-Token':$('#optoken').value}, body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data.error || data);
  return data;
}

let META = null, CHANGES = [], CID = location.hash.slice(1) || localStorage.cid || '', showStart = false;
const EMPTY = {change:null, nodes:[], runs:[], gates:[]};
const docPath = rel => CID ? `changes/${CID}/${rel}` : rel;
async function loadState() {
  META = await api('/api/meta');
  CHANGES = await api('/api/changes');
  if (!CHANGES.find(c => c.id === CID)) CID = CHANGES.length ? CHANGES[0].id : '';
  const st = CID ? await api(`/api/changes/${CID}/state`) : EMPTY;
  return Object.assign({}, st, {project: META.project, repo: META.repo, assets_sha: META.assets_sha});
}
function pickChange(id) { CID = id; location.hash = id; localStorage.cid = id; selected = null; docSel = null; showStart = false; refresh(); }
async function refresh() {
  S = await loadState();
  renderHeader(); renderPipeline(); renderGraph(); renderLive(); renderDetail(); renderTab();
}

// Regions redrawn on every state update keep what the operator opened: unchanged HTML is not
// rewritten at all, and when it does change, expanded <details> (by data-key, else summary text)
// and scroll positions of keyed elements are restored.
function setHtml(el, html) {
  if (el._html === html && el._first === el.firstChild) return;   // (others may write el too)
  const keyOf = d => d.dataset.key || d.querySelector('summary')?.textContent || '';
  const open = new Set([...el.querySelectorAll('details')].filter(d => d.open).map(keyOf));
  const scroll = Object.fromEntries([...el.querySelectorAll('[data-key]')].filter(x => x.scrollTop).map(x => [x.dataset.key, x.scrollTop]));
  el.innerHTML = html;
  el._html = html; el._first = el.firstChild;
  el.querySelectorAll('details').forEach(d => { if (open.has(keyOf(d))) d.open = true; });
  el.querySelectorAll('[data-key]').forEach(x => { if (scroll[x.dataset.key]) x.scrollTop = scroll[x.dataset.key]; });
}
// A form field in this region has focus (redrawing would drop what is being typed).
function typingIn(sel) {
  const a = document.activeElement;
  return !!a && ['TEXTAREA','INPUT','SELECT'].includes(a.tagName) && !!a.closest(sel);
}
function badge(s) { return `<span class="badge" style="background:${color(s)}">${esc(s)}</span>`; }
const STAGE_COLORS = {PREPARING:'#a78bfa', LAUNCHING:'#fbbf24', RUNNING:'#3b82f6', EXITED:'#6b7280'};
const STAGE_TEXT = {PREPARING:'preparing sandbox', LAUNCHING:'launching agent', RUNNING:'agent connected', EXITED:'exited'};
function stageBadge(r) { return r.stage ? `<span class="badge" title="${esc(r.stage)}" style="background:${STAGE_COLORS[r.stage]||'#888'}">${esc(STAGE_TEXT[r.stage]||r.stage)}</span>` : ''; }
const SOURCE_TEXT = {AGENT:'agent error', SYSTEM:'system error', TIMEOUT:'timeout', CONTROLLER:'controller error'};
function sourceBadge(src) { return src ? `<span class="badge src-${esc(src)}">${esc(SOURCE_TEXT[src]||src)}</span>` : ''; }
function failureHtml(f) {
  if (!f) return '';
  return `<div>${sourceBadge(f.source)} ${f.stage?`<span class="muted">while ${esc(STAGE_TEXT[f.stage]||f.stage)}</span>`:''}${f.exit_code!=null?` <span class="muted">exit ${f.exit_code}</span>`:''}
    <div class="${f.source==='AGENT'?'':'err'}">${esc(f.reason)}</div></div>`;
}
function short(sha) { return sha ? `<code>${sha.slice(0,10)}</code>` : '—'; }

function renderHeader() {
  const c = S.change;
  $('#startbox').style.display = (showStart || !CHANGES.length) ? '' : 'none';
  $('#changeSel').innerHTML = CHANGES.length ? CHANGES.map(x => `<option value="${esc(x.id)}" ${x.id===CID?'selected':''}>${esc(x.id)} · ${esc(x.status)}${x.tier?' · '+x.tier:''}${x.live_runs?' · '+x.live_runs+' live':''} · ${esc(x.goal.slice(0,40))}</option>`).join('') : '<option>no changes yet</option>';
  if (!c) { $('#hdr').innerHTML = `${esc(S.project)} · repo <b>${esc(S.repo)}</b> · no changes`; return; }
  $('#hdr').innerHTML = `${esc(S.project)} · <b>${esc(c.id)}</b> · ${badge(c.status)} · tier ${c.tier ? `<b>${c.tier}</b>` : '<i>unassessed</i>'}
    · base ${short(c.base_sha)} · integration ${short(c.integration_head)} · <span class="muted" title="${esc(c.goal)}">${esc(c.goal.slice(0,90))}</span>`;
}

function renderPipeline() {
  const stageNodes = S.nodes.filter(n => n.kind !== 'build');
  const builds = S.nodes.filter(n => n.kind === 'build');
  let html = '', insertedBuild = false;
  for (const n of stageNodes) {
    if (!insertedBuild && ['integration','release','H-3','ship'].includes(n.id) && builds.length) {
      const acc = builds.filter(b => b.status === 'ACCEPTED').length;
      html += `<span class="stage"><span class="dot" style="background:${color(acc===builds.length?'ACCEPTED':'RUNNING')}"></span>build ${acc}/${builds.length}</span><span class="arrow">→</span>`;
      insertedBuild = true;
    }
    html += `<span class="stage" ${on('select', n.id)}><span class="dot" style="background:${color(n.status)}"></span>${esc(n.kind==='human'?n.id+' '+n.title:n.title.split(':')[0])}</span><span class="arrow">→</span>`;
  }
  $('#pipeline').innerHTML = html.replace(/<span class="arrow">→<\/span>$/, '') || '<span class="muted">—</span>';
  const counts = {}; S.nodes.forEach(n => counts[n.status] = (counts[n.status]||0)+1);
  const total = S.nodes.length || 1;
  $('#progressbar').innerHTML = STATUSES.filter(s => counts[s]).map(s => `<div title="${s}: ${counts[s]}" style="width:${100*counts[s]/total}%;background:${color(s)}"></div>`).join('');
  const acc = counts.ACCEPTED || 0;
  $('#progresstext').innerHTML = `${acc}/${S.nodes.length} nodes accepted · ` + STATUSES.filter(s => counts[s]).map(s => `${s.toLowerCase()} ${counts[s]}`).join(' · ')
    + ` · ${S.runs.length} run(s), ${S.runs.filter(r => ['STARTING','ACTIVE'].includes(r.status)).length} live`;
}

// Layered DAG layout (Sugiyama-style). Columns are dependency depth. Edges implied through
// another dependency are not drawn (transitive reduction); an edge spanning several columns gets
// a thin lane of its own in each column it crosses, so no edge is drawn through a box; rows are
// ordered by barycenter sweeps to reduce crossings.
function layout(nodes) {
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const deps = id => (byId[id] ? byId[id].depends_on : []).filter(x => byId[x] && x !== id);
  const depth = {}, anc = {};
  const d = id => { if (depth[id] !== undefined) return depth[id]; depth[id] = 0;
    return depth[id] = Math.max(0, ...deps(id).map(x => d(x) + 1)); };
  const ancestors = id => { if (anc[id]) return anc[id]; anc[id] = new Set();
    for (const x of deps(id)) { anc[id].add(x); ancestors(x).forEach(a => anc[id].add(a)); } return anc[id]; };
  nodes.forEach(n => d(n.id));
  // transitive reduction: drop dep x of n when another dep of n already depends on x
  const kept = [];
  for (const n of nodes) { const ds = deps(n.id);
    for (const x of ds) if (!ds.some(o => o !== x && ancestors(o).has(x))) kept.push([x, n.id]); }
  // layers of real and virtual items; long edges become chains of virtual items
  const ncol = Math.max(...nodes.map(n => depth[n.id])) + 1;
  const layers = Array.from({length: ncol}, () => []);
  const item = {};                              // id -> {id, layer, virtual, up: [], down: []}
  const add = (id, layer, virtual) => { item[id] = {id, layer, virtual, up: [], down: []}; layers[layer].push(id); };
  nodes.forEach(n => add(n.id, depth[n.id], false));
  const chains = [];
  kept.forEach(([a, b], k) => {
    const chain = [a];
    for (let L = depth[a] + 1; L < depth[b]; L++) { const v = `~${k}:${L}`; add(v, L, true); chain.push(v); }
    chain.push(b);
    for (let i = 0; i + 1 < chain.length; i++) { item[chain[i]].down.push(chain[i+1]); item[chain[i+1]].up.push(chain[i]); }
    chains.push({from: a, to: b, chain});
  });
  // order rows: barycenter sweeps down and up (ties keep the current order)
  const rank = () => { const r = {}; layers.forEach(l => l.forEach((id, i) => r[id] = i)); return r; };
  const sweep = (L, side) => { const r = rank();
    const bc = id => { const ns = item[id][side]; return ns.length ? ns.reduce((s, x) => s + r[x], 0) / ns.length : r[id]; };
    layers[L] = layers[L].map((id, i) => [bc(id), i, id]).sort((p, q) => p[0] - q[0] || p[1] - q[1]).map(x => x[2]); };
  for (let it = 0; it < 6; it++) {
    for (let L = 1; L < ncol; L++) sweep(L, 'up');
    for (let L = ncol - 2; L >= 0; L--) sweep(L, 'down');
  }
  // coordinates: boxes H tall, lanes VH tall; each column centred on the tallest
  const W = 178, H = 46, GX = 54, GY = 14, VH = 8, GV = 6, pos = {};
  const tall = id => item[id].virtual ? VH : H, gap = id => item[id].virtual ? GV : GY;
  const colH = layers.map(l => l.reduce((s, id) => s + tall(id) + gap(id), 0));
  const maxH = Math.max(...colH);
  layers.forEach((l, L) => { let y = 12 + (maxH - colH[L]) / 2;
    l.forEach(id => { pos[id] = {x: 12 + L * (W + GX), y}; y += tall(id) + gap(id); }); });
  // edge routes: box right-middle -> through each lane (straight across the column) -> box left-middle
  const edges = chains.map(({from, to, chain}) => {
    const pts = [[pos[from].x + W, pos[from].y + H / 2]];
    chain.slice(1, -1).forEach(v => { const cy = pos[v].y + VH / 2; pts.push([pos[v].x, cy], [pos[v].x + W, cy]); });
    pts.push([pos[to].x, pos[to].y + H / 2]);
    let path = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i-1], [x1, y1] = pts[i];
      path += (i % 2) ? ` C${(x0+x1)/2},${y0} ${(x0+x1)/2},${y1} ${x1},${y1}` : ` L${x1},${y1}`;
    }
    return {from, to, path};
  });
  const real = Object.fromEntries(nodes.map(n => [n.id, pos[n.id]]));
  return {pos: real, W, H, edges, width: 24 + ncol * (W + GX), height: 24 + maxH};
}

function renderGraph() {
  const svg = $('#graph');
  if (!S.nodes.length) { svg.innerHTML = ''; svg.setAttribute('width', 10); svg.setAttribute('height', 10); return; }
  const {pos, W, H, edges: routes, width, height} = layout(S.nodes);
  svg.setAttribute('width', width); svg.setAttribute('height', height);
  let edges = '', boxes = '';
  const byId = Object.fromEntries(S.nodes.map(n => [n.id, n]));
  for (const e of routes)
    edges += `<path class="edge ${byId[e.from].status==='ACCEPTED'?'done':''}" d="${e.path}"/>`;
  for (const n of S.nodes) {
    const p = pos[n.id], c = color(n.status);
    const live = S.runs.filter(r => r.node_id === n.id && ['STARTING','ACTIVE'].includes(r.status));
    const sub = `${n.status.toLowerCase()}${n.cycle>1?' · cycle '+n.cycle:''}${live.length?' · '+live.map(r=>r.role).join('+'):''}`;
    const title = n.kind==='human' ? `${n.id} · ${n.title}` : n.title;
    boxes += `<g class="node ${n.status} ${selected===n.id?'sel':''}" ${on('select', n.id)}>
      <rect x="${p.x}" y="${p.y}" width="${W}" height="${H}" rx="7" fill="${c}22" stroke="${c}"/>
      <rect x="${p.x}" y="${p.y}" width="5" height="${H}" rx="2" fill="${c}"/>
      <text x="${p.x+12}" y="${p.y+18}" font-weight="600">${esc(title.length>26?title.slice(0,25)+'…':title)}</text>
      <text x="${p.x+12}" y="${p.y+35}" fill="${c}" style="fill:${c}">${esc(sub)}</text>
      <title>${esc(n.title)}\n${n.status}</title></g>`;
  }
  svg.innerHTML = edges + boxes;
  $('#legend').innerHTML = STATUSES.map(s => `<span style="margin-right:10px"><span class="dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color(s)}"></span> ${s.toLowerCase()}</span>`).join('');
}

function renderLive() {
  const live = S.runs.filter(r => ['STARTING','ACTIVE'].includes(r.status));
  setHtml($('#livebody'), live.length ? live.map(r => {
    const p = r.progress[r.progress.length-1];
    return `<div class="card"><div class="row"><b>${esc(r.id)}</b> ${stageBadge(r) || badge(r.status)} <span>${esc(r.role)}</span> <a ${on('select', r.node_id)}>${esc(r.node_id)}</a> <span style="flex:1"></span><a ${on('showLog', r.id)}>log</a></div>
      <div class="muted">soul ${esc(r.soul)} · sandbox <code>${esc(r.sandbox)}</code> · reports ${r.attempts}</div>
      ${p ? `<div>${p.percent!=null?`<b>${Math.round(p.percent)}%</b> `:''}${esc(p.message)}</div>` : `<div class="muted">${esc(STAGE_TEXT[r.stage]||'starting')}…</div>`}</div>`;
  }).join('') : '<span class="muted">none</span>');
}

function select(id) { selected = id; renderGraph(); renderDetail(); }

function findingsHtml(fs) {
  if (!fs || !fs.length) return '<div class="muted">no findings</div>';
  return fs.map(f => `<div class="card"><b class="sev-${esc(f.severity)}">${esc(f.severity)}</b> <span class="pill">${esc(f.confidence)}</span> <b>${esc(f.title)}</b>
    <div>${esc(f.observation||'')}</div><div class="muted">impact: ${esc(f.impact||'')}</div><div>fix: ${esc(f.fix||'')}</div>
    <div class="mono muted">${(f.attestations||[]).map(esc).join('<br>')}</div></div>`).join('');
}

function actionForm(n) {
  const c = S.change;
  if (n.status === 'ESCALATED') {
    const e = n.escalation || {};
    if (e.retrying) return `<div class="card" style="border-color:var(--ESCALATED)"><b>Retrying</b> <span class="muted">removing the old sandbox, then dispatching a new run…</span></div>`;
    return `<div class="card" style="border-color:var(--ESCALATED)"><b>Escalation: ${esc(e.kind)}</b> ${sourceBadge(e.source)}<div>${esc(e.reason)}</div>
      ${e.run ? `<a ${on('showLog', e.run)}>agent log of ${esc(e.run)}</a>` : ''}
      ${e.missing_decision ? `<div><b>Decision needed:</b> ${esc(e.missing_decision)}</div>` : ''}
      ${e.log_tail ? `<details data-key="esc-log"><summary class="muted">last log lines</summary><pre data-key="esc-log-pre">${esc(e.log_tail)}</pre></details>` : ''}
      <textarea id="f_text" placeholder="answer / decision (attested as feedback)"></textarea>
      <input id="f_reason" placeholder="override reason (≥24 chars, build tasks only)">
      <button class="act" ${on('act', n.id, 'answer', 'answer')}>Answer &amp; resume</button>
      <button class="act warn" title="removes the failed run's sandbox, then starts a new run" ${on('act', n.id, 'retry', 'none')}>Retry</button>
      ${n.kind==='build'?`<button class="act warn" ${on('act', n.id, 'override_accept', 'override')}>Override accept</button>`:''}
      <button class="act danger" ${on('act', n.id, 'abandon', 'none', null, 'Abandon the change?')}>Abandon</button></div>`;
  }
  if (n.status !== 'AWAITING_HUMAN' && !(n.kind==='ship' && n.status==='ACCEPTED')) return '';
  if (n.id === 'H-1') {
    const r = c.risk || {};
    return `<div class="card"><b>Accept requirements</b><div class="muted">derived tier <b>${r.tier}</b> (score ${r.total}; threshold ${r.threshold_tier}; floors ${(r.floors||[]).map(f=>f.signal+'→'+f.tier).join(', ')||'none'})</div>
      <table>${Object.entries(r.scores||{}).map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join('')}</table>
      <div class="grid2"><select id="f_tier"><option value="">keep ${r.tier}</option>${['R0','R1','R2','R3'].map(t=>`<option>${t}</option>`).join('')}</select>
      <input id="f_reason" placeholder="reason (required to lower)"></div>
      <a ${on('openDoc', docPath('intake/requirements.md'))}>view requirements</a> · <a ${on('openDoc', docPath('intake/risk.json'))}>risk.json</a> · <a ${on('openDoc', docPath('intake/questions.md'))}>questions</a>
      <textarea id="f_text" placeholder="feedback for revision"></textarea>
      <button class="act ok" ${on('act', 'H-1', 'accept', 'h1accept')}>Accept</button>
      <button class="act warn" ${on('act', 'H-1', 'revise', 'feedback')}>Request revision</button></div>`;
  }
  if (n.id === 'H-2') {
    const subj = (S.h2||[]).map(x => `<tr><td><b>${esc(x.subject)}</b></td><td class="mono">${x.paths.map(p=>`<a ${on('openDoc', p)}>${esc(p.split('/').slice(2).join('/'))}</a>`).join('<br>')}</td>
      <td class="${x.status==='approved'?'gate-satisfied':x.status==='stale'?'gate-unsatisfied':'gate-unevaluable'}">${x.status}${x.approver?' · '+esc(x.approver):''}</td>
      <td>${x.status!=='approved'?`<button class="act ok" ${on('act', 'H-2', 'approve', 'subjects', [x.subject])}>Approve</button>`:''}</td></tr>`).join('');
    return `<div class="card"><b>Approve spec &amp; oracle</b> <span class="muted">(each delta spec, the plan and the oracle are separate decisions; all must be approved before build${c.tier==='R3'?'; R3 also needs an independent-baseline attestation first':''})</span>
      <table>${subj}</table> oracle commit ${short(c.oracle_sha)}
      <div><button class="act ok" ${on('act', 'H-2', 'approve', 'subjects', 'all')}>Approve all pending</button></div>
      <textarea id="f_text" placeholder="feedback for revision"></textarea>
      <button class="act warn" ${on('act', 'H-2', 'revise', 'feedback')}>Request revision</button></div>`;
  }
  if (n.id === 'H-3') return `<div class="card"><b>Accept the whole change</b> at integration head ${short(c.integration_head)}
      <textarea id="f_text" placeholder="note"></textarea>
      <button class="act ok" ${on('act', 'H-3', 'accept', 'feedback')}>Accept change</button>
      <button class="act danger" ${on('act', 'H-3', 'reject', 'none')}>Reject</button></div>`;
  if (n.kind === 'ship') return `<button class="act" ${on('act', 'ship', 'recheck', 'none')}>Re-run readiness check</button>`;
  return '';
}

function renderDetail() {
  const n = S.nodes.find(x => x.id === selected);
  if (!n) { setHtml($('#detail'), '<span class="muted">Select a node in the graph.</span>'); return; }
  const runs = S.runs.filter(r => r.node_id === n.id).reverse();
  let h = `<div class="row"><b style="font-size:14px">${esc(n.title)}</b> ${badge(n.status)}</div>
    <div class="kv" style="margin:6px 0"><div>id</div><div><code>${esc(n.id)}</code> · ${esc(n.kind)}${n.wave?` · wave ${n.wave}`:''}</div>
    <div>depends on</div><div>${n.depends_on.map(d=>`<a ${on('select', d)}>${esc(d)}</a>`).join(', ')||'—'}</div>
    <div>cycle</div><div>${n.cycle}${n.extra_cycles?` (+${n.extra_cycles} granted)`:''}</div>
    ${n.base_sha?`<div>base</div><div>${short(n.base_sha)}</div>`:''}
    ${n.candidate_sha?`<div>candidate</div><div>${short(n.candidate_sha)}</div>`:''}
    ${n.accepted_sha?`<div>accepted</div><div>${short(n.accepted_sha)}</div>`:''}
    ${n.sandbox?`<div>sandbox</div><div><code>${esc(n.sandbox)}</code></div>`:''}</div>`;
  h += actionForm(n);
  if (n.task) h += `<details data-key="task"><summary>Task contract</summary><pre data-key="task-pre">${esc(JSON.stringify(n.task, null, 2))}</pre></details>`;
  const ver = Object.entries(n.verification||{});
  if (ver.length) h += `<h2 style="margin-top:10px">Verification</h2>` + ver.map(([k,v]) => `<div class="card">${k==='integration'?'integration':'cycle '+k}: ${v.ok?'<b class="gate-satisfied">PASS</b>':'<b class="gate-unsatisfied">FAIL</b>'} at ${short(v.sha)} ${v.infra_error?`<span class="err">${esc(v.infra_error)}</span>`:''}
      <div class="mono">${(v.results||[]).map(r=>`${r.exit_code===0?'✓':'✗'} ${esc(r.command)} → ${r.exit_code}`).join('<br>')}</div>
      ${v.attestation?`<a ${on('openAtt', v.attestation)}>${esc(v.attestation)}</a>`:''}</div>`).join('');
  const rev = Object.entries(n.reviews||{});
  if (rev.length) h += `<h2 style="margin-top:10px">Reviews</h2>` + rev.map(([k,r]) => `<div class="card"><b>${esc(k)}</b> ${r.verdict==='PASS'?'<b class="gate-satisfied">PASS</b>':'<b class="gate-unsatisfied">'+esc(r.verdict)+'</b>'} at ${short(r.sha)} ${r.doc?`<a ${on('openAtt', r.doc)}>${esc(r.doc)}</a>`:''}
      ${findingsHtml(r.findings)}</div>`).join('');
  if (n.feedback.length) h += `<h2 style="margin-top:10px">Feedback (attested)</h2>` + n.feedback.map(f => `<div class="card"><span class="pill">cycle ${f.cycle}</span> <span class="pill">${esc(f.source)}</span> ${esc(f.text)}
      <div class="mono muted">${(f.attestations||[]).map(a=>`<a ${on('openAtt', a)}>${esc(a)}</a>`).join('<br>')}</div></div>`).join('');
  if (runs.length) h += `<h2 style="margin-top:10px">Runs</h2>` + runs.map(r => `<div class="card"><div class="row"><b>${r.id}</b> ${badge(r.status)} ${['STARTING','ACTIVE'].includes(r.status)?stageBadge(r):''} ${esc(r.role)} · cycle ${r.cycle} · ${esc(r.soul)}</div>
      ${failureHtml(r.failure)}
      <div class="muted mono">sandbox ${esc(r.sandbox)}${r.sandbox_deleted?' (deleted)':''} · pin ${esc((r.prompt_pin||'').slice(7,19))} · branch ${esc(r.branch)}</div>
      ${r.progress.slice(-4).map(p=>`<div>· ${p.percent!=null?Math.round(p.percent)+'% ':''}${esc(p.message)}</div>`).join('')}
      ${r.report?`<div><b>${esc(r.report.status)}</b>: ${esc(r.report.summary)}</div><div class="mono muted">${r.report.attestations.map(a=>`<a ${on('openAtt', a)}>${esc(a)}</a>`).join('<br>')}</div>`:''}
      ${r.violations.length?`<details data-key="viol-${esc(r.id)}"><summary class="err">${r.violations.length} rejected report(s)</summary><pre data-key="viol-pre-${esc(r.id)}">${esc(r.violations.map(v=>v.join('\n')).join('\n---\n'))}</pre></details>`:''}
      <div class="muted mono">assets ${esc((r.assets_sha||'').slice(7,19))} · prompt ${esc((r.prompt_pin||'').slice(7,19))}</div>
      <a ${on('showLog', r.id)}>agent log</a></div>`).join('');
  setHtml($('#detail'), h);
}

// Payloads read the form at click time; keyed so handlers need no inline code.
const PAYLOADS = {
  none: () => ({}),
  answer: () => ({answer: $('#f_text').value}),
  override: () => ({reason: $('#f_reason').value}),
  feedback: () => ({feedback: $('#f_text').value}),
  h1accept: () => ({tier: $('#f_tier').value || null, reason: $('#f_reason').value}),
  subjects: subjects => ({subjects}),
  attest: () => ({kind: $('#g_kind').value, verdict: $('#g_verdict').value, sources: csv('#g_sources'),
                  covers: csv('#g_covers').length ? csv('#g_covers') : null, waiver_reason: $('#g_waiver').value || null}),
  override_gate: () => ({kind: $('#o_kind').value, gate: $('#o_gate').value, reason: $('#o_reason').value,
                         evidence: $('#o_evidence').value}),
  protected: () => ({task: $('#p_task').value, paths: csv('#p_paths'), reason: $('#p_reason').value}),
  surface: surface => ({surface, reason: prompt('Why does this surface still describe the real interface? (≥24 chars)') || ''}),
};
function actClick(node, action, payloadKey, arg, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  act(node, action, PAYLOADS[payloadKey](arg));
}
function governClick(action, payloadKey, arg) { govern(action, PAYLOADS[payloadKey](arg)); }
async function act(node, action, payload) {
  const approver = $('#approver').value.trim();
  if (!approver) { alert('Enter your approver id (top right).'); return; }
  try { const r = await api(`/api/changes/${CID}/nodes/${encodeURIComponent(node)}/action`, {action, approver, payload}); if (r.note) alert(r.note); await refresh(); }
  catch (e) { alert(e.message); }
}
async function startChange() {
  try { const r = await api('/api/changes', {goal: $('#goal').value}); $('#starterr').textContent=''; $('#goal').value=''; pickChange(r.change); }
  catch (e) { $('#starterr').textContent = e.message; }
}

const TABS = {gates:'Ship gates', plan:'Plan', docs:'Documents', workflow:'WORKFLOW.md'};
function renderTabs() { $('#tabs').innerHTML = Object.entries(TABS).map(([k,v]) => `<button class="${tab===k?'on':''}" ${on('setTab', k)}>${v}</button>`).join(''); }
let tabCache = {};
async function renderTab(force) {
  const body = $('#tabbody');
  if (tab === 'gates') {
    const c = S.change; if (!c || !c.tier) { setHtml(body, '<span class="muted">Tier not assessed yet.</span>'); return; }
    const res = c.ship;
    setHtml(body, `<div>Tier <b>${c.tier}</b>: ${S.gates.length} gates. ${res?`Verdict <b class="${res.verdict==='READY'?'gate-satisfied':'gate-unsatisfied'}">${res.verdict}</b> at ${short(res.integration_head)} on <code>${esc(res.integration_branch)}</code>`:'<span class="muted">not evaluated yet (runs at the ship node)</span>'}</div>
      <table><tr><th>gate</th><th>status</th><th>detail</th></tr>${S.gates.map(g => { const r = res && res.gates[g];
        return `<tr><td><code>${g}</code></td><td class="gate-${r?r.status:''}">${r?r.status:'—'}${r&&r.human_judgement?' (human)':''}</td><td>${r?esc(r.detail):''}</td></tr>`; }).join('')}</table>
      ${res?`<div class="muted">${esc(res.note)}</div>`:''}` + governanceHtml(c));
  } else if (tab === 'plan') {
    try {
      const tg = await api('/api/doc?path=' + encodeURIComponent(docPath('plan/taskgraph.json')));
      const spec = await api('/api/doc?path=' + encodeURIComponent(docPath('plan/spec.md')));
      const tasks = JSON.parse(tg.content).tasks || [];
      body.innerHTML = `<div class="muted">${esc(tg.attestation)} · ${esc(spec.attestation)}</div>
        <table><tr><th>task</th><th>soul</th><th>deps</th><th>files</th><th>reqs</th><th>verification</th></tr>${tasks.map(t=>`<tr><td><a ${on('select', 'build:' + t.id)}>${esc(t.id)}</a> ${esc(t.title)}</td><td>${esc(t.soul)}</td><td>${esc((t.depends_on||[]).join(', '))}</td><td class="mono">${(t.files_modified||[]).map(esc).join('<br>')}</td><td>${esc((t.requirements||[]).join(', '))}</td><td class="mono">${(t.verification||[]).map(esc).join('<br>')}</td></tr>`).join('')}</table>
        <pre>${esc(spec.content)}</pre>`;
    } catch (e) { body.innerHTML = '<span class="muted">No plan yet.</span>'; }
  } else if (tab === 'docs') {
    const scope = $('#docscope') ? $('#docscope').value : 'change';
    const showAll = $('#docall') && $('#docall').checked;
    const list = await api(`/api/docs?prefix=${encodeURIComponent(scope==='change'?docPath(''):'')}${showAll?'&all=1':''}`);
    const controls = `<div class="row" style="margin-bottom:6px"><select id="docscope" style="width:auto" data-onchange="renderTab"><option value="change" ${scope==='change'?'selected':''}>this change</option><option value="all" ${scope==='all'?'selected':''}>all changes</option></select>
      <label class="muted"><input id="docall" type="checkbox" style="width:auto" ${showAll?'checked':''} data-onchange="renderTab"> include controller records (control/: state, runs, logs)</label></div>`;
    let viewer = '';
    if (docSel) { try { const d = await api(`/api/doc?path=${encodeURIComponent(docSel.path)}${docSel.version?'&version='+docSel.version:''}`);
      viewer = `<div class="card"><b>${esc(d.attestation)}</b> <span class="muted">by ${esc(d.author)} (${esc(d.role)}) · ${esc(d.created_at)} · ${esc(d.sha256.slice(0,19))} · schema <code>${esc(d.schema)}</code> ${esc((d.schema_sha||'').slice(7,15))}</span>
        <div>versions: ${d.history.map(h=>`<a ${on('openDoc', d.path, h.version)}>v${h.version}</a>`).join(' ')}</div><pre>${esc(d.content)}</pre></div>`; } catch(e) { viewer = `<div class="err">${esc(e.message)}</div>`; } }
    body.innerHTML = controls + viewer + `<table><tr><th>path</th><th>latest</th><th>schema</th><th>author</th><th>written</th></tr>${list.map(d=>`<tr><td><a ${on('openDoc', d.path)}>${esc(d.path)}</a></td><td>v${d.version}</td><td><code>${esc(d.schema)}</code></td><td>${esc(d.author)} <span class="muted">${esc(d.role)}</span></td><td class="muted">${esc(d.created_at)}</td></tr>`).join('')}</table>`;
  } else if (tab === 'workflow') {
    if (!tabCache.wf || force) tabCache.wf = await api('/api/workflow');
    body.innerHTML = `<div class="muted">asset set ${esc(S.assets_sha)}</div><pre style="max-height:600px">${esc(tabCache.wf)}</pre>`;
  }
}
function governanceHtml(c) {
  const atts = Object.values(c.attestations||{});
  const tasks = S.nodes.filter(n=>n.kind==='build').map(n=>n.task.id);
  return `<h2 style="margin-top:14px">Governance</h2>
    <table><tr><th>attestation</th><th>verdict</th><th>by</th><th>sources</th></tr>${atts.map(a=>`<tr><td>${esc(a.kind)}</td><td>${esc(a.verdict)}</td><td>${esc(a.attested_by)}</td><td class="mono">${a.sources.map(x=>`<a ${on('openAtt', x)}>${esc(x)}</a>`).join('<br>')}${a.warnings.length?`<div class="err">${a.warnings.map(esc).join('<br>')}</div>`:''}</td></tr>`).join('') || '<tr><td colspan=4 class="muted">none</td></tr>'}</table>
    <div class="card"><b>Attest</b><div class="grid2">
      <select id="g_kind">${(S.attestation_kinds||[]).map(k=>`<option>${k}</option>`).join('')}</select>
      <select id="g_verdict">${['pass','fail','unknown','not_applicable'].map(v=>`<option>${v}</option>`).join('')}</select></div>
      <input id="g_sources" placeholder="sources: file:… or git:… (comma separated; relative paths are in this change)">
      <input id="g_covers" placeholder="covers task ids (optional, comma separated: ${esc(tasks.join(', '))})">
      <input id="g_waiver" placeholder="waiver reason (not_applicable only)">
      <button class="act" ${on('govern', 'attest', 'attest')}>Record attestation</button></div>
    <table><tr><th>override</th><th>gate</th><th>by</th><th>reason / evidence</th></tr>${(c.overrides||[]).map(o=>`<tr><td>${esc(o.kind)}</td><td><code>${esc(o.gate)}</code></td><td>${esc(o.approver)}</td><td>${esc(o.reason)} ${o.evidence?`<a ${on('openAtt', o.evidence)}>${esc(o.evidence)}</a>`:''}</td></tr>`).join('') || '<tr><td colspan=4 class="muted">none</td></tr>'}</table>
    <div class="card"><b>Override a gate</b><div class="grid2"><select id="o_kind"><option>add_gate</option><option>waive_gate</option></select><input id="o_gate" placeholder="gate id"></div>
      <input id="o_reason" placeholder="reason (≥24 chars)"><input id="o_evidence" placeholder="replacement evidence attestation (waive only)">
      <button class="act warn" ${on('govern', 'override', 'override_gate')}>Record override</button></div>
    <table><tr><th>protected-path approval</th><th>paths</th><th>by</th><th>reason</th></tr>${(c.protected_approvals||[]).map(p=>`<tr><td>${esc(p.task)}</td><td class="mono">${p.paths.map(esc).join('<br>')}</td><td>${esc(p.approver)} <span class="muted">${esc(p.at)}</span></td><td>${esc(p.reason)}</td></tr>`).join('') || '<tr><td colspan=4 class="muted">none</td></tr>'}</table>
    <div class="card"><b>Permit a task to modify protected acceptance tests</b> <span class="muted">(only runs started after this approval)</span>
      <div class="grid2"><select id="p_task">${tasks.map(t=>`<option>${esc(t)}</option>`).join('')}</select><input id="p_paths" placeholder="paths (comma separated)"></div>
      <input id="p_reason" placeholder="reason (≥24 chars)">
      <button class="act warn" ${on('govern', 'approve_protected_paths', 'protected')}>Approve protected paths</button></div>
    <table><tr><th>surface</th><th>commands</th><th>pins</th><th>pinned by</th></tr>${(S.surfaces||[]).map(sf=>{const pin=(c.surface_pins||{})[sf.id];
      return `<tr><td><b>${esc(sf.id)}</b><div class="muted">${esc(sf.description||'')}</div></td><td class="mono">${(sf.commands||[]).map(esc).join('<br>')}</td><td class="mono">${pin?Object.entries(pin.pins).map(([p,b])=>`${esc(p)} ${b?b.slice(0,8):'(absent)'}`).join('<br>'):'not pinned'}</td>
      <td>${pin?esc(pin.approver)+' · '+esc(pin.reason):''}<br><button class="act warn" ${on('govern', 'approve_surface', 'surface', sf.id)}>Re-affirm</button></td></tr>`}).join('') || '<tr><td colspan=4 class="muted">no surfaces declared</td></tr>'}</table>`;
}
const csv = sel => ($(sel).value||'').split(',').map(x=>x.trim()).filter(Boolean);
async function govern(action, payload) {
  const approver = $('#approver').value.trim();
  if (!approver) { alert('Enter your approver id (top right).'); return; }
  try { const r = await api(`/api/changes/${CID}/action`, {action, approver, payload});
    if (r.warnings && r.warnings.length) alert(r.warnings.join('\n')); await refresh(); }
  catch (e) { alert(e.message); }
}
function openDoc(path, version) { docSel = {path, version}; tab = 'docs'; renderTabs(); renderTab(); }
function openAtt(a) { const m = /^file:(.+)@(\d+)$/.exec(a); if (m) openDoc(m[1], +m[2]); else alert(a); }
// ---- agent log drawer. It lives outside every re-rendered region; while open it polls (the
// log grows without state changes) and patches entries in place, keeping the scroll position
// (or following the end when scrolled to the bottom) and any expanded entries.
let LOG = null;   // {id, raw, data, timer}
const LOG_KIND = {sandbox:'sandbox', init:'agent start', text:'agent', prompt:'input', thinking:'thinking',
                  tool_call:'tool', tool_result:'result', result:'final result', event:'event'};
function entryHtml(e) {
  const body = e.text ? `<div class="lt">${esc(e.text)}</div>` : '';
  const head = `<span class="lk">${esc(LOG_KIND[e.kind]||e.kind)}</span>${e.title?`<b>${esc(e.title)}</b>`:''}`;
  if (e.kind === 'tool_result' && e.text.length > 300)
    return `<details><summary>${head} <span class="muted">${esc(e.text.slice(0,120))}…</span></summary>${body}</details>`;
  return head + body;
}
async function showLog(id) {
  if (LOG && LOG.timer) clearInterval(LOG.timer);
  LOG = {id, raw: false, data: null, timer: setInterval(pollLog, 2000)};
  $('#logbody').innerHTML = '<span class="muted">loading…</span>';
  $('#logview').classList.add('open');
  await pollLog(true);
}
function closeLog() { if (LOG && LOG.timer) clearInterval(LOG.timer); LOG = null; $('#logview').classList.remove('open'); }
function toggleRaw() { if (LOG) { LOG.raw = !LOG.raw; LOG.data = null; pollLog(true); } }
async function pollLog(force) {
  if (!LOG) return;
  const id = LOG.id;
  if (!force && LOG.data && !LOG.data.live) return;          // finished runs do not change
  let data, raw;
  try {
    data = await api(`/api/runs/${id}/log?format=entries&lines=5000`);
    if (LOG && LOG.raw) raw = await api(`/api/runs/${id}/log?lines=5000`);
  } catch (e) { if (LOG && LOG.id === id) $('#logbody').innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
  if (!LOG || LOG.id !== id) return;
  $('#loghead').innerHTML = `<b>agent log ${esc(id)}</b> ${badge(data.status)} ${data.live?stageBadge(data):''} ${data.failure?sourceBadge(data.failure.source):''}
    <span style="flex:1"></span><label class="muted"><input type="checkbox" style="width:auto" ${LOG.raw?'checked':''} data-onchange="toggleRaw"> raw</label>
    <button class="act" style="margin:0" ${on('closeLog')}>close</button>
    ${data.failure?`<div style="flex-basis:100%">${failureHtml(data.failure)}</div>`:''}`;
  const box = $('#logbody');
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  if (LOG.raw) {
    const pre = box.querySelector('pre') || (box.innerHTML = '<pre></pre>', box.querySelector('pre'));
    if (pre.textContent !== raw) pre.textContent = raw || '(empty)';
  } else {
    const old = LOG.data ? LOG.data.entries : null;
    const ents = data.entries;
    if (!old || !old.length || JSON.stringify(old[0]) !== JSON.stringify(ents[0]) || ents.length < old.length || !box.querySelector('.le')) {
      box.innerHTML = ents.length ? ents.map(e => `<div class="le ${esc(e.kind)}${e.error?' error':''}">${entryHtml(e)}</div>`).join('') : '<span class="muted">(empty)</span>';
    } else {     // patch: changed entries in place, new ones appended
      const nodes = box.querySelectorAll('.le');
      ents.forEach((e, i) => {
        if (i < old.length && JSON.stringify(old[i]) === JSON.stringify(e)) return;
        const html = `<div class="le ${esc(e.kind)}${e.error?' error':''}">${entryHtml(e)}</div>`;
        if (i < nodes.length) nodes[i].outerHTML = html; else box.insertAdjacentHTML('beforeend', html);
      });
    }
  }
  LOG.data = data;
  if (atEnd || force) box.scrollTop = box.scrollHeight;
}

const HANDLERS = {
  select, openDoc, openAtt, showLog, startChange, act: actClick, govern: governClick,
  setTab: k => { tab = k; renderTabs(); renderTab(true); },
  newChange: () => { showStart = true; renderHeader(); },
  closeLog,
};
const CHANGE_HANDLERS = { renderTab: () => renderTab(), pickChange: el => pickChange(el.value), toggleRaw };
document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && LOG) closeLog(); });
document.addEventListener('click', ev => {
  const el = ev.target.closest('[data-on]');
  if (!el || !Object.hasOwn(HANDLERS, el.dataset.on)) return;
  HANDLERS[el.dataset.on](...JSON.parse(el.dataset.args || '[]'));
});
document.addEventListener('change', ev => {
  const el = ev.target.closest('[data-onchange]');
  if (el && Object.hasOwn(CHANGE_HANDLERS, el.dataset.onchange)) CHANGE_HANDLERS[el.dataset.onchange](el);
});

renderTabs();
refresh();
let lastVersion = -1, pending = false;
const es = new EventSource('/api/stream');
es.onmessage = async ev => { const v = JSON.parse(ev.data).version; if (v !== lastVersion && !pending) { lastVersion = v; pending = true;
  try { S = await loadState(); renderHeader(); renderPipeline(); renderGraph(); renderLive();
    if (!typingIn('#detail')) renderDetail();
    if (tab === 'gates' && !typingIn('#tabbody')) renderTab();
    if (LOG) pollLog(); } finally { pending = false; } } };
