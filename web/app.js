"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const FIELDS = [
  "client_id", "client_secret", "save_secret", "category", "max_total",
  "follower_enabled", "min_followers", "max_followers",
  "viewer_enabled", "min_viewers", "max_viewers",
  "uptime_enabled", "max_uptime_hours", "skip_urls_text", "tags", "debug_log",
];
const CATS = ["전체", "토크", "게임", "스포츠", "기타"];
const BATCH = 80;

const state = { hits: [], shown: [], rendered: 0, sort: "viewers", view: "grid", searching: false, searched: false, tagTerms: [] };
let api = null;

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const safeUrl = (u) => (/^https:\/\/[^\s"'<>]+$/.test(u || "") ? esc(u) : "");
const fmt = (n) => Number(n || 0).toLocaleString("ko-KR");
function fmtUptime(h) {
  if (h == null) return "-";
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}분`;
  const m = Math.round((h % 1) * 60);
  return m ? `${Math.floor(h)}시간 ${m}분` : `${Math.floor(h)}시간`;
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const splitTerms = (t) => [...new Set(String(t || "").toLowerCase().split(/[,#\s]+/).filter(Boolean))];
const countSkip = (text) => new Set((text.match(/[0-9a-f]{32}/gi) || []).map((s) => s.toLowerCase())).size;

function setStatus(text, err = false, action = null) {
  const el = $("#status");
  el.textContent = text;
  el.title = text;
  el.classList.toggle("err", err);
  const box = $("#statusAction");
  box.textContent = "";
  if (action) {
    const a = document.createElement("a");
    a.textContent = action.label;
    a.onclick = () => { box.textContent = ""; action.run(); };
    box.appendChild(a);
  }
}

window.ChzzkLook = {
  onStatus: (m) => setStatus(m),
  onProgress: (p) => setStatus(`검색 중... ${fmt(p.scanned)}개 확인, 카테고리 맞는 방송 ${fmt(p.matched)}개`),
};

function collect() {
  const d = {};
  for (const k of FIELDS) {
    const el = $("#" + k);
    d[k] = el.type === "checkbox" ? el.checked : el.value.trim();
  }
  d.tag_mode = ($("input[name=tag_mode]:checked") || {}).value || "all";
  d.sort = state.sort;
  d.view = state.view;
  return d;
}

function apply(s) {
  for (const k of FIELDS) {
    const el = $("#" + k);
    if (s[k] === undefined || s[k] === null) continue;
    if (el.type === "checkbox") el.checked = !!s[k];
    else el.value = s[k];
  }
  $(`input[name=tag_mode][value=${s.tag_mode === "any" ? "any" : "all"}]`).checked = true;
  state.sort = s.sort || "viewers";
  $("#sort").value = state.sort;
  setView(s.view || "grid", false);
  syncUI();
}

const save = debounce(() => { if (api) api.save_settings(collect()); }, 400);

function syncUI() {
  for (const box of $$(".cond[data-filter]")) {
    const on = $(`#${box.dataset.filter}_enabled`).checked;
    box.classList.toggle("off", !on);
    $$("input.num", box).forEach((i) => (i.disabled = !on));
  }
  const cat = $("#category").value.trim() || "전체";
  $$("#catBtns button").forEach((b) => b.classList.toggle("on", b.dataset.cat === cat));
  $("#skipCount").textContent = countSkip($("#skip_urls_text").value);
}

const nullLast = (v) => (v == null ? Infinity : v);
const SORTERS = {
  viewers: (a, b) => a.viewers - b.viewers || nullLast(a.followers) - nullLast(b.followers),
  followers: (a, b) => nullLast(a.followers) - nullLast(b.followers) || a.viewers - b.viewers,
  recent: (a, b) => nullLast(a.uptime) - nullLast(b.uptime),
  name: (a, b) => a.channelName.localeCompare(b.channelName, "ko"),
};

function thumbHTML(h) {
  const src = safeUrl(h.thumbnail) || safeUrl(h.channelImage);
  return `<div class="thumb" data-act="open">
    <div class="noimg">이미지 없음</div>
    ${src ? `<img src="${src}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
    ${state.view === "grid" ? `<span class="v">시청자 ${fmt(h.viewers)}</span>` : ""}
  </div>`;
}

const btnsHTML = `<div class="btns"><button data-act="open">열기</button><button data-act="copy">링크복사</button><button data-act="pass">패스</button></div>`;
const titleHTML = (h) => `${h.adult ? '<span class="adult">[19] </span>' : ""}${esc(h.title) || "(제목 없음)"}`;
function tagsText(h) {
  const terms = state.tagTerms;
  const isHit = (t) => terms.some((q) => t.toLowerCase().includes(q));
  const tags = [...(h.tags || [])].sort((a, b) => isHit(b) - isHit(a));
  const shown = tags.slice(0, Math.max(5, tags.filter(isHit).length));
  const more = tags.length - shown.length;
  return shown.map((t) => (isHit(t) ? `<b class="hl">#${esc(t)}</b>` : "#" + esc(t))).join(" ") + (more > 0 ? ` +${more}` : "");
}

