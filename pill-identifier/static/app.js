const fileInput = document.getElementById("file");
const runBtn = document.getElementById("run");
const previews = document.getElementById("previews");
const progress = document.getElementById("progress");
const resultsEl = document.getElementById("results");
const issuesEl = document.getElementById("issues");
const summaryEl = document.getElementById("summary");

let files = [];
let pills = [];
let options = { shapes: [], colors: [], forms: [] };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

fetch("/api/status").then((r) => r.json()).then((s) => {
  options = s;
  const el = document.getElementById("db-status");
  if (s.total === 0) {
    el.innerHTML = "⚠️ 낱알식별 DB가 비어 있습니다. <code>scripts/sync_mfds.py</code>로 식약처 데이터를 받아 주세요.";
    el.className = "warn";
  } else if (s.demo_only) {
    el.innerHTML = `⚠️ 데모 데이터(${s.total}건)만 들어 있습니다. 실제 약은 찾을 수 없습니다.`;
    el.className = "warn";
  } else {
    el.textContent = `낱알식별 DB: ${s.total.toLocaleString()}건`;
  }
});

// 긴 변 1568px JPEG로 줄여서 전송 (Claude 비전의 권장 해상도, 업로드도 빨라짐)
function shrink(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, 1568 / Math.max(img.width, img.height));
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

fileInput.addEventListener("change", () => {
  files = [...files, ...fileInput.files].slice(0, 6);
  fileInput.value = "";
  previews.innerHTML = files.map((f, i) =>
    `<figure><img src="${URL.createObjectURL(f)}" alt="사진 ${i + 1}"><button data-i="${i}" title="삭제">✕</button></figure>`).join("");
  runBtn.disabled = files.length === 0;
});

previews.addEventListener("click", (e) => {
  const i = e.target.dataset.i;
  if (i === undefined) return;
  files.splice(Number(i), 1);
  fileInput.dispatchEvent(new Event("change"));
});

runBtn.addEventListener("click", async () => {
  runBtn.disabled = true;
  progress.textContent = "분석 중… (보통 20~60초)";
  resultsEl.innerHTML = "";
  issuesEl.classList.add("hidden");
  summaryEl.classList.add("hidden");
  try {
    const form = new FormData();
    for (const [i, f] of files.entries()) form.append("images", await shrink(f), `photo${i + 1}.jpg`);
    const r = await fetch("/api/identify", { method: "POST", body: form });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `오류 ${r.status}`);
    pills = data.pills.map((p) => ({ ...p, chosen: null }));
    render(data);
    progress.textContent = "";
  } catch (e) {
    progress.textContent = "⚠️ " + e.message;
  } finally {
    runBtn.disabled = files.length === 0;
  }
});

function describe(p) {
  const color = [p.color_primary, p.color_secondary].filter((c) => c && c !== "없음").join("/");
  const imprint = [p.imprint_front, p.imprint_back].filter(Boolean).join(" | ") || (p.has_mark ? "마크만" : "각인 없음/안 보임");
  return `${color} ${p.shape} ${p.form} · ${imprint}`;
}

function checkBadge(v) {
  return v === true ? '<span class="ok">일치</span>' : v === "partial" ? '<span class="warn-text">부분 일치</span>' : v === false ? '<span class="bad">불일치</span>' : '<span class="muted">-</span>';
}

function render(data) {
  if (data.photo_issues.length) {
    issuesEl.innerHTML = "<h2>📸 사진 확인</h2><ul>" + data.photo_issues.map((s) => `<li>${esc(s)}</li>`).join("") + "</ul>";
    issuesEl.classList.remove("hidden");
  }
  if (!pills.length) {
    resultsEl.innerHTML = '<section class="card"><p>사진에서 알약을 찾지 못했습니다.</p></section>';
    return;
  }
  resultsEl.innerHTML = '<h2 class="section-title">2. 알약별 후보 확인</h2>' +
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
  if (c.length >= 2 && c[1].score >= c[0].score - 10) return `비슷한 후보가 ${c.filter((x) => x.score >= c[0].score - 10).length}개 있습니다.`;
  if (c[0].checks["각인"] === "partial") return "각인 일부가 불확실합니다.";
  return null;
}

const opts = (list, sel) => list.map((v) => `<option${v === sel ? " selected" : ""}>${esc(v)}</option>`).join("");

