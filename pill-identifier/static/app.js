const $ = (id) => document.getElementById(id);
const runBtn = $("run");
const previews = $("previews");
const resultsEl = $("results");
const issuesEl = $("issues");
const summaryEl = $("summary");

const SHOW_CANDIDATES = 3;
let files = [];
let pills = [];
let options = { shapes: [], colors: [], forms: [] };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

fetch("/api/status").then((r) => r.json()).then((s) => {
  options = s;
  const el = $("db-status");
  if (s.total === 0) {
    el.innerHTML = "⚠️ 낱알식별 DB가 비어 있습니다";
    el.className = "error small";
  } else if (s.demo_only) {
    el.innerHTML = `⚠️ 데모 데이터(${s.total}건)만 있습니다`;
    el.className = "error small";
  } else {
    el.textContent = `식약처 낱알식별 DB ${s.total.toLocaleString()}건`;
  }
});

// ---------- 1. 사진 올리기 ----------

// 긴 변 4096px JPEG로 맞춰 전송. 서버가 모델용(2576px)으로 줄이고, 확대는 이 원본에서 잘라
// 작은 알약의 각인도 선명하게 본다. 촬영 정보(EXIF)도 이때 빠진다.
function shrink(file, edge = 4096) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, edge / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * scale);
      c.height = Math.round(img.height * scale);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(img.src);
      c.toBlob((b) => (b ? resolve(b) : reject(new Error("이미지 변환 실패"))), "image/jpeg", 0.9);
    };
    img.onerror = () => reject(new Error("이미지를 읽을 수 없습니다"));
    img.src = URL.createObjectURL(file);
  });
}

function renderPreviews() {
  previews.innerHTML = files.map((f, i) => `
    <figure><img src="${URL.createObjectURL(f)}" alt="사진 ${i + 1}">
      <figcaption>${i + 1}</figcaption><button data-i="${i}" aria-label="삭제">✕</button></figure>`).join("");
  runBtn.disabled = files.length === 0;
}

for (const input of [$("camera"), $("album")]) {
  input.addEventListener("change", () => {
    files = [...files, ...input.files].slice(0, 6);
    input.value = "";
    renderPreviews();
  });
}

previews.addEventListener("click", (e) => {
  const i = e.target.dataset.i;
  if (i === undefined) return;
  files.splice(Number(i), 1);
  renderPreviews();
});

// 분석은 수십 초 걸리므로 무엇을 하는 중인지 보여 준다 (실제 단계와 정확히 맞지는 않음).
function startProgress(el, textEl) {
  const steps = ["사진 확인 중…", "알약을 하나씩 확대해서 각인 읽는 중…", "각인을 다시 확인하는 중…", "식약처 DB와 대조 중…"];
  let k = 0;
  el.classList.remove("hidden");
  textEl.textContent = steps[0];
  const timer = setInterval(() => { k = Math.min(k + 1, steps.length - 1); textEl.textContent = steps[k]; }, 12000);
  return () => { clearInterval(timer); el.classList.add("hidden"); };
}

