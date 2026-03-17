from flask import Flask, jsonify, request, render_template_string
import requests
import re
import io
from pypdf import PdfReader

app = Flask(__name__)


HTML_PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sawbona | TenderAI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #0b0f0c;
      --panel: rgba(18, 26, 20, 0.82);
      --panel-2: rgba(24, 36, 28, 0.88);
      --border: rgba(139, 170, 126, 0.18);
      --text: #edf3ec;
      --muted: #9fb09c;
      --green: #7fb069;
      --green-2: #5f8f5b;
      --green-3: #a7d08c;
      --gold: #d9c27c;
      --danger: #d27d7d;
      --shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
      --radius: 20px;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(95, 143, 91, 0.22), transparent 35%),
        radial-gradient(circle at top right, rgba(127, 176, 105, 0.12), transparent 30%),
        linear-gradient(180deg, #08100a 0%, #0b0f0c 100%);
      color: var(--text);
      min-height: 100vh;
    }

    .shell {
      max-width: 1320px;
      margin: 0 auto;
      padding: 24px;
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 18px;
      margin-bottom: 20px;
      border: 1px solid var(--border);
      background: rgba(12, 18, 14, 0.72);
      backdrop-filter: blur(16px);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .brand-mark {
      width: 42px;
      height: 42px;
      border-radius: 14px;
      background: linear-gradient(135deg, var(--green-3), var(--green-2));
      display: grid;
      place-items: center;
      color: #102013;
      font-weight: 900;
      box-shadow: 0 10px 30px rgba(127, 176, 105, 0.28);
    }

    .brand h1 {
      margin: 0;
      font-size: 18px;
      letter-spacing: 0.02em;
    }

    .brand small {
      display: block;
      color: var(--muted);
      margin-top: 2px;
    }

    .hero {
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 18px;
      margin-bottom: 18px;
    }

    .hero-panel,
    .panel,
    .metric,
    .tender-card {
      background: var(--panel);
      backdrop-filter: blur(18px);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }

    .hero-panel {
      padding: 30px;
      position: relative;
      overflow: hidden;
    }

    .hero-panel::after {
      content: "";
      position: absolute;
      inset: auto -60px -60px auto;
      width: 220px;
      height: 220px;
      background: radial-gradient(circle, rgba(127, 176, 105, 0.14), transparent 70%);
      pointer-events: none;
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--green-3);
      text-transform: uppercase;
      letter-spacing: 0.16em;
      margin-bottom: 12px;
    }

    .hero h2 {
      margin: 0 0 12px 0;
      font-size: 40px;
      line-height: 1.05;
      max-width: 720px;
    }

    .hero p {
      margin: 0;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.6;
      max-width: 760px;
    }

    .hero-side {
      padding: 24px;
      display: grid;
      gap: 14px;
      align-content: start;
    }

    .pulse {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--green-3);
      font-weight: 700;
      font-size: 14px;
    }

    .pulse-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--green-3);
      box-shadow: 0 0 0 0 rgba(167, 208, 140, 0.6);
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(167, 208, 140, 0.5); }
      70% { box-shadow: 0 0 0 12px rgba(167, 208, 140, 0); }
      100% { box-shadow: 0 0 0 0 rgba(167, 208, 140, 0); }
    }

    .ai-steps {
      display: grid;
      gap: 10px;
      color: var(--muted);
      font-size: 14px;
    }

    .ai-step {
      padding: 12px 14px;
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.05);
    }

    .layout {
      display: grid;
      grid-template-columns: 390px 1fr;
      gap: 18px;
      align-items: start;
    }

    .panel {
      padding: 20px;
    }

    .panel h3, .panel h4 {
      margin-top: 0;
    }

    label {
      display: block;
      margin-bottom: 8px;
      font-size: 13px;
      font-weight: 700;
      color: #dfe7dd;
      letter-spacing: 0.02em;
    }

    input,
    textarea,
    button {
      width: 100%;
      border-radius: 14px;
      border: 1px solid rgba(167, 208, 140, 0.16);
      background: rgba(255, 255, 255, 0.04);
      color: var(--text);
      padding: 12px 14px;
      font-size: 14px;
      outline: none;
      transition: 0.2s ease;
      margin-bottom: 14px;
    }

    input:focus,
    textarea:focus {
      border-color: rgba(167, 208, 140, 0.45);
      box-shadow: 0 0 0 4px rgba(127, 176, 105, 0.12);
    }

    textarea {
      min-height: 120px;
      resize: vertical;
    }

    .btn {
      background: linear-gradient(135deg, var(--green), var(--green-2));
      color: #102013;
      font-weight: 800;
      border: none;
      cursor: pointer;
      box-shadow: 0 16px 30px rgba(95, 143, 91, 0.28);
    }

    .btn:hover {
      transform: translateY(-1px);
      filter: brightness(1.04);
    }

    .btn-secondary {
      background: rgba(255, 255, 255, 0.03);
      color: var(--text);
      border: 1px solid rgba(255, 255, 255, 0.06);
      box-shadow: none;
    }

    .hint {
      color: var(--muted);
      font-size: 12px;
      margin-top: -6px;
      margin-bottom: 12px;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 14px;
      margin-bottom: 18px;
    }

    .metric {
      padding: 18px;
    }

    .metric-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 8px;
    }

    .metric-value {
      font-size: 30px;
      font-weight: 900;
      margin-bottom: 6px;
    }

    .metric-sub {
      color: var(--muted);
      font-size: 13px;
    }

    .insight-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
      margin-bottom: 18px;
    }

    .keyword-cloud {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .chip {
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(127, 176, 105, 0.12);
      border: 1px solid rgba(127, 176, 105, 0.18);
      color: #d9eed1;
      font-size: 12px;
      font-weight: 700;
    }

    pre {
      margin: 0;
      background: rgba(255, 255, 255, 0.03);
      color: #d7e2d3;
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 14px;
      padding: 14px;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 12px;
      line-height: 1.5;
    }

    .loading-box {
      display: none;
      margin-bottom: 16px;
      padding: 18px;
      border-radius: 18px;
      background: rgba(127, 176, 105, 0.08);
      border: 1px solid rgba(127, 176, 105, 0.18);
    }

    .loading-title {
      font-weight: 800;
      color: #dff0d5;
      margin-bottom: 10px;
    }

    .loading-steps {
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 14px;
    }

    .results-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
      gap: 12px;
      flex-wrap: wrap;
    }

    .results-head h3 {
      margin: 0;
    }

    .results-head .sub {
      color: var(--muted);
      font-size: 13px;
    }

    .tender-card {
      padding: 20px;
      margin-bottom: 16px;
      background: var(--panel-2);
    }

    .tender-top {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: start;
      margin-bottom: 14px;
    }

    .tender-title {
      margin: 0 0 6px 0;
      font-size: 22px;
      line-height: 1.2;
    }

    .tender-meta {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }

    .score-block {
      min-width: 180px;
      text-align: right;
    }

    .band {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 10px;
    }

    .band.high {
      background: rgba(127, 176, 105, 0.14);
      color: #d9eed1;
      border: 1px solid rgba(127, 176, 105, 0.28);
    }

    .band.medium {
      background: rgba(217, 194, 124, 0.14);
      color: #f2e4b4;
      border: 1px solid rgba(217, 194, 124, 0.28);
    }

    .band.low {
      background: rgba(210, 125, 125, 0.12);
      color: #f0cccc;
      border: 1px solid rgba(210, 125, 125, 0.28);
    }

    .score-number {
      font-size: 28px;
      font-weight: 900;
    }

    .score-bar {
      width: 100%;
      height: 10px;
      background: rgba(255, 255, 255, 0.06);
      border-radius: 999px;
      overflow: hidden;
      margin-top: 8px;
    }

    .score-bar-fill {
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #d27d7d 0%, #d9c27c 45%, #7fb069 100%);
    }

    .two-col {
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 18px;
      margin-top: 14px;
    }

    .mini-panel {
      padding: 16px;
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.05);
    }

    .mini-panel h4 {
      margin: 0 0 10px 0;
      font-size: 14px;
      color: #dce8d8;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .value-big {
      font-size: 28px;
      font-weight: 900;
      margin-bottom: 6px;
    }

    .muted {
      color: var(--muted);
    }

    .why-list {
      display: grid;
      gap: 8px;
    }

    .why-item {
      display: flex;
      align-items: start;
      gap: 10px;
      color: #deead9;
      font-size: 14px;
      line-height: 1.45;
    }

    .check {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      flex: 0 0 22px;
      background: rgba(127, 176, 105, 0.15);
      color: #b8dfaa;
      font-weight: 900;
      font-size: 12px;
    }

    .empty {
      padding: 20px;
      border-radius: 16px;
      border: 1px dashed rgba(255, 255, 255, 0.08);
      color: var(--muted);
      text-align: center;
    }

    @media (max-width: 1100px) {
      .hero,
      .layout,
      .insight-grid,
      .two-col {
        grid-template-columns: 1fr;
      }

      .metrics {
        grid-template-columns: 1fr 1fr;
      }
    }

    @media (max-width: 700px) {
      .metrics {
        grid-template-columns: 1fr;
      }

      .tender-top {
        flex-direction: column;
      }

      .score-block {
        min-width: 0;
        text-align: left;
      }

      .shell {
        padding: 14px;
      }

      .hero h2 {
        font-size: 30px;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="topbar">
      <div class="brand">
        <div class="brand-mark">S</div>
        <div>
          <h1>SAWBONA | TenderAI</h1>
          <small>AI-powered procurement intelligence</small>
        </div>
      </div>
      <div class="pulse">
        <span class="pulse-dot"></span>
        Live public tender analysis
      </div>
    </div>

    <div class="hero">
      <div class="hero-panel">
        <div class="eyebrow">Opportunity Intelligence Engine</div>
        <h2>Find the tenders your business is actually built to win.</h2>
        <p>
          Upload a supplier profile, let TenderAI read the signals in your business,
          compare them against live public procurement opportunities, and surface the
          highest-fit tenders with AI-style scoring, estimated project value, and clear reasons.
        </p>
      </div>

      <div class="hero-side hero-panel">
        <div class="pulse">
          <span class="pulse-dot"></span>
          TenderAI workflow
        </div>
        <div class="ai-steps">
          <div class="ai-step">1. Read supplier profile PDF or pasted company profile text</div>
          <div class="ai-step">2. Scan public tender releases from the government data source</div>
          <div class="ai-step">3. Score opportunity fit using capability, intent, and category signals</div>
          <div class="ai-step">4. Estimate likely tender value range where no official value is published</div>
        </div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h3 style="margin-bottom: 14px;">Run a TenderAI scan</h3>

        <form id="scoreForm">
          <label for="profile_pdf">Supplier profile PDF</label>
          <input type="file" id="profile_pdf" name="profile_pdf" accept=".pdf">
          <div class="hint">Preferred input: CSD summary, capability profile, or company brochure PDF.</div>

          <label for="profile_text">Or paste profile text</label>
          <textarea id="profile_text" name="profile_text" placeholder="Paste supplier profile text here if you are not uploading a PDF."></textarea>

          <label for="date_from">From date</label>
          <input type="date" id="date_from" name="date_from" value="2026-01-01">

          <label for="date_to">To date</label>
          <input type="date" id="date_to" name="date_to" value="2026-03-17">

          <label for="page_number">Page number</label>
          <input type="number" id="page_number" name="page_number" value="1" min="1">

          <label for="page_size">Page size</label>
          <input type="number" id="page_size" name="page_size" value="10" min="1" max="100">

          <button class="btn" type="submit">Analyze opportunities</button>
        </form>

        <button class="btn btn-secondary" type="button" id="clearBtn">Clear form</button>
      </div>

      <div>
        <div class="loading-box" id="loadingBox">
          <div class="loading-title">TenderAI is working...</div>
          <div class="loading-steps" id="loadingSteps">
            <div>🔍 Scanning government tender releases...</div>
            <div>🧠 Reading your business profile...</div>
            <div>⚡ Matching opportunities to your capabilities...</div>
          </div>
        </div>

        <div class="metrics" id="metrics" style="display:none;">
          <div class="metric">
            <div class="metric-label">Returned tenders</div>
            <div class="metric-value" id="mTotal">0</div>
            <div class="metric-sub">Scored opportunities</div>
          </div>
          <div class="metric">
            <div class="metric-label">High fit</div>
            <div class="metric-value" id="mHigh">0</div>
            <div class="metric-sub">Best opportunities</div>
          </div>
          <div class="metric">
            <div class="metric-label">Medium fit</div>
            <div class="metric-value" id="mMedium">0</div>
            <div class="metric-sub">Worth reviewing</div>
          </div>
          <div class="metric">
            <div class="metric-label">Low fit</div>
            <div class="metric-value" id="mLow">0</div>
            <div class="metric-sub">Lower priority</div>
          </div>
        </div>

        <div class="insight-grid">
          <div class="panel">
            <h3>Profile insights</h3>
            <div class="muted" id="profileSource">No profile processed yet.</div>
            <h4 style="margin-top:18px;">Extracted keywords</h4>
            <div class="keyword-cloud" id="keywordCloud"></div>
            <h4 style="margin-top:18px;">Profile preview</h4>
            <pre id="profilePreview">No profile preview yet.</pre>
          </div>

          <div class="panel">
            <h3>Request configuration</h3>
            <pre id="requestUsed">No request executed yet.</pre>
          </div>
        </div>

        <div class="panel">
          <div class="results-head">
            <div>
              <h3>Matched opportunities</h3>
              <div class="sub">TenderAI ranked results using live procurement data and your profile signals.</div>
            </div>
          </div>
          <div id="results">
            <div class="empty">Run your first scan to see matched opportunities here.</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const form = document.getElementById("scoreForm");
    const clearBtn = document.getElementById("clearBtn");
    const loadingBox = document.getElementById("loadingBox");
    const metrics = document.getElementById("metrics");
    const results = document.getElementById("results");
    const profileSource = document.getElementById("profileSource");
    const keywordCloud = document.getElementById("keywordCloud");
    const profilePreview = document.getElementById("profilePreview");
    const requestUsed = document.getElementById("requestUsed");

    const mTotal = document.getElementById("mTotal");
    const mHigh = document.getElementById("mHigh");
    const mMedium = document.getElementById("mMedium");
    const mLow = document.getElementById("mLow");

    function bandClass(band) {
      if (band === "High fit") return "band high";
      if (band === "Medium fit") return "band medium";
      return "band low";
    }

    function safeText(value) {
      return value === null || value === undefined || value === "" ? "N/A" : value;
    }

    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function renderKeywords(keywords) {
      if (!keywords || keywords.length === 0) {
        keywordCloud.innerHTML = '<div class="muted">No keywords extracted.</div>';
        return;
      }

      keywordCloud.innerHTML = keywords
        .map(k => `<span class="chip">${escapeHtml(k)}</span>`)
        .join("");
    }

    function renderWhyMatched(t) {
      const items = [];

      if (t.matched_keywords && t.matched_keywords.length > 0) {
        items.push(`Matched capability keywords: ${t.matched_keywords.join(", ")}`);
      }

      if (t.category && (t.category.toLowerCase() === "works" || t.category.toLowerCase() === "services")) {
        items.push(`Tender category aligns with operational service delivery work.`);
      }

      if (t.estimation_reason) {
        items.push(`Estimated project size insight: ${t.estimation_reason}`);
      }

      if (items.length === 0) {
        items.push("TenderAI found limited matching signals, so this opportunity was ranked lower.");
      }

      return items.map(item => `
        <div class="why-item">
          <div class="check">✓</div>
          <div>${escapeHtml(item)}</div>
        </div>
      `).join("");
    }

    function renderTenders(tenders) {
      if (!tenders || tenders.length === 0) {
        results.innerHTML = '<div class="empty">No tenders found for this scan.</div>';
        return;
      }

      results.innerHTML = tenders.map(t => {
        const scoreWidth = Math.max(0, Math.min(100, Number(t.fit_score || 0)));
        return `
          <div class="tender-card">
            <div class="tender-top">
              <div>
                <h3 class="tender-title">${escapeHtml(safeText(t.title))}</h3>
                <div class="tender-meta">
                  ${escapeHtml(safeText(t.buyer))} •
                  ${escapeHtml(safeText(t.category))} •
                  closes ${escapeHtml(safeText(t.close_date))}
                </div>
              </div>

              <div class="score-block">
                <div class="${bandClass(t.fit_band)}">${escapeHtml(safeText(t.fit_band))}</div>
                <div class="score-number">${escapeHtml(safeText(t.fit_score))}/100</div>
                <div class="score-bar">
                  <div class="score-bar-fill" style="width:${scoreWidth}%;"></div>
                </div>
              </div>
            </div>

            <div class="mini-panel" style="margin-bottom:14px;">
              <h4>Description of work</h4>
              <div class="muted">${escapeHtml(safeText(t.description))}</div>
            </div>

            <div class="two-col">
              <div class="mini-panel">
                <h4>Why this matches you</h4>
                <div class="why-list">
                  ${renderWhyMatched(t)}
                </div>
              </div>

              <div class="mini-panel">
                <h4>Estimated deal value</h4>
                <div class="value-big">${escapeHtml(safeText(t.value_display))}</div>
                <div class="muted">
                  Source: ${escapeHtml(safeText(t.value_source))} •
                  Confidence: ${escapeHtml(safeText(t.estimation_confidence))}
                </div>
                <div style="margin-top:10px;" class="muted">${escapeHtml(safeText(t.estimation_reason))}</div>
                <div style="margin-top:14px;" class="muted">
                  OCID: ${escapeHtml(safeText(t.ocid))}
                </div>
              </div>
            </div>
          </div>
        `;
      }).join("");
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();

      loadingBox.style.display = "block";
      metrics.style.display = "none";
      results.innerHTML = '<div class="empty">Processing your scan...</div>';

      try {
        const pdfFile = document.getElementById("profile_pdf").files[0];
        const profileText = document.getElementById("profile_text").value.trim();

        let response;

        if (pdfFile) {
          const formData = new FormData();
          formData.append("profile_pdf", pdfFile);
          formData.append("date_from", document.getElementById("date_from").value);
          formData.append("date_to", document.getElementById("date_to").value);
          formData.append("page_number", document.getElementById("page_number").value);
          formData.append("page_size", document.getElementById("page_size").value);

          response = await fetch("/score", {
            method: "POST",
            body: formData
          });
        } else {
          response = await fetch("/score", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              profile_text: profileText,
              date_from: document.getElementById("date_from").value,
              date_to: document.getElementById("date_to").value,
              page_number: Number(document.getElementById("page_number").value),
              page_size: Number(document.getElementById("page_size").value)
            })
          });
        }

        const data = await response.json();

        if (data.status !== "ok") {
          results.innerHTML = `<div class="empty">Error: ${escapeHtml(data.error || "Unknown error")}</div>`;
          return;
        }

        metrics.style.display = "grid";
        mTotal.textContent = safeText(data.summary.returned_tenders);
        mHigh.textContent = safeText(data.summary.high_fit);
        mMedium.textContent = safeText(data.summary.medium_fit);
        mLow.textContent = safeText(data.summary.low_fit);

        profileSource.textContent = "Profile source: " + safeText(data.profile_source);
        renderKeywords(data.profile_keywords || []);
        profilePreview.textContent = data.profile_text_preview || "No profile preview available.";
        requestUsed.textContent = JSON.stringify(data.request_used || {}, null, 2);

        renderTenders(data.tenders || []);
      } catch (err) {
        results.innerHTML = `<div class="empty">Error: ${escapeHtml(err.message)}</div>`;
      } finally {
        loadingBox.style.display = "none";
      }
    });

    clearBtn.addEventListener("click", () => {
      document.getElementById("profile_pdf").value = "";
      document.getElementById("profile_text").value = "";
      document.getElementById("page_number").value = "1";
      document.getElementById("page_size").value = "10";
      metrics.style.display = "none";
      profileSource.textContent = "No profile processed yet.";
      keywordCloud.innerHTML = "";
      profilePreview.textContent = "No profile preview yet.";
      requestUsed.textContent = "No request executed yet.";
      results.innerHTML = '<div class="empty">Run your first scan to see matched opportunities here.</div>';
    });
  </script>