function renderPill(i) {
  const p = pills[i];
  const el = document.getElementById(`pill-${i}`);
  const reason = helpReason(p);
  const cands = p.candidates.map((c) => `
    <label class="cand">
      <input type="radio" name="pill${i}" value="${esc(c.item_seq)}"${p.chosen?.item_seq === c.item_seq ? " checked" : ""}>
      ${c.item_image ? `<img src="${esc(c.item_image)}" alt="" loading="lazy">` : '<div class="noimg">이미지 없음</div>'}
      <div>
        <b>${esc(c.item_name)}</b> <span class="muted">${esc(c.entp_name)} · ${esc(c.etc_otc_name)}</span><br>
        각인 <code>${esc(c.print_front) || "-"}</code> / <code>${esc(c.print_back) || "-"}</code>
        · ${esc(c.color)} ${esc(c.drug_shape)} · ${esc(c.form_code_name)}
        ${c.line ? `· 분할선 ${esc(c.line)}` : ""} ${c.size ? `· ${esc(c.size)}mm` : ""}<br>
        <small>각인 ${checkBadge(c.checks["각인"])} 모양 ${checkBadge(c.checks["모양"])}
        색상 ${checkBadge(c.checks["색상"])} 제형 ${checkBadge(c.checks["제형"])}
        · 유사도 ${c.score}</small>
      </div>
    </label>`).join("");

  el.innerHTML = `
    <h3>#${i + 1} ${esc(p.label)} <span class="muted">× ${p.count}</span>
      ${p.source === "manual" ? '<span class="src">각인 입력</span>' : p.source === "retake" ? '<span class="src">재촬영</span>' : ""}</h3>
    <p class="features">
      ${esc(describe(p))}
      ${p.score_line && p.score_line !== "없음" && p.score_line !== "불명" ? ` · 분할선 ${esc(p.score_line)}` : ""}
      ${p.mark_description ? ` · 마크: ${esc(p.mark_description)}` : ""}
      ${p.package_text ? `<br>포장 인쇄: <b>${esc(p.package_text)}</b>` : ""}
      ${p.notes ? `<br><span class="muted">${esc(p.notes)}</span>` : ""}
    </p>
    ${reason ? `
      <div class="help">
        <p>🔍 ${esc(reason)}</p>
        <div class="help-actions">
          <label class="btn primary">📷 이 알약만 다시 찍기
            <input type="file" accept="image/*" capture="environment" multiple hidden data-act="retake"></label>
          <button class="btn" data-act="manual">⌨️ 각인 입력</button>
        </div>
        <p class="muted small">재촬영: 이 알약만 흰 종이 위에 놓고 앞면·뒷면을 한 장씩 찍어 주세요.</p>
      </div>` : ""}
    <p class="busy muted hidden"></p>
    ${cands || '<p class="muted">DB에서 일치하는 후보가 없습니다.</p>'}
    <label class="cand none"><input type="radio" name="pill${i}" value="__none"${p.decided && !p.chosen ? " checked" : ""}>해당 없음 / 식별 불가 (약사 의뢰)</label>
    ${reason ? "" : `
      <p class="small muted alt">결과가 실물과 다르면:
        <label class="link">다시 찍기<input type="file" accept="image/*" capture="environment" multiple hidden data-act="retake"></label>
        · <button class="link" data-act="manual">각인 입력</button></p>`}
    <form class="manual hidden" data-act="search">
      <p class="small muted">실물에서 읽은 각인을 입력하세요. 사진 판독 값이 미리 채워져 있습니다.</p>
      <div class="grid">
        <label>앞면 각인<input name="imprint_front" value="${esc(p.imprint_front)}" autocapitalize="characters"></label>
        <label>뒷면 각인<input name="imprint_back" value="${esc(p.imprint_back)}" autocapitalize="characters"></label>
        <label>모양<select name="shape">${opts(options.shapes, p.shape)}</select></label>
        <label>색상<select name="color_primary">${opts(options.colors, p.color_primary)}</select></label>
        <label>제형<select name="form">${opts(options.forms, p.form)}</select></label>
      </div>
      <button class="btn primary">검색</button>
    </form>`;
}

function setBusy(i, text) {
  const b = document.querySelector(`#pill-${i} .busy`);
  b.textContent = text || "";
  b.classList.toggle("hidden", !text);
}

resultsEl.addEventListener("change", async (e) => {
  const card = e.target.closest(".pill");
  if (!card) return;
  const i = Number(card.id.slice(5));
  if (e.target.type === "radio") {
    pills[i].chosen = e.target.value === "__none" ? null : pills[i].candidates.find((c) => c.item_seq === e.target.value);
    pills[i].decided = true;
    renderSummary();
  } else if (e.target.dataset.act === "retake" && e.target.files.length) {
    const picked = [...e.target.files].slice(0, 4);
    setBusy(i, "다시 분석 중… (20~60초)");
    try {
      const form = new FormData();
      for (const [k, f] of picked.entries()) form.append("images", await shrink(f), `retake${k + 1}.jpg`);
      const r = await fetch("/api/identify?single=true", { method: "POST", body: form });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `오류 ${r.status}`);
      if (!data.pills.length) throw new Error("사진에서 알약을 찾지 못했습니다. " + data.photo_issues.join(" "));
      pills[i] = { ...data.pills[0], label: pills[i].label, count: pills[i].count, source: "retake", chosen: null, decided: false };
      renderPill(i);
      renderSummary();
      if (data.photo_issues.length) setBusy(i, "📸 " + data.photo_issues.join(" "));
    } catch (err) {
      setBusy(i, "⚠️ " + err.message);
    }
  }
});

resultsEl.addEventListener("click", (e) => {
  if (e.target.dataset.act !== "manual") return;
  const form = e.target.closest(".pill").querySelector("form.manual");
  form.classList.remove("hidden");
  form.querySelector("input").focus();
});

resultsEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const i = Number(e.target.closest(".pill").id.slice(5));
  const q = Object.fromEntries(new FormData(e.target));
  if (!q.imprint_front.trim() && !q.imprint_back.trim()) {
    setBusy(i, "각인을 한 면 이상 입력해 주세요.");
    return;
  }
  setBusy(i, "검색 중…");
  try {
    const r = await fetch("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(q),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `오류 ${r.status}`);
    pills[i] = { ...pills[i], ...q, imprint_confidence: "높음", has_mark: false,
      candidates: data.candidates, source: "manual", chosen: null, decided: false };
    renderPill(i);
    renderSummary();
  } catch (err) {
    setBusy(i, "⚠️ " + err.message);
  }
});

function renderSummary() {
  summaryEl.classList.remove("hidden");
  summaryEl.querySelector("tbody").innerHTML = pills.map((p, i) => `
    <tr>
      <td>${i + 1}</td>
      <td>${esc(describe(p))}</td>
      <td>${p.chosen ? `${esc(p.chosen.item_name)} <span class="muted">(${esc(p.chosen.item_seq)})</span>`
        : p.decided ? '<span class="bad">식별 불가 · 약사 의뢰</span>' : '<span class="muted">미확인</span>'}</td>
      <td>${p.count}</td>
    </tr>`).join("");
}