async function getJson(url, init) {
  let r;
  try {
    r = await fetch(url, init);
  } catch {
    throw new Error("인터넷 연결이 끊겼습니다. 연결 상태를 확인하고 다시 시도해 주세요.");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `오류 ${r.status}`);
  return data;
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

// 사진을 올리면 작업 번호를 받고, 결과가 나올 때까지 2초마다 확인한다 (분석이 1분 넘게 걸릴 수 있음).
async function postImages(list, url) {
  const form = new FormData();
  for (const [i, f] of list.entries()) form.append("images", await shrink(f), `photo${i + 1}.jpg`);
  const { job } = await getJson(url, { method: "POST", body: form });
  let misses = 0;
  for (;;) {
    await sleep(2000);
    let status;
    try {
      status = await getJson(`/api/jobs/${job}`);
      misses = 0;
    } catch (e) {
      // 잠깐 끊긴 건 다시 시도, 계속 안 되면 포기
      if (++misses >= 5 || e.message.includes("작업을 찾을 수 없습니다")) throw e;
      continue;
    }
    if (status.status === "done") return status.result;
    if (status.status === "error") throw new Error(status.error);
    if (status.elapsed > 300) throw new Error("분석이 너무 오래 걸립니다. 사진 수를 줄여 다시 시도해 주세요.");
  }
}

runBtn.addEventListener("click", async () => {
  runBtn.disabled = true;
  $("error").classList.add("hidden");
  resultsEl.innerHTML = "";
  issuesEl.classList.add("hidden");
  summaryEl.classList.add("hidden");
  const stop = startProgress($("progress"), $("progress-text"));
  try {
    const data = await postImages(files, "/api/identify");
    pills = data.pills.map((p) => ({ ...p, chosen: null, decided: false }));
    render(data);
    $("reset").classList.remove("hidden");
    resultsEl.scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("error").textContent = "⚠️ " + e.message;
    $("error").classList.remove("hidden");
  } finally {
    stop();
    runBtn.disabled = files.length === 0;
  }
});

$("reset").addEventListener("click", () => {
  if (pills.some((p) => p.decided) && !confirm("확인한 내용이 지워집니다. 처음부터 할까요?")) return;
  files = []; pills = [];
  renderPreviews();
  resultsEl.innerHTML = "";
  issuesEl.classList.add("hidden");
  summaryEl.classList.add("hidden");
  $("reset").classList.add("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// ---------- 2. 알약별 후보 ----------

const imprintText = (p) => {
  const faces = [p.imprint_front, p.imprint_back].filter(Boolean);
  if (faces.length) return faces.map((f) => `<code>${esc(f)}</code>`).join(" / ");
  return p.has_mark ? "마크만 있음" : '<span class="muted">각인 안 보임</span>';
};

const describe = (p) => {
  const color = [p.color_primary, p.color_secondary].filter((c) => c && c !== "없음" && c !== "불명").join("·");
  return `${color} ${p.shape !== "불명" ? p.shape : ""} ${p.form !== "불명" ? p.form : ""}`.replace(/\s+/g, " ").trim();
};

const CHECK_STYLE = { true: ["ok", "✓"], near: ["near", "≈"], partial: ["near", "≈"], false: ["bad", "✗"] };
function chip(label, v) {
  if (v === null || v === undefined) return `<span class="chip none">${label} -</span>`;
  const [cls, mark] = CHECK_STYLE[String(v)];
  return `<span class="chip ${cls}">${label} ${mark}</span>`;
}

function render(data) {
  if (data.photo_issues.length) {
    issuesEl.innerHTML = "<b>📸 사진 참고</b><ul>" + data.photo_issues.map((s) => `<li>${esc(s)}</li>`).join("") + "</ul>";
    issuesEl.classList.remove("hidden");
  }
  if (!pills.length) {
    resultsEl.innerHTML = '<section class="card"><p>사진에서 알약을 찾지 못했습니다. 더 가까이, 밝게 찍어 주세요.</p></section>';
    return;
  }
  resultsEl.innerHTML = `<h2 class="section-title"><span class="step">2</span> 알약별 확인 <span id="done-count" class="muted small"></span></h2>` +
    pills.map((_, i) => `<section class="card pill" id="pill-${i}"></section>`).join("");
  pills.forEach((_, i) => renderPill(i));
  renderSummary();
}

// 사진 판독만으로 확정하기 어려운 경우 → 재촬영/각인 입력을 권한다.
function helpReason(p) {
  const c = p.candidates;
  if (p.source !== "manual" && ((!p.imprint_front && !p.imprint_back) || ["낮음", "없음"].includes(p.imprint_confidence)))
    return "각인이 잘 읽히지 않았습니다. 모양·색만으로는 약을 특정할 수 없습니다.";
  if (!c.length) return "일치하는 후보를 찾지 못했습니다.";
  const close = c.filter((x) => x.score >= c[0].score - 10);
  if (close.length >= 2) {
    if (close.every((x) => imprintKey(x) === imprintKey(c[0])))
      return `각인·모양이 똑같은 약이 ${close.length}개 있습니다 (같은 성분의 다른 회사 제품일 수 있음). 포장이나 처방 내역으로 구분하세요.`;
    return `비슷한 후보가 ${close.length}개 있습니다. 실물과 사진을 비교해 고르거나 각인을 확인하세요.`;
  }
  if (c[0].checks["각인"] === "partial") return "각인 일부가 불확실합니다.";
  return null;
}

const imprintKey = (c) => [c.print_front, c.print_back].map((v) => (v || "").toUpperCase().replace(/분할선|마크|[^0-9A-Z가-힣]/g, "")).sort().join("|");
const hasImprint = (p) => p.source === "manual" || !!`${p.imprint_front}${p.imprint_back}`.replace(/\?/g, "");

const opts = (list, sel) => list.map((v) => `<option${v === sel ? " selected" : ""}>${esc(v)}</option>`).join("");

function candidateHtml(i, c, p) {
  return `
    <label class="cand">
      <input type="radio" name="pill${i}" value="${esc(c.item_seq)}"${p.chosen?.item_seq === c.item_seq ? " checked" : ""}>
      <div class="cand-img">${c.item_image ? `<img class="zoomable" src="${esc(c.item_image)}" alt="" loading="lazy">` : '<div class="noimg">이미지 없음</div>'}</div>
      <div class="cand-body">
        <div class="cand-top"><b>${esc(c.item_name)}</b><span class="score">${c.score}%</span></div>
        <div class="muted small">${esc([c.entp_name, c.etc_otc_name].filter(Boolean).join(" · "))}</div>
        <div class="small">각인 <code>${esc(c.print_front) || "-"}</code> / <code>${esc(c.print_back) || "-"}</code></div>
        <div class="muted small">${esc(c.color)} ${esc(c.drug_shape)} · ${esc(c.form_code_name)}${c.size ? ` · ${esc(c.size)}mm` : ""}</div>
        <div class="chips">${chip("각인", c.checks["각인"])}${chip("모양", c.checks["모양"])}${chip("색상", c.checks["색상"])}${chip("제형", c.checks["제형"])}</div>
      </div>
    </label>`;
}

function renderPill(i) {
  const p = pills[i];
  const el = $(`pill-${i}`);
  const reason = helpReason(p);
  const done = p.decided;
  el.classList.toggle("done", done);

  const photos = (p.photos || []).map((src) => `<img class="zoomable" src="${src}" alt="촬영한 알약">`).join("");
  const header = `
    <div class="pill-head">
      <div class="my-photos">${photos || '<div class="noimg">사진</div>'}</div>
      <div>
        <h3>#${i + 1} ${esc(describe(p) || p.label)} <span class="muted">× ${p.count}</span>
          ${p.source === "manual" ? '<span class="src">각인 입력</span>' : p.source === "retake" ? '<span class="src">재촬영</span>' : ""}</h3>
        <div class="small">각인 ${imprintText(p)}${p.score_line && !["없음", "불명"].includes(p.score_line) ? ` · 분할선 ${esc(p.score_line)}` : ""}</div>
        ${p.package_text ? `<div class="small">포장 인쇄: <b>${esc(p.package_text)}</b></div>` : ""}
      </div>
    </div>`;

  if (done && !p.editing) {
    el.innerHTML = header + `
      <div class="decided">
        ${p.chosen ? `<span class="ok">✓</span> <b>${esc(p.chosen.item_name)}</b>` : '<span class="bad">식별 불가 · 약사 의뢰</span>'}
        <button class="link" data-act="edit">변경</button>
      </div>`;
    return;
  }

  const cands = p.candidates;
  // 각인 없이 모양·색만 비슷한 약은 수백 개라 의미가 적다 → 눌러야 보이게 한다.
  const shown = p.showAll ? cands : cands.slice(0, hasImprint(p) ? SHOW_CANDIDATES : 0);
  el.innerHTML = header + `
    ${reason ? `
      <div class="help">
        <p>🔍 ${esc(reason)}</p>
        <p class="muted small">다시 찍기: 이 알약만 10~15cm 거리에서 앞면·뒷면을 한 장씩 찍어 주세요. 작은 알약일수록 가까이!</p>
        <div class="row">
          <label class="btn primary small grow">📷 다시 찍기
            <input type="file" accept="image/*" capture="environment" multiple hidden data-act="retake"></label>
          <button class="btn small grow" data-act="manual">⌨️ 각인 입력</button>
        </div>
      </div>` : ""}
    <p class="busy hidden"></p>
    ${shown.map((c) => candidateHtml(i, c, p)).join("") || '<p class="muted">DB에서 일치하는 후보가 없습니다.</p>'}
    ${cands.length > shown.length ? `<button class="link more" data-act="more">${shown.length ? `후보 ${cands.length - shown.length}개 더 보기` : "모양·색만 비슷한 약 보기 (참고용)"}</button>` : ""}
    <label class="cand none"><input type="radio" name="pill${i}" value="__none"${done && !p.chosen ? " checked" : ""}>해당 없음 / 식별 불가 (약사 의뢰)</label>
    ${reason ? "" : `
      <p class="small muted alt">실물과 다르면:
        <label class="link">다시 찍기<input type="file" accept="image/*" capture="environment" multiple hidden data-act="retake"></label>
        · <button class="link" data-act="manual">각인 입력</button></p>`}
    ${p.notes ? `<details class="small muted"><summary>판독 메모</summary>${esc(p.notes)}</details>` : ""}
    <form class="manual hidden" data-act="search">
      <p class="small muted">실물에서 읽은 각인을 입력하세요. 분할선 양쪽 글자는 같은 칸에 이어 쓰면 됩니다.</p>
      <div class="grid">
        <label>앞면 각인<input name="imprint_front" value="${esc(p.imprint_front)}" autocapitalize="characters" autocomplete="off"></label>
        <label>뒷면 각인<input name="imprint_back" value="${esc(p.imprint_back)}" autocapitalize="characters" autocomplete="off"></label>
        <label>모양<select name="shape">${opts(options.shapes, p.shape)}</select></label>
        <label>색상<select name="color_primary">${opts(options.colors, p.color_primary)}</select></label>
        <label>제형<select name="form">${opts(options.forms, p.form)}</select></label>
      </div>
      <button class="btn primary block">검색</button>
    </form>`;
}

function setBusy(i, text) {
  const b = document.querySelector(`#pill-${i} .busy`);
  if (!b) return;
  b.textContent = text || "";
  b.classList.toggle("hidden", !text);
}

const pillIndex = (el) => Number(el.closest(".pill").id.slice(5));

resultsEl.addEventListener("change", async (e) => {
  if (!e.target.closest(".pill")) return;
  const i = pillIndex(e.target);
  if (e.target.type === "radio") {
    pills[i].chosen = e.target.value === "__none" ? null : pills[i].candidates.find((c) => c.item_seq === e.target.value);
    pills[i].decided = true;
    pills[i].editing = false;
    renderPill(i);
    renderSummary();
    const next = pills.findIndex((p) => !p.decided);
    if (next >= 0) $(`pill-${next}`).scrollIntoView({ behavior: "smooth", block: "start" });
    else summaryEl.scrollIntoView({ behavior: "smooth" });
  } else if (e.target.dataset.act === "retake" && e.target.files.length) {
    const picked = [...e.target.files].slice(0, 4);
    setBusy(i, "⏳ 다시 분석 중… (20~60초)");
    try {
      const data = await postImages(picked, "/api/identify?single=true");
      if (!data.pills.length) throw new Error("사진에서 알약을 찾지 못했습니다. " + data.photo_issues.join(" "));
      pills[i] = { ...data.pills[0], count: pills[i].count, source: "retake", chosen: null, decided: false };
      renderPill(i);
      renderSummary();
      if (data.photo_issues.length) setBusy(i, "📸 " + data.photo_issues.join(" "));
    } catch (err) {
      setBusy(i, "⚠️ " + err.message);
    }
  }
});

resultsEl.addEventListener("click", (e) => {
  const act = e.target.dataset.act;
  if (!act || !e.target.closest(".pill")) return;
  const i = pillIndex(e.target);
  if (act === "manual") {
    const form = e.target.closest(".pill").querySelector("form.manual");
    form.classList.remove("hidden");
    form.querySelector("input").focus();
  } else if (act === "more") {
    pills[i].showAll = true;
    renderPill(i);
  } else if (act === "edit") {
    pills[i].editing = true;
    renderPill(i);
  }
});

resultsEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const i = pillIndex(e.target);
  const q = Object.fromEntries(new FormData(e.target));
  if (!q.imprint_front.trim() && !q.imprint_back.trim()) {
    setBusy(i, "각인을 한 면 이상 입력해 주세요.");
    return;
  }
  setBusy(i, "⏳ 검색 중…");
  try {
    const data = await getJson("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(q),
    });
    pills[i] = { ...pills[i], ...q, imprint_confidence: "높음", has_mark: false, showAll: false,
      candidates: data.candidates, source: "manual", chosen: null, decided: false };
    renderPill(i);
    renderSummary();
  } catch (err) {
    setBusy(i, "⚠️ " + err.message);
  }
});

// ---------- 3. 확인 목록 ----------

function summaryLines() {
  return pills.map((p, i) => {
    const what = p.chosen ? `${p.chosen.item_name} (${p.chosen.item_seq})` : p.decided ? "식별 불가 - 약사 의뢰" : "미확인";
    return `${i + 1}. ${what} × ${p.count} [${describe(p)} / 각인 ${[p.imprint_front, p.imprint_back].filter(Boolean).join("/") || "없음"}]`;
  });
}

function renderSummary() {
  summaryEl.classList.remove("hidden");
  const done = pills.filter((p) => p.decided).length;
  const counter = $("done-count");
  if (counter) counter.textContent = `${done}/${pills.length} 확인`;
  $("summary-list").innerHTML = pills.map((p) => `
    <li>
      ${p.chosen ? `<b>${esc(p.chosen.item_name)}</b> × ${p.count}`
        : p.decided ? `<span class="bad">식별 불가 · 약사 의뢰</span> × ${p.count}` : `<span class="muted">미확인</span> × ${p.count}`}
      <div class="muted small">${esc(describe(p))} · 각인 ${esc([p.imprint_front, p.imprint_back].filter(Boolean).join(" / ") || "없음")}</div>
    </li>`).join("");
}

$("copy").addEventListener("click", async () => {
  const text = summaryLines().join("\n");
  try {
    await navigator.clipboard.writeText(text);
    $("copy").textContent = "✓ 복사됨";
  } catch {
    prompt("아래 내용을 복사하세요", text);
  }
  setTimeout(() => ($("copy").textContent = "📋 복사"), 1500);
});

// ---------- 사진 크게 보기 ----------

document.addEventListener("click", (e) => {
  if (!e.target.classList.contains("zoomable")) return;
  e.preventDefault();
  $("lightbox").querySelector("img").src = e.target.src;
  $("lightbox").classList.remove("hidden");
});
$("lightbox").addEventListener("click", () => $("lightbox").classList.add("hidden"));
