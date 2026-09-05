// Shared client: event stream, cue/state/debrief rendering, mic capture (16 kHz PCM16) and PCM playback.
const $ = (id) => document.getElementById(id);
const STAGES = ["intro", "gatekeeper", "discovery", "objection", "close"];
const OUTCOME = {meeting_booked: ["Meeting booked", "good"], meeting_soft_yes: ["Soft yes", "good"], callback_agreed: ["Callback agreed", "good"], send_info: ["Send info", "warn"],
  gatekeeper_block: ["Gatekeeper block", "bad"], objection_unresolved: ["Objection unresolved", "warn"], not_interested: ["Not interested", "bad"], do_not_call: ["Do not call", "bad"], no_outcome: ["No outcome", "dim"], aborted: ["Aborted — no mic audio", "dim"]};

function wsUrl(path) { return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path; }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])); }
function fmtT(s) { s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }

function renderStages(el, stage) {
  if (!el) return;
  const i = STAGES.indexOf(stage);
  el.innerHTML = STAGES.map((s, k) => `<span class="${s} ${k === i ? "on" : k < i ? "done" : ""}">${s}</span>`).join("");
}
function renderCue(el, cue) {
  if (!el) return;
  if (!cue) { el.className = "cue silent"; el.innerHTML = `<div class="lab">listening</div><div class="line">· · ·</div>`; return; }
  el.className = "cue " + cue.kind;
  const lat = cue.latency_ms != null ? `<span class="lat">${cue.source} · ${cue.latency_ms} ms</span>` : "";
  el.innerHTML = `<div class="lab"><span>${esc(cue.label)}</span>${lat}</div><div class="line">${esc(cue.text)}</div>`;
}
function renderState(st, els) {
  if (!st) return;
  renderStages(els.stages, st.stage);
  if (els.role) els.role.textContent = st.role === "dm" ? "decision maker" : st.role === "gatekeeper" ? "gatekeeper" : "—";
  if (els.mom) { const m = st.momentum; els.mom.innerHTML = [-3, -2, -1, 0, 1, 2, 3].map(k => { const on = (m < -0.15 && k < 0 && k >= Math.round(m * 3)) || (m > 0.15 && k > 0 && k <= Math.round(m * 3)) || (Math.abs(m) <= 0.15 && k === 0); return `<i class="${on ? "on" : ""} ${k < 0 ? "neg" : k > 0 ? "pos" : "mid"}"></i>`; }).join(""); }
  if (els.talk) { const r = st.talk_ratio || 0; els.talk.innerHTML = `<span class="zone"></span><span class="mark" style="left:${Math.round(r * 100)}%"></span>`; if (els.talkv) els.talkv.textContent = Math.round(r * 100) + "%"; }
  if (els.facts) { const f = st.facts || {}; els.facts.textContent = [f.dm_first && `DM ${f.dm_first}`, f.gk_first && `GK ${f.gk_first}`, f.renewal_month && `renews ${f.renewal_month}`, f.email].filter(Boolean).join(" · ") || "—"; }
}
function addTranscript(el, t, partial) {
  if (!el) return;
  if (partial) { let p = el.querySelector("p.partial"); if (!p) { p = document.createElement("p"); p.className = "partial " + t.speaker; el.appendChild(p); } p.innerHTML = `<b>${t.speaker}</b><span>${esc(t.text)}…</span>`; }
  else { el.querySelectorAll("p.partial").forEach(x => x.remove()); const p = document.createElement("p"); p.className = t.speaker; p.innerHTML = `<b>${t.speaker}</b><span>${esc(t.text)}</span>`; el.appendChild(p); }
  el.scrollTop = el.scrollHeight;
}
function renderDebrief(el, d, opts = {}) {
  if (!el || !d) return;
  const [olabel, ocls] = OUTCOME[d.outcome] || [d.outcome, "dim"];
  const li = (xs) => (xs && xs.length ? xs.map(x => `<li>${esc(x)}</li>`).join("") : `<li class="muted">—</li>`);
  const crm = d.crm || {};
  const unv = (d.unverified || []).map(o => Object.keys(o)[0]);
  const crmRows = [["contact_name", "Contact"], ["contact_role", "Role"], ["gatekeeper_name", "Gatekeeper"], ["email", "Email"], ["phone", "Phone"], ["renewal_month", "Renewal"], ["current_broker_or_carrier", "Current broker"], ["next_step", "Next step"], ["next_step_when", "When"]]
    .map(([k, l]) => `<div><div class="k">${l}${unv.includes(k) ? ' <span class="badge">unverified → dropped</span>' : ""}</div><div class="v ${crm[k] ? "" : "none"}">${crm[k] ? esc(crm[k]) : "not stated on the call"}</div></div>`).join("");
  el.className = "panel debrief";
  el.innerHTML = `
    <div class="row" style="justify-content:space-between"><div class="kicker">Debrief · ${d.source === "llm" ? `LLM (${d.provider || "claude"}), transcript-grounded` : "rule-based"}${opts.pending ? " · <i>LLM version on its way…</i>" : ""}</div><span class="pill ${ocls}">${olabel}</span></div>
    <div class="head">${esc(d.headline || "")}</div>
    <div class="grid" style="grid-template-columns:1fr 1fr 1fr">
      <div><div class="kicker">What happened</div><ul>${li(d.what_happened)}</ul></div>
      <div><div class="kicker" style="color:var(--good)">What worked</div><ul>${li(d.what_worked)}</ul></div>
      <div><div class="kicker" style="color:var(--bad)">What didn't</div><ul>${li(d.what_didnt)}</ul></div>
    </div>
    <div class="improve">${esc(d.one_improvement || "")}</div>
    <div class="row" style="gap:28px"><span class="muted">talk ratio <b class="ink2">${Math.round((d.talk_ratio || 0) * 100)}%</b></span><span class="muted">filler words <b class="ink2">${d.fillers ?? 0}</b></span><span class="muted">stage reached <b class="ink2">${esc(d.stage_reached || "")}</b></span>${d.next_time ? `<span class="muted">next time: <b class="ink2">${esc(d.next_time)}</b></span>` : ""}</div>
    <div class="kicker" style="margin-top:16px">CRM — only what was said on the call</div>
    <div class="crm">${crmRows}</div>
    ${(d.new_objection_candidates || []).length ? `<div class="kicker" style="margin-top:14px">New objections (not in playbook)</div><ul>${li(d.new_objection_candidates)}</ul>` : ""}`;
}