</body>
</html>
"""


@app.get("/")
def ui():
    return render_template_string(HTML_PAGE)


@app.get("/health")
def health():
    return {"status": "ok"}


def extract_releases(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    for key in ["releases", "data", "value", "results", "items"]:
        value = payload.get(key)
        if isinstance(value, list):
            return value

    if "ocid" in payload or "tender" in payload or "buyer" in payload:
        return [payload]

    return []


def tokenize(text):
    if not text:
        return []

    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    stopwords = {
        "the", "and", "for", "with", "from", "that", "this", "are", "was",
        "your", "you", "our", "have", "has", "will", "not", "all", "can",
        "services", "service", "company", "business", "profile", "south",
        "africa", "of", "to", "in", "on", "by", "at", "is", "as", "or",
        "an", "be", "we", "it", "their", "its", "pty", "ltd", "cc",
        "supplier", "summary", "report", "registration", "database",
        "government"
    }
    return [w for w in words if len(w) > 2 and w not in stopwords]


def extract_pdf_text(file_storage):
    pdf_bytes = file_storage.read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []

    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(text)

    return "\\n".join(pages)


def extract_profile_text():
    if "profile_pdf" in request.files:
        uploaded_file = request.files["profile_pdf"]
        if uploaded_file and uploaded_file.filename.lower().endswith(".pdf"):
            return extract_pdf_text(uploaded_file), "pdf"

    body = request.get_json(silent=True) or {}
    profile_text = body.get("profile_text", "")
    return profile_text, "text"


def get_request_value(name, default_value):
    if request.content_type and "multipart/form-data" in request.content_type:
        return request.form.get(name, default_value)

    body = request.get_json(silent=True) or {}
    return body.get(name, default_value)


def score_tender(profile_keywords, tender_text, category=""):
    tender_tokens = set(tokenize(tender_text))
    profile_set = set(profile_keywords)

    matched = sorted(profile_set.intersection(tender_tokens))
    base_score = (len(matched) / max(len(profile_set), 1)) * 100

    bonus = 0

    if category:
      category = category.lower()
      if category in ["works", "services"]:
          bonus += 10

    intent_keywords = ["installation", "maintenance", "repair", "construction"]
    intent_hits = [k for k in intent_keywords if k in tender_tokens]
    bonus += len(intent_hits) * 5

    final_score = round(min(base_score + bonus, 100), 1)

    if final_score >= 70:
        fit_band = "High fit"
    elif final_score >= 40:
        fit_band = "Medium fit"
    else:
        fit_band = "Low fit"

    return final_score, fit_band, matched


def estimate_tender_value(title, description, category):
    text = f"{title} {description}".lower()

    low = 50000
    high = 300000
    confidence = "Low"
    reason = "Generic service estimate based on tender wording."

    if "generator" in text:
        low = 800000
        high = 3000000
        confidence = "Medium"
        reason = "Generator installations typically fall within this range."
    elif any(k in text for k in ["construction", "building", "infrastructure"]):
        low = 500000
        high = 5000000
        confidence = "Medium"
        reason = "Construction and infrastructure tenders are usually medium to high value."
    elif any(k in text for k in ["maintenance", "repair", "servicing"]):
        low = 100000
        high = 1000000
        confidence = "Medium"
        reason = "Maintenance and repair contracts vary with scope and contract term."
    elif any(k in text for k in ["truck", "vehicle", "fire truck"]):
        low = 1000000
        high = 8000000
        confidence = "High"
        reason = "Specialized vehicles are typically high-value procurements."
    elif any(k in text for k in ["server", "hardware", "storage", "backup appliance"]):
        low = 200000
        high = 2000000
        confidence = "Medium"
        reason = "IT infrastructure procurement depends on scale and specification."
    elif category and category.lower() == "goods":
        low = 50000
        high = 1000000
        confidence = "Low"
        reason = "General goods procurement estimate."

    value_display = f"R{low:,.0f} - R{high:,.0f}"

    return {
        "value_display": value_display,
        "value_source": "estimated",
        "estimation_confidence": confidence,
        "estimation_reason": reason,
        "estimated_value_low": low,
        "estimated_value_high": high,
        "estimated_value_mid": round((low + high) / 2, 0)
    }


@app.post("/score")
def score():
    profile_text, profile_source = extract_profile_text()

    date_from = get_request_value("date_from", "2026-01-01")
    date_to = get_request_value("date_to", "2026-03-17")
    page_number = int(get_request_value("page_number", 1))
    page_size = int(get_request_value("page_size", 10))

    profile_keywords = tokenize(profile_text)[:25]

    url = "https://ocds-api.etenders.gov.za/api/OCDSReleases"
    params = {
        "PageNumber": page_number,
        "PageSize": page_size,
        "dateFrom": date_from,
        "dateTo": date_to
    }

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        releases = extract_releases(data)

        tenders = []
        for item in releases:
            tender = item.get("tender", {}) if isinstance(item, dict) else {}
            buyer = item.get("buyer", {}) if isinstance(item, dict) else {}
            tender_period = tender.get("tenderPeriod", {}) if isinstance(tender, dict) else {}
            value = tender.get("value", {}) if isinstance(tender, dict) else {}

            description = tender.get("description", "") or ""
            title = tender.get("title", "") or ""
            buyer_name = buyer.get("name", "") or ""
            category = tender.get("mainProcurementCategory", "") or ""

            combined_text = f"{title} {description} {buyer_name} {category}"
            fit_score, fit_band, matched_keywords = score_tender(
                profile_keywords,
                combined_text,
                category
            )

            published_value = value.get("amount")
            published_currency = value.get("currency")
            estimation = estimate_tender_value(title, description, category)

            if published_value and published_value > 0:
                value_display = f"R{published_value:,.0f}"
                value_source = "published"
                estimation_confidence = "High"
                estimation_reason = "Published by tender source."
                estimated_value_low = published_value
                estimated_value_high = published_value
                estimated_value_mid = published_value
            else:
                value_display = estimation["value_display"]
                value_source = estimation["value_source"]
                estimation_confidence = estimation["estimation_confidence"]
                estimation_reason = estimation["estimation_reason"]
                estimated_value_low = estimation["estimated_value_low"]
                estimated_value_high = estimation["estimated_value_high"]
                estimated_value_mid = estimation["estimated_value_mid"]

            tenders.append({
                "ocid": item.get("ocid") if isinstance(item, dict) else None,
                "title": title,
                "buyer": buyer_name,
                "description": description,
                "status": tender.get("status"),
                "category": category,
                "close_date": tender_period.get("endDate"),
                "value_amount": published_value,
                "value_currency": published_currency,
                "value_display": value_display,
                "value_source": value_source,
                "estimation_confidence": estimation_confidence,
                "estimation_reason": estimation_reason,
                "estimated_value_low": estimated_value_low,
                "estimated_value_high": estimated_value_high,
                "estimated_value_mid": estimated_value_mid,
                "fit_score": fit_score,
                "fit_band": fit_band,
                "matched_keywords": matched_keywords
            })

        tenders = sorted(tenders, key=lambda x: x["fit_score"], reverse=True)

        return jsonify({
            "status": "ok",
            "profile_source": profile_source,
            "profile_text_preview": profile_text[:500],
            "profile_keywords": profile_keywords,
            "request_used": params,
            "summary": {
                "total_releases_found": len(releases),
                "returned_tenders": len(tenders),
                "high_fit": sum(1 for t in tenders if t["fit_band"] == "High fit"),
                "medium_fit": sum(1 for t in tenders if t["fit_band"] == "Medium fit"),
                "low_fit": sum(1 for t in tenders if t["fit_band"] == "Low fit")
            },
            "tenders": tenders
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500