function cardHTML(h) {
  const meta = [h.followers != null ? `팔로워 ${fmt(h.followers)}` : null, fmtUptime(h.uptime), esc(h.category)]
    .filter(Boolean).join(" · ");
  const tags = tagsText(h);
  return `<div class="card" data-id="${h.liveId}">
    ${thumbHTML(h)}
    <div class="body">
      <div class="t" data-act="open">${titleHTML(h)}</div>
      <div class="ch">${esc(h.channelName)}</div>
      <div class="m">${meta}</div>
      ${tags ? `<div class="m">${tags}</div>` : ""}
      ${btnsHTML}
    </div>
  </div>`;
}

function rowHTML(h) {
  const tags = tagsText(h);
  return `<tr data-id="${h.liveId}">
    <td>${thumbHTML(h)}</td>
    <td>${esc(h.channelName)}</td>
    <td><div class="t" data-act="open">${titleHTML(h)}</div><div class="m">${esc(h.category)}${tags ? " · " + tags : ""}</div></td>
    <td class="n">${fmt(h.viewers)}</td>
    <td class="n">${h.followers != null ? fmt(h.followers) : "-"}</td>
    <td class="n">${fmtUptime(h.uptime)}</td>
    <td>${btnsHTML}</td>
  </tr>`;
}

function render() {
  const q = $("#q").value.trim().toLowerCase();
  const qTerms = splitTerms(q);
  let rows = state.hits;
  if (qTerms.length) {
    rows = rows.filter((h) => {
      const hay = [h.channelName, h.title, h.category, ...(h.tags || [])].join("\n").toLowerCase();
      return qTerms.every((t) => hay.includes(t));
    });
  }
  state.shown = [...rows].sort(SORTERS[state.sort]);
  state.rendered = 0;
  $("#content").scrollTop = 0;

  const res = $("#results");
  if (state.view === "list" && rows.length) {
    res.className = "";
    res.innerHTML = `<table class="list"><thead><tr><th>썸네일</th><th>채널</th><th>제목</th><th class="n">시청자</th><th class="n">팔로워</th><th class="n">방송시간</th><th></th></tr></thead><tbody></tbody></table>`;
  } else {
    res.className = "grid";
    res.innerHTML = "";
  }

  $("#count").textContent = state.searched ? (q ? `${fmt(rows.length)} / ${fmt(state.hits.length)}개` : `${fmt(rows.length)}개`) : "";
  let msg = "";
  if (!state.searched) msg = "왼쪽에서 조건을 정하고 [검색]을 누르세요.";
  else if (!state.hits.length) msg = "조건에 맞는 방송이 없습니다. 조건을 넓히거나 최대 스캔 수를 늘려 보세요.";
  else if (!rows.length) msg = `'${q}' 에 해당하는 결과가 없습니다.`;
  $("#empty").textContent = msg;
  renderMore();
}

function renderMore() {
  const next = state.shown.slice(state.rendered, state.rendered + BATCH);
  if (!next.length) return;
  const html = next.map(state.view === "list" ? rowHTML : cardHTML).join("");
  ($("#results tbody") || $("#results")).insertAdjacentHTML("beforeend", html);
  state.rendered += next.length;
}