// ---- audio -------------------------------------------------------------------------------
const WORKLET = `class P extends AudioWorkletProcessor{constructor(){super();this.buf=[];this.ratio=sampleRate/16000;this.pos=0;this.peak=0;}
process(inputs){const ch=inputs[0][0];if(!ch)return true;for(let i=0;i<ch.length;i++){const a=Math.abs(ch[i]);if(a>this.peak)this.peak=a;}
let p=this.pos;const out=[];while(p<ch.length-1){const i=Math.floor(p),f=p-i;out.push(ch[i]*(1-f)+ch[i+1]*f);p+=this.ratio;}this.pos=p-ch.length;for(const v of out)this.buf.push(v);
if(this.buf.length>=640){const o=new Int16Array(640);for(let i=0;i<640;i++){const s=Math.max(-1,Math.min(1,this.buf[i]));o[i]=s<0?s*0x8000:s*0x7fff;}this.buf=this.buf.slice(640);this.port.postMessage({pcm:o.buffer,peak:this.peak},[o.buffer]);this.peak=0;}return true;}}
registerProcessor('p',P);`;

class Player {
  constructor(rate) { this.ctx = new AudioContext({sampleRate: rate}); this.next = 0; this.sources = []; this.rxBytes = 0; this.resume(); }
  resume() { if (this.ctx.state !== "running") this.ctx.resume().catch(() => {}); }
  play(buf) {
    this.rxBytes += buf.byteLength; this.resume();
    const i16 = new Int16Array(buf), f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    const ab = this.ctx.createBuffer(1, f32.length, this.ctx.sampleRate); ab.getChannelData(0).set(f32);
    const src = this.ctx.createBufferSource(); src.buffer = ab; src.connect(this.ctx.destination);
    const t = Math.max(this.ctx.currentTime + 0.03, this.next); src.start(t); this.next = t + ab.duration; this.sources.push(src);
    src.onended = () => { this.sources = this.sources.filter(s => s !== src); };
  }
  clear() { this.sources.forEach(s => { try { s.stop(); } catch (e) {} }); this.sources = []; this.next = this.ctx.currentTime; }
}
const VIRTUAL_MIC = /blackhole|loopback|soundflower|virtual|aggregate|zoom|teams|obs|cable/i;
async function listMics() {
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput");
  return devs;
}
function pickMic(devs) {
  const saved = localStorage.getItem("micId");
  if (saved && devs.some(d => d.deviceId === saved)) return saved;
  const real = devs.filter(d => !VIRTUAL_MIC.test(d.label));
  const pref = real.find(d => /macbook|built-in|internal/i.test(d.label)) || real[0] || devs[0];
  return pref ? pref.deviceId : undefined;
}
async function startMic(onPcm, onLevel, deviceId) {
  const base = {echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1};
  let stream = null;
  if (deviceId && typeof deviceId === "string" && deviceId.length > 3) {
    try { stream = await navigator.mediaDevices.getUserMedia({audio: {...base, deviceId: {exact: deviceId}}}); } catch (e) { stream = null; }
  }
  if (!stream) stream = await navigator.mediaDevices.getUserMedia({audio: base});   // permission prompt happens here
  // after permission the device list has labels/ids: move off a virtual device if the browser picked one
  try {
    const devs = await listMics();
    const cur = stream.getAudioTracks()[0].getSettings().deviceId;
    const want = deviceId && devs.some(d => d.deviceId === deviceId) ? deviceId : pickMic(devs);
    if (want && want !== cur && want.length > 3) {
      const s2 = await navigator.mediaDevices.getUserMedia({audio: {...base, deviceId: {exact: want}}});
      stream.getTracks().forEach(t => t.stop()); stream = s2;
    }
  } catch (e) { /* keep whatever we have */ }
  const track = stream.getAudioTracks()[0];
  const ctx = new AudioContext();
  await ctx.resume();
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], {type: "application/javascript"})));
  const node = new AudioWorkletNode(ctx, "p");
  const st = {tx: 0, peak: 0, frames: 0};
  node.port.onmessage = (e) => { st.tx += e.data.pcm.byteLength; st.frames++; if (e.data.peak > st.peak) st.peak = e.data.peak; onPcm(e.data.pcm); if (onLevel) onLevel(e.data.peak); };
  ctx.createMediaStreamSource(stream).connect(node);
  const sink = ctx.createGain(); sink.gain.value = 0; node.connect(sink); sink.connect(ctx.destination);
  return {stop: () => { stream.getTracks().forEach(t => t.stop()); ctx.close(); }, device: track.label, track, ctx,
          stats: () => { const o = {device: track.label, muted: track.muted, state: track.readyState, ctx: ctx.state, rate: ctx.sampleRate, tx: st.tx, frames: st.frames, peak: +st.peak.toFixed(3), ua: navigator.userAgent.slice(0, 90)}; st.peak = 0; return o; }};
}

// Chrome only lets audio run after a user gesture: create contexts inside the click, and resume them on any later click.
document.addEventListener("click", () => { for (const p of Object.values(window.__players || {})) p.resume(); }, true);
