const fileInput = document.getElementById("file");
const runBtn = document.getElementById("run");
const previews = document.getElementById("previews");
const progress = document.getElementById("progress");
const resultsEl = document.getElementById("results");
const issuesEl = document.getElementById("issues");
const summaryEl = document.getElementById("summary");

let files = [];
let pills = [];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

fetch("/api/status").then((r) => r.json()).then((s) => {
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
  resultsEl.innerHTML = '<h2 class="section-title">2. 알약별 후보 확인</h2>' + pills.map((p, i) => {
    const lowConf = !p.imprint_front && !p.imprint_back || ["낮음", "없음"].includes(p.imprint_confidence);
    const cands = p.candidates.map((c) => `
      <label class="cand">
        <input type="radio" name="pill${i}" value="${esc(c.item_seq)}">
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
    return `
      <section class="card pill">
        <h3>#${i + 1} ${esc(p.label)} <span class="muted">× ${p.count}</span></h3>
        <p class="features">
          ${esc(describe(p))}
          ${p.score_line !== "없음" && p.score_line !== "불명" ? ` · 분할선 ${esc(p.score_line)}` : ""}
          ${p.mark_description ? ` · 마크: ${esc(p.mark_description)}` : ""}
          ${p.package_text ? `<br>포장 인쇄: <b>${esc(p.package_text)}</b>` : ""}
          ${p.notes ? `<br><span class="muted">${esc(p.notes)}</span>` : ""}
        </p>
        ${lowConf ? '<p class="warn">각인이 확실하지 않습니다. 모양·색만으로는 약을 특정할 수 없으니 재촬영하거나 약사에게 의뢰하세요.</p>' : ""}
        ${cands || '<p class="muted">DB에서 일치하는 후보가 없습니다.</p>'}
        <label class="cand none"><input type="radio" name="pill${i}" value="__none">해당 없음 / 식별 불가 (약사 의뢰)</label>
      </section>`;
  }).join("");

  resultsEl.querySelectorAll("input[type=radio]").forEach((el) =>
    el.addEventListener("change", () => {
      const i = Number(el.name.slice(4));
      pills[i].chosen = el.value === "__none" ? null : pills[i].candidates.find((c) => c.item_seq === el.value);
      pills[i].decided = true;
      renderSummary();
    }));
  renderSummary();
}

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