function setView(v, rerender = true) {
  state.view = v;
  $$("#viewBtns button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  if (rerender) { render(); save(); }
}

function setBusy(on) {
  state.searching = on;
  $("#searchBtn").classList.toggle("hidden", on);
  $("#stopBtn").classList.toggle("hidden", !on);
  $("#stopBtn").disabled = false;
}

async function search() {
  if (state.searching || !api) return;
  setBusy(true);
  setStatus("검색 준비 중...");
  $("#results").innerHTML = "";
  $("#empty").textContent = "검색 중...";
  save();
  let res;
  try { res = await api.search(collect()); } catch (e) { res = { ok: false, error: String(e) }; }
  setBusy(false);

  if (!res.ok) {
    setStatus("오류: " + res.error, true);
    render();
    return;
  }
  Object.assign(state, { hits: res.hits, searched: true, tagTerms: splitTerms($("#tags").value) });
  setStatus(`${res.cancelled ? "중지됨" : "완료"} | ${fmt(res.scanned)}개 확인 → ${fmt(res.hits.length)}개 | ${res.condition}`);
  render();
}

function stop() {
  if (!api) return;
  api.stop();
  $("#stopBtn").disabled = true;
  setStatus("중지하는 중... 지금까지 확인한 것만 보여줍니다");
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  setStatus("링크 복사함: " + text);
}

function passHit(h) {
  const box = $("#skip_urls_text");
  const before = box.value;
  if (!before.toLowerCase().includes(h.channelId)) box.value = (before.trimEnd() + "\n" + h.url).trim();
  const prevHits = state.hits;
  state.hits = state.hits.filter((x) => x.channelId !== h.channelId);
  syncUI(); save(); render();
  setStatus(`패스함: ${h.channelName}`, false, {
    label: "되돌리기",
    run: () => { box.value = before; state.hits = prevHits; syncUI(); save(); render(); setStatus("되돌림"); },
  });
}

async function refreshCats() {
  if (!api) return;
  const btn = $("#refreshCats");
  btn.disabled = true;
  setStatus("카테고리 목록 받는 중...");
  const res = await api.refresh_categories($("#client_id").value, $("#client_secret").value);
  btn.disabled = false;
  if (!res.ok) return setStatus("오류: " + res.error, true);
  $("#catList").innerHTML = res.categories.map((c) => `<option value="${esc(c)}">`).join("");
  setStatus(`카테고리 ${res.categories.length}개 받음 — 입력칸을 누르면 목록이 나옵니다`);
  $("#category").focus();
}

function bind() {
  $("#catBtns").innerHTML = CATS.map((c) => `<button type="button" data-cat="${c}">${c}</button>`).join("");
  $("#catBtns").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    $("#category").value = b.dataset.cat === "전체" ? "" : b.dataset.cat;
    syncUI(); save();
  });

  $$(".side input, .side textarea").forEach((el) => el.addEventListener(["checkbox", "radio"].includes(el.type) ? "change" : "input", () => { syncUI(); save(); }));
  $("#debug_log").addEventListener("change", (e) => {
    save();
    setStatus(e.target.checked ? "디버그 로그 켬 — debug.log에 기록합니다" : "디버그 로그 끔 — 더 이상 기록하지 않습니다");
  });
  $(".side").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("input")) search(); });
  $("#showSecret").addEventListener("change", (e) => { $("#client_secret").type = e.target.checked ? "text" : "password"; });
  $("#refreshCats").addEventListener("click", refreshCats);
  $("#searchBtn").addEventListener("click", search);
  $("#stopBtn").addEventListener("click", stop);

  $("#sort").addEventListener("change", (e) => { state.sort = e.target.value; render(); save(); });
  $("#viewBtns").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView(b.dataset.view); });
  $("#q").addEventListener("input", debounce(render, 150));

  $("#results").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]");
    const item = e.target.closest("[data-id]");
    if (!act || !item || !api) return;
    const h = state.shown.find((x) => String(x.liveId) === item.dataset.id);
    if (!h) return;
    if (act.dataset.act === "open") api.open_urls([h.url]);
    else if (act.dataset.act === "copy") copy(h.url);
    else if (act.dataset.act === "pass") passHit(h);
  });
  $("#content").addEventListener("scroll", (e) => {
    const el = e.currentTarget;
    if (el.scrollTop + el.clientHeight > el.scrollHeight - 600) renderMore();
  });

  $("#clearSkip").addEventListener("click", () => {
    const box = $("#skip_urls_text");
    const before = box.value;
    if (!before.trim()) return;
    box.value = ""; syncUI(); save();
    setStatus("패스 목록 비움", false, { label: "되돌리기", run: () => { box.value = before; syncUI(); save(); setStatus("되돌림"); } });
  });
  $("#skipFile").addEventListener("change", (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const r = new FileReader();
    r.onload = () => {
      const box = $("#skip_urls_text");
      const n0 = countSkip(box.value);
      box.value = (box.value.trimEnd() + "\n" + String(r.result).trim()).trim();
      syncUI(); save();
      setStatus(`패스 목록에 ${countSkip(box.value) - n0}개 추가함 (${f.name})`);
    };
    r.readAsText(f, "utf-8");
    e.target.value = "";
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); search(); }
    else if (e.key === "Escape" && state.searching) stop();
  });

  document.addEventListener("error", (e) => {
    if (e.target instanceof HTMLImageElement && e.target.closest("#results")) e.target.remove();
  }, true);
}

let started = false;
async function init() {
  if (started) return;
  started = true;
  const bridge = window.pywebview.api;
  let s = {};
  try { s = await bridge.get_settings(); }
  catch (e) { setStatus("설정 파일을 읽지 못했습니다: " + e, true); }
  apply(s || {});
  api = bridge;
  document.body.classList.remove("loading");
  $("#searchBtn").disabled = false;
  if (!$("#client_id").value || !$("#client_secret").value) setStatus("Client ID / Secret을 입력하세요");
  else setStatus(`준비됨 | 카테고리: ${$("#category").value || "전체"}`);
}

bind();
render();
if (window.pywebview && window.pywebview.api && window.pywebview.api.get_settings) init();
else window.addEventListener("pywebviewready", init);
