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
      --bg: #f7f9fc;
      --panel: #ffffff;
      --panel-2: #fbfcfe;
      --border: #e5ebf3;
      --text: #0f172a;
      --muted: #5f6b7a;
      --brand: #1f6feb;
      --brand-2: #0ea5e9;
      --brand-3: #e8f2ff;
      --green: #0f9d58;
      --amber: #d97706;
      --red: #dc2626;
      --shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
      --radius: 22px;
      --chat-user: #eef6ff;
      --chat-ai: #ffffff;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(14, 165, 233, 0.10), transparent 24%),
        radial-gradient(circle at top right, rgba(31, 111, 235, 0.08), transparent 22%),
        linear-gradient(180deg, #f8fbff 0%, #f5f7fb 100%);
      min-height: 100vh;
    }

    .shell {
      max-width: 1380px;
      margin: 0 auto;
      padding: 20px;
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 14px 18px;
      background: rgba(255, 255, 255, 0.88);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
      margin-bottom: 18px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .brand img {
      height: 42px;
      width: auto;
      display: block;
    }

    .brand-title {
      font-size: 18px;
      font-weight: 800;
      letter-spacing: 0.02em;
    }

    .brand-sub {
      color: var(--muted);
      font-size: 13px;
      margin-top: 2px;
    }

    .hero {
      padding: 28px;
      border-radius: var(--radius);
      background:
        linear-gradient(135deg, rgba(31, 111, 235, 0.05) 0%, rgba(14, 165, 233, 0.07) 100%),
        var(--panel);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      margin-bottom: 18px;
    }

    .hero h1 {
      margin: 0 0 10px 0;
      font-size: 42px;
      line-height: 1.05;
    }

    .hero p {
      margin: 0;
      max-width: 900px;
      color: var(--muted);
      line-height: 1.65;
      font-size: 16px;
    }

    .tabs {
      display: flex;
      gap: 10px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }

    .tab-btn {
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.85);
      color: var(--muted);
      font-weight: 700;
      padding: 12px 16px;
      border-radius: 14px;
      cursor: pointer;
      transition: 0.2s ease;
      box-shadow: 0 10px 24px rgba(15, 23, 42, 0.04);
    }

    .tab-btn.active {
      color: var(--brand);
      background: var(--brand-3);
      border-color: rgba(31, 111, 235, 0.18);
    }

    .page {
      display: none;
    }

    .page.active {
      display: block;
    }

    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }

    .page-layout {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 18px;
      align-items: start;
    }

    .assistant-column {
      display: grid;
      gap: 18px;
    }

    .chat-panel {
      padding: 18px;
    }

    .chat-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }

    .chat-title {
      font-size: 20px;
      font-weight: 800;
      margin: 0;
    }

    .chat-sub {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #fbfdff;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }

    .chat-thread {
      display: grid;
      gap: 14px;
    }

    .message {
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }

    .message.user {
      flex-direction: row-reverse;
    }

    .avatar {
      width: 38px;
      height: 38px;
      border-radius: 14px;
      display: grid;
      place-items: center;
      font-weight: 900;
      flex: 0 0 38px;
      color: white;
      box-shadow: 0 10px 22px rgba(15, 23, 42, 0.10);
    }

    .avatar.ai {
      background: linear-gradient(135deg, var(--brand), var(--brand-2));
    }

    .avatar.user {
      background: linear-gradient(135deg, #0f172a, #334155);
    }

    .bubble {
      max-width: calc(100% - 60px);
      padding: 16px 18px;
      border-radius: 18px;
      border: 1px solid var(--border);
      line-height: 1.65;
      box-shadow: 0 10px 28px rgba(15, 23, 42, 0.04);
    }

    .bubble.ai {
      background: var(--chat-ai);
    }

    .bubble.user {
      background: var(--chat-user);
    }

    .bubble h3,
    .bubble h4 {
      margin-top: 0;
    }

    .bubble p {
      margin: 0 0 10px 0;
    }

    .bubble p:last-child {
      margin-bottom: 0;
    }

    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }

    label {
      display: block;
      margin-bottom: 8px;
      font-size: 13px;
      font-weight: 700;
      color: #334155;
    }

    input,
    textarea,
    button,
    select {
      width: 100%;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--text);
      padding: 12px 14px;
      font-size: 14px;
      outline: none;
      transition: 0.2s ease;
    }

    input:focus,
    textarea:focus,
    select:focus {
      border-color: rgba(31, 111, 235, 0.28);
      box-shadow: 0 0 0 4px rgba(31, 111, 235, 0.08);
    }

    textarea {
      min-height: 120px;
      resize: vertical;
    }

    .field {
      margin-bottom: 14px;
    }

    .field.full {
      grid-column: 1 / -1;
    }

    .hint {
      color: var(--muted);
      font-size: 12px;
      margin-top: 8px;
    }

    .actions {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }

    .btn {
      border: none;
      cursor: pointer;
      font-weight: 800;
      border-radius: 16px;
      padding: 13px 16px;
      transition: 0.2s ease;
    }

    .btn-primary {
      background: linear-gradient(135deg, var(--brand), var(--brand-2));
      color: white;
      box-shadow: 0 14px 28px rgba(31, 111, 235, 0.22);
    }

    .btn-secondary {
      background: #ffffff;
      border: 1px solid var(--border);
      color: var(--text);
    }

    .btn:hover {
      transform: translateY(-1px);
    }

    .summary-grid {
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

    .results-panel {
      padding: 18px;
    }

    .results-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }

    .results-head h2 {
      margin: 0;
      font-size: 20px;
    }

    .results-sub {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
    }

    .loading-wrap {
      display: none;
      padding: 20px;
      border: 1px solid var(--border);
      border-radius: 18px;
      background: #fbfdff;
      margin-bottom: 16px;
    }

    .loading-row {
      display: flex;
      align-items: center;
      gap: 16px;
    }

    .orb-loader {
      width: 54px;
      height: 54px;
      border-radius: 50%;
      position: relative;
      background:
        radial-gradient(circle at center, rgba(31, 111, 235, 0.05) 0%, transparent 62%);
      border: 1px solid rgba(31, 111, 235, 0.12);
    }

    .orb-loader::before {
      content: "";
      position: absolute;
      inset: 5px;
      border-radius: 50%;
      border: 1px dashed rgba(31, 111, 235, 0.18);
    }

    .orb-loader::after {
      content: "";
      position: absolute;
      top: 4px;
      left: 50%;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: linear-gradient(135deg, var(--brand), var(--brand-2));
      transform: translateX(-50%);
      box-shadow: 0 0 18px rgba(31, 111, 235, 0.8);
      animation: orbit 1.25s linear infinite;
      transform-origin: 0 23px;
    }

    @keyframes orbit {
      from {
        transform: rotate(0deg) translateX(-50%);
      }
      to {
        transform: rotate(360deg) translateX(-50%);
      }
    }

    .loading-title {
      font-weight: 800;
      font-size: 16px;
      margin-bottom: 6px;
    }

    .loading-step {
      color: var(--muted);
      font-size: 14px;
    }

    .tender-list {
      display: grid;
      gap: 14px;
    }

    .tender-card {
      padding: 18px;
    }

    .tender-top {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
      margin-bottom: 14px;
    }

    .tender-title {
      margin: 0 0 6px 0;
      font-size: 21px;
      line-height: 1.25;
    }

    .tender-meta {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }

    .band {
      display: inline-flex;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 10px;
    }

    .band.high {
      background: rgba(15, 157, 88, 0.10);
      color: var(--green);
      border: 1px solid rgba(15, 157, 88, 0.18);
    }

    .band.medium {
      background: rgba(217, 119, 6, 0.10);
      color: var(--amber);
      border: 1px solid rgba(217, 119, 6, 0.18);
    }

    .band.low {
      background: rgba(220, 38, 38, 0.08);
      color: var(--red);
      border: 1px solid rgba(220, 38, 38, 0.16);
    }

    .score-number {
      font-size: 28px;
      font-weight: 900;
      text-align: right;
    }

    .score-caption {
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }

    .two-col {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 14px;
    }

    .mini {
      padding: 16px;
      background: var(--panel-2);
      border: 1px solid var(--border);
      border-radius: 18px;
    }

    .mini h4 {
      margin: 0 0 10px 0;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #334155;
    }

    .value-big {
      font-size: 28px;
      font-weight: 900;
      margin-bottom: 6px;
    }

    .why-list {
      display: grid;
      gap: 8px;
    }

    .why-item {
      display: flex;
      gap: 10px;
      align-items: start;
      color: var(--text);
      line-height: 1.5;
      font-size: 14px;
    }

    .check {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      font-weight: 900;
      color: var(--brand);
      background: rgba(31, 111, 235, 0.10);
      flex: 0 0 22px;
    }

    .keyword-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }

    .chip {
      padding: 8px 12px;
      border-radius: 999px;
      background: #f3f8ff;
      border: 1px solid #dbeafe;
      color: var(--brand);
      font-size: 12px;
      font-weight: 700;
    }

    .empty {
      padding: 24px;
      text-align: center;
      color: var(--muted);
      border: 1px dashed var(--border);
      border-radius: 18px;
      background: #fbfdff;
    }

    .explorer-layout {
      display: grid;
      grid-template-columns: 0.88fr 1.12fr;
      gap: 18px;
      align-items: start;
    }

    .list-card,
    .detail-card {
      padding: 18px;
      min-height: 560px;
    }

    .toolbar {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 14px;
    }

    .list-scroll {
      max-height: 760px;
      overflow: auto;
      padding-right: 6px;
    }

    .list-item {
      padding: 16px;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: #fbfdff;
      margin-bottom: 10px;
      cursor: pointer;
      transition: 0.2s ease;
    }

    .list-item:hover,
    .list-item.active {
      border-color: rgba(31, 111, 235, 0.24);
      box-shadow: 0 10px 24px rgba(31, 111, 235, 0.08);
      background: #f8fbff;
    }

    .list-item h4 {
      margin: 0 0 6px 0;
      font-size: 16px;
      line-height: 1.35;
    }

    .list-meta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }

    .detail-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }

    .detail-head h2 {
      margin: 0 0 6px 0;
      font-size: 24px;
      line-height: 1.25;
    }

    .detail-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-top: 14px;
    }

    .section {
      margin-top: 16px;
    }

    .section h3 {
      margin: 0 0 10px 0;
      font-size: 16px;
    }

    .advice-box,
    .service-box {
      padding: 16px;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: #fbfdff;
      margin-top: 14px;
    }

    .service-form {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 12px;
    }

    .service-form .full {
      grid-column: 1 / -1;
    }

    .notice {
      color: var(--muted);
      font-size: 12px;
      margin-top: 8px;
      line-height: 1.5;
    }

    @media (max-width: 1180px) {
      .page-layout,
      .explorer-layout,
      .two-col,
      .detail-grid,
      .toolbar,
      .summary-grid {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 760px) {
      .shell {
        padding: 14px;
      }

      .hero h1 {
        font-size: 32px;
      }

      .form-grid {
        grid-template-columns: 1fr;
      }

      .tender-top {
        flex-direction: column;
      }

      .score-number,
      .score-caption {
        text-align: left;
      }

      .service-form {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="topbar">
      <div class="brand">
        <img src="https://static.wixstatic.com/media/7193cf_13ca777ff7cc4c79a68caa1b2024f707~mv2.png" alt="Sawbona logo">
        <div>
          <div class="brand-title">Sawbona | TenderAI</div>
          <div class="brand-sub">Procurement intelligence, tender guidance, and delivery support</div>
        </div>
      </div>
      <div class="status-pill">Live public tender analysis engine</div>
    </div>

    <div class="hero card">
      <h1>Your AI procurement co-pilot for public tenders.</h1>
      <p>
        Upload a supplier profile, have TenderAI interpret your capabilities, match them against live public opportunities,
        estimate likely project size and execution investment, and guide you on how to improve your chances of winning.
      </p>
    </div>

    <div class="tabs">
      <button class="tab-btn active" data-page="assistantPage">AI Assistant</button>
      <button class="tab-btn" data-page="explorerPage">Tender Explorer</button>
    </div>

    <div id="assistantPage" class="page active">
      <div class="page-layout">
        <div class="assistant-column">
          <div class="chat-panel card">
            <div class="chat-header">
              <div>
                <div class="chat-title">TenderAI Assistant</div>
                <div class="chat-sub">Interact with the engine as if you were briefing an AI analyst.</div>
              </div>
            </div>

            <div class="chat-thread">
              <div class="message ai">
                <div class="avatar ai">AI</div>
                <div class="bubble ai">
                  <h3>Tell me about your business.</h3>
                  <p>Upload a supplier profile PDF or paste business capability text. I will read your profile, scan public tenders, estimate opportunity size, and rank the best-fit contracts for you.</p>
                </div>
              </div>

              <div class="message user">
                <div class="avatar user">U</div>
                <div class="bubble user">
                  <div class="form-grid">
                    <div class="field">
                      <label for="profile_pdf">Supplier profile PDF</label>
                      <input type="file" id="profile_pdf" name="profile_pdf" accept=".pdf">
                      <div class="hint">Best inputs: CSD summary, capability statement, or company profile PDF.</div>
                    </div>

                    <div class="field">
                      <label for="profile_text">Or paste company profile text</label>
                      <textarea id="profile_text" name="profile_text" placeholder="Use this only if you are not uploading a PDF."></textarea>
                    </div>

                    <div class="field">
                      <label for="date_from">From date</label>
                      <input type="date" id="date_from" value="2026-01-01">
                    </div>

                    <div class="field">
                      <label for="date_to">To date</label>
                      <input type="date" id="date_to" value="2026-03-17">
                    </div>

                    <div class="field">
                      <label for="page_number">Page number</label>
                      <input type="number" id="page_number" value="1" min="1">
                    </div>

                    <div class="field">
                      <label for="page_size">Page size</label>
                      <input type="number" id="page_size" value="10" min="1" max="100">
                    </div>
                  </div>

                  <div class="actions">
                    <button class="btn btn-primary" id="runScanBtn" type="button">Analyze opportunities</button>
                    <button class="btn btn-secondary" id="clearBtn" type="button">Clear</button>
                  </div>
                </div>
              </div>

              <div class="message ai">
                <div class="avatar ai">AI</div>
                <div class="bubble ai">
                  <div class="loading-wrap" id="loadingWrap">
                    <div class="loading-row">
                      <div class="orb-loader"></div>
                      <div>
                        <div class="loading-title">TenderAI is analyzing your opportunity landscape.</div>
                        <div class="loading-step" id="loadingStepText">Reading supplier profile and scanning public tenders...</div>
                      </div>
                    </div>
                  </div>

                  <div id="assistantResponse">
                    <p>Once you run a scan, I will show you ranked opportunities, investment estimates, and why each tender matches your business.</p>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="card results-panel">
            <div class="results-head">
              <div>
                <h2>Ranked opportunity matches</h2>
                <div class="results-sub">Best-fit tenders based on your submitted profile.</div>
              </div>
            </div>

            <div class="summary-grid" id="summaryGrid" style="display:none;">
              <div class="card metric">
                <div class="metric-label">Returned tenders</div>
                <div class="metric-value" id="mTotal">0</div>
                <div class="metric-sub">Scored opportunities</div>
              </div>
              <div class="card metric">
                <div class="metric-label">High fit</div>
                <div class="metric-value" id="mHigh">0</div>
                <div class="metric-sub">Priority targets</div>
              </div>
              <div class="card metric">
                <div class="metric-label">Medium fit</div>
                <div class="metric-value" id="mMedium">0</div>
                <div class="metric-sub">Worth pursuing</div>
              </div>
              <div class="card metric">
                <div class="metric-label">Low fit</div>
                <div class="metric-value" id="mLow">0</div>
                <div class="metric-sub">Monitor only</div>
              </div>
            </div>

            <div id="resultsList" class="tender-list">
              <div class="empty">Your ranked matches will appear here after a scan.</div>
            </div>
          </div>
        </div>

        <div class="card results-panel">
          <div class="results-head">
            <div>
              <h2>AI interpretation</h2>
              <div class="results-sub">A concise summary of what TenderAI understood from your profile.</div>
            </div>
          </div>
          <div id="insightPanel">
            <div class="empty">Run a scan to see profile understanding, extracted capability signals, and opportunity interpretation.</div>
          </div>
        </div>
      </div>
    </div>

    <div id="explorerPage" class="page">
      <div class="explorer-layout">
        <div class="card list-card">
          <div class="results-head">
            <div>
              <h2>All tenders</h2>
              <div class="results-sub">Browse live tenders manually and inspect each opportunity in detail.</div>
            </div>
          </div>

          <div class="toolbar">
            <div>
              <label for="explorer_date_from">From date</label>
              <input type="date" id="explorer_date_from" value="2026-01-01">
            </div>
            <div>
              <label for="explorer_date_to">To date</label>
              <input type="date" id="explorer_date_to" value="2026-03-17">
            </div>
            <div>
              <label for="explorer_page_number">Page number</label>
              <input type="number" id="explorer_page_number" value="1" min="1">
            </div>
            <div>
              <label for="explorer_page_size">Page size</label>
              <input type="number" id="explorer_page_size" value="20" min="1" max="100">
            </div>
          </div>

          <div class="actions" style="margin-bottom: 12px;">
            <button class="btn btn-primary" id="loadTendersBtn" type="button">Load tenders</button>
          </div>

          <div id="explorerLoading" class="loading-wrap" style="margin-bottom: 12px;">
            <div class="loading-row">
              <div class="orb-loader"></div>
              <div>
                <div class="loading-title">Fetching live tenders...</div>
                <div class="loading-step">Retrieving open procurement opportunities for manual review.</div>
              </div>
            </div>
          </div>

          <div id="explorerList" class="list-scroll">
            <div class="empty">Load tenders to browse the market manually.</div>
          </div>
        </div>

        <div class="card detail-card">
          <div id="detailPanel">
            <div class="empty">Select a tender from the list to view its overview, get AI advice, and request Sawbona logistics support.</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    let latestScan = null;
    let explorerTenders = [];
    let selectedTender = null;

    const loadingMessages = [
      "Reading supplier profile and extracting capability signals...",
      "Scanning live tender releases from the public procurement source...",
      "Scoring opportunity fit and estimating project size...",
      "Preparing advice and delivery insights..."
    ];

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function safeText(value, fallback = "N/A") {
      return value === null || value === undefined || value === "" ? fallback : value;
    }

    function formatCount(value) {
      return Number(value || 0).toLocaleString();
    }

    function bandClass(band) {
      if (band === "High fit") return "band high";
      if (band === "Medium fit") return "band medium";
      return "band low";
    }

    function whyItemsForTender(t) {
      const items = [];
      if (t.matched_keywords && t.matched_keywords.length) {
        items.push("Matched capability keywords: " + t.matched_keywords.join(", "));
      }
      if (t.category && ["works", "services"].includes(String(t.category).toLowerCase())) {
        items.push("Tender category aligns with operational delivery work.");
      }
      if (t.estimation_reason) {
        items.push("Investment signal: " + t.estimation_reason);
      }
      if (!items.length) {
        items.push("TenderAI found limited fit signals for this opportunity.");
      }
      return items;
    }

    function renderTenderCard(t) {
      const whyItems = whyItemsForTender(t).map(item => `
        <div class="why-item">
          <div class="check">✓</div>
          <div>${escapeHtml(item)}</div>
        </div>
      `).join("");

      return `
        <div class="card tender-card">
          <div class="tender-top">
            <div>
              <h3 class="tender-title">${escapeHtml(safeText(t.title, "Untitled tender"))}</h3>
              <div class="tender-meta">
                ${escapeHtml(safeText(t.buyer))} •
                ${escapeHtml(safeText(t.category))} •
                closes ${escapeHtml(safeText(t.close_date))}
              </div>
            </div>
            <div>
              <div class="${bandClass(t.fit_band)}">${escapeHtml(safeText(t.fit_band))}</div>
              <div class="score-number">${escapeHtml(safeText(t.fit_score, 0))}/100</div>
              <div class="score-caption">Tender fit score</div>
            </div>
          </div>

          <div class="mini" style="margin-bottom: 14px;">
            <h4>Description of work</h4>
            <div>${escapeHtml(safeText(t.description, "No description provided."))}</div>
          </div>

          <div class="two-col">
            <div class="mini">
              <h4>Why this matches</h4>
              <div class="why-list">${whyItems}</div>
              <div class="keyword-list">
                ${(t.matched_keywords || []).map(k => `<span class="chip">${escapeHtml(k)}</span>`).join("")}
              </div>
            </div>

            <div class="mini">
              <h4>Project value and delivery investment</h4>
              <div class="value-big">${escapeHtml(safeText(t.value_display))}</div>
              <div class="tender-meta">
                Source: ${escapeHtml(safeText(t.value_source))} •
                Confidence: ${escapeHtml(safeText(t.estimation_confidence))}
              </div>
              <div style="margin-top: 10px;">${escapeHtml(safeText(t.estimation_reason))}</div>
              <div style="margin-top: 14px; font-weight: 700;">
                Estimated delivery investment: ${escapeHtml(safeText(t.execution_investment_display))}
              </div>
              <div class="tender-meta">${escapeHtml(safeText(t.execution_investment_reason))}</div>
            </div>
          </div>
        </div>
      `;
    }

    function renderAssistantResults(data) {
      document.getElementById("summaryGrid").style.display = "grid";
      document.getElementById("mTotal").textContent = formatCount(data.summary.returned_tenders);
      document.getElementById("mHigh").textContent = formatCount(data.summary.high_fit);
      document.getElementById("mMedium").textContent = formatCount(data.summary.medium_fit);
      document.getElementById("mLow").textContent = formatCount(data.summary.low_fit);

      const list = document.getElementById("resultsList");
      if (!data.tenders || !data.tenders.length) {
        list.innerHTML = '<div class="empty">No tenders found for this scan.</div>';
      } else {
        list.innerHTML = data.tenders.map(renderTenderCard).join("");
      }

      const insightPanel = document.getElementById("insightPanel");
      insightPanel.innerHTML = `
        <div class="message ai" style="margin-bottom: 14px;">
          <div class="avatar ai">AI</div>
          <div class="bubble ai" style="max-width: 100%;">
            <h3>Here is what I understood about your business.</h3>
            <p>I extracted profile signals and used them to rank the most relevant tenders from the selected date range.</p>
            <div class="keyword-list">
              ${(data.profile_keywords || []).map(k => `<span class="chip">${escapeHtml(k)}</span>`).join("")}
            </div>
          </div>
        </div>

        <div class="message ai">
          <div class="avatar ai">AI</div>
          <div class="bubble ai" style="max-width: 100%;">
            <h4>Profile preview</h4>
            <p>${escapeHtml(data.profile_text_preview || "No preview available.")}</p>
            <h4 style="margin-top: 16px;">Request scope</h4>
            <p>Date range: ${escapeHtml(data.request_used.dateFrom)} to ${escapeHtml(data.request_used.dateTo)}</p>
            <p>Scan size: page ${escapeHtml(data.request_used.PageNumber)} / ${escapeHtml(data.request_used.PageSize)} tenders requested</p>
          </div>
        </div>
      `;
    }

    function setLoadingState(show) {
      document.getElementById("loadingWrap").style.display = show ? "block" : "none";
    }

    function startLoadingMessages() {
      let i = 0;
      document.getElementById("loadingStepText").textContent = loadingMessages[0];
      window.loadingTicker = setInterval(() => {
        i = (i + 1) % loadingMessages.length;
        document.getElementById("loadingStepText").textContent = loadingMessages[i];
      }, 1300);
    }

    function stopLoadingMessages() {
      clearInterval(window.loadingTicker);
    }

    async function runScan() {
      setLoadingState(true);
      startLoadingMessages();

      const pdfFile = document.getElementById("profile_pdf").files[0];
      const profileText = document.getElementById("profile_text").value.trim();

      try {
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
          document.getElementById("assistantResponse").innerHTML = `<div class="empty">Error: ${escapeHtml(data.error || "Unknown error")}</div>`;
          document.getElementById("resultsList").innerHTML = `<div class="empty">No results available.</div>`;
          return;
        }

        latestScan = data;
        renderAssistantResults(data);
        document.getElementById("assistantResponse").innerHTML = `
          <p>I completed the scan. I found <strong>${escapeHtml(data.summary.returned_tenders)}</strong> ranked opportunities and classified
          <strong>${escapeHtml(data.summary.high_fit)}</strong> as high-fit matches.</p>
          <p>You can also open the <strong>Tender Explorer</strong> tab to inspect the broader market manually.</p>
        `;
      } catch (err) {
        document.getElementById("assistantResponse").innerHTML = `<div class="empty">Error: ${escapeHtml(err.message)}</div>`;
      } finally {
        stopLoadingMessages();
        setLoadingState(false);
      }
    }

    function clearScanForm() {
      document.getElementById("profile_pdf").value = "";
      document.getElementById("profile_text").value = "";
      document.getElementById("page_number").value = "1";
      document.getElementById("page_size").value = "10";
    }

    async function loadExplorerTenders() {
      const loading = document.getElementById("explorerLoading");
      const list = document.getElementById("explorerList");
      loading.style.display = "block";
      list.innerHTML = "";

      try {
        const qs = new URLSearchParams({
          date_from: document.getElementById("explorer_date_from").value,
          date_to: document.getElementById("explorer_date_to").value,
          page_number: document.getElementById("explorer_page_number").value,
          page_size: document.getElementById("explorer_page_size").value
        });

        const response = await fetch(`/tenders?${qs.toString()}`);
        const data = await response.json();

        if (data.status !== "ok") {
          list.innerHTML = `<div class="empty">Error: ${escapeHtml(data.error || "Unable to load tenders.")}</div>`;
          return;
        }

        explorerTenders = data.tenders || [];

        if (!explorerTenders.length) {
          list.innerHTML = `<div class="empty">No tenders found for this range.</div>`;
          return;
        }

        list.innerHTML = explorerTenders.map((t, idx) => `
          <div class="list-item" data-index="${idx}">
            <h4>${escapeHtml(safeText(t.title, "Untitled tender"))}</h4>
            <div class="list-meta">
              ${escapeHtml(safeText(t.buyer))}<br>
              ${escapeHtml(safeText(t.category))} • closes ${escapeHtml(safeText(t.close_date))}<br>
              ${escapeHtml(safeText(t.value_display))}
            </div>
          </div>
        `).join("");

        document.querySelectorAll(".list-item").forEach(item => {
          item.addEventListener("click", () => {
            document.querySelectorAll(".list-item").forEach(x => x.classList.remove("active"));
            item.classList.add("active");
            const idx = Number(item.getAttribute("data-index"));
            selectTender(explorerTenders[idx]);
          });
        });

        selectTender(explorerTenders[0]);
        document.querySelector('.list-item[data-index="0"]')?.classList.add("active");
      } catch (err) {
        list.innerHTML = `<div class="empty">Error: ${escapeHtml(err.message)}</div>`;
      } finally {
        loading.style.display = "none";
      }
    }

    function selectTender(tender) {
      selectedTender = tender;
      renderTenderDetail(tender);
    }

    async function getTenderAdvice() {
      if (!selectedTender) return;

      const response = await fetch("/advise", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tender: selectedTender,
          profile_keywords: latestScan ? latestScan.profile_keywords : [],
          profile_text: latestScan ? latestScan.profile_text_preview : ""
        })
      });

      const data = await response.json();
      const box = document.getElementById("adviceResult");

      if (data.status !== "ok") {
        box.innerHTML = `<div class="empty">Error: ${escapeHtml(data.error || "Unable to generate advice.")}</div>`;
        return;
      }

      box.innerHTML = `
        <div class="advice-box">
          <h3 style="margin-top:0;">AI advice to improve your score</h3>
          <div class="why-list">
            ${data.advice.map(item => `
              <div class="why-item">
                <div class="check">✓</div>
                <div>${escapeHtml(item)}</div>
              </div>
            `).join("")}
          </div>
          <div style="margin-top: 14px;">
            <strong>Recommended supporting material</strong>
            <div class="keyword-list">
              ${data.recommended_documents.map(item => `<span class="chip">${escapeHtml(item)}</span>`).join("")}
            </div>
          </div>
        </div>
      `;
    }

    function renderTenderDetail(t) {
      document.getElementById("detailPanel").innerHTML = `
        <div class="detail-head">
          <div>
            <h2>${escapeHtml(safeText(t.title, "Untitled tender"))}</h2>
            <div class="tender-meta">
              ${escapeHtml(safeText(t.buyer))} •
              ${escapeHtml(safeText(t.category))} •
              closes ${escapeHtml(safeText(t.close_date))}
            </div>
          </div>
          <div>
            <div class="${bandClass(t.fit_band || "Low fit")}">${escapeHtml(safeText(t.fit_band || "Unscored"))}</div>
          </div>
        </div>

        <div class="section">
          <h3>Tender overview</h3>
          <div class="mini">${escapeHtml(safeText(t.description, "No description supplied."))}</div>
        </div>

        <div class="detail-grid">
          <div class="mini">
            <h4>Estimated tender value</h4>
            <div class="value-big">${escapeHtml(safeText(t.value_display))}</div>
            <div class="tender-meta">
              Source: ${escapeHtml(safeText(t.value_source))} •
              Confidence: ${escapeHtml(safeText(t.estimation_confidence))}
            </div>
            <div style="margin-top: 10px;">${escapeHtml(safeText(t.estimation_reason))}</div>
          </div>

          <div class="mini">
            <h4>Expected delivery investment</h4>
            <div class="value-big">${escapeHtml(safeText(t.execution_investment_display))}</div>
            <div class="tender-meta">${escapeHtml(safeText(t.execution_investment_reason))}</div>
          </div>
        </div>

        <div class="section">
          <h3>AI guidance</h3>
          <div class="actions">
            <button class="btn btn-primary" id="adviceBtn" type="button">Get AI advice on how to score better</button>
          </div>
          <div id="adviceResult" style="margin-top: 12px;">
            <div class="empty">Ask TenderAI for tailored advice for this selected tender.</div>
          </div>
        </div>

        <div class="section">
          <h3>Request Sawbona logistics services</h3>
          <div class="service-box">
            <div class="tender-meta">
              Use this to request bid logistics support, document preparation coordination, supplier mobilisation, project readiness planning, and tender response support.
            </div>

            <div class="service-form">
              <div>
                <label for="service_name">Your name</label>
                <input id="service_name" type="text" placeholder="Full name">
              </div>
              <div>
                <label for="service_email">Email</label>
                <input id="service_email" type="email" placeholder="you@example.com">
              </div>
              <div>
                <label for="service_company">Company</label>
                <input id="service_company" type="text" placeholder="Company name">
              </div>
              <div>
                <label for="service_phone">Phone</label>
                <input id="service_phone" type="text" placeholder="+27 ...">
              </div>
              <div class="full">
                <label for="service_notes">Support required</label>
                <textarea id="service_notes" placeholder="Describe the support you want from Sawbona for this tender."></textarea>
              </div>
            </div>

            <div class="actions">
              <button class="btn btn-primary" id="requestServiceBtn" type="button">Request logistics support</button>
            </div>
            <div id="serviceResult" class="notice"></div>
          </div>
        </div>
      `;

      document.getElementById("adviceBtn").addEventListener("click", getTenderAdvice);
      document.getElementById("requestServiceBtn").addEventListener("click", submitServiceRequest);
    }

    async function submitServiceRequest() {
      if (!selectedTender) return;

      const payload = {
        tender: selectedTender,
        name: document.getElementById("service_name").value,
        email: document.getElementById("service_email").value,
        company: document.getElementById("service_company").value,
        phone: document.getElementById("service_phone").value,
        notes: document.getElementById("service_notes").value
      };

      const response = await fetch("/service-request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      const data = await response.json();
      const result = document.getElementById("serviceResult");

      if (data.status !== "ok") {
        result.textContent = "Unable to submit request.";
        return;
      }

      result.textContent = "Request captured. Reference: " + data.reference + ". We can later route this to your CRM or support inbox.";
    }

    document.querySelectorAll(".tab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));

        btn.classList.add("active");
        document.getElementById(btn.getAttribute("data-page")).classList.add("active");
      });
    });

    document.getElementById("runScanBtn").addEventListener("click", runScan);
    document.getElementById("clearBtn").addEventListener("click", clearScanForm);
    document.getElementById("loadTendersBtn").addEventListener("click", loadExplorerTenders);
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

    intent_keywords = ["installation", "maintenance", "repair", "construction", "electrical", "generator"]
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


def estimate_execution_investment(title, description, category, estimated_low, estimated_high):
    text = f"{title} {description}".lower()

    ratio_low = 0.35
    ratio_high = 0.70
    reason = "Typical execution readiness, procurement, mobilisation, and delivery costs were applied."

    if "generator" in text:
        ratio_low = 0.55
        ratio_high = 0.82
        reason = "Generator supply and installation usually require significant equipment, transport, and technical delivery spend."
    elif any(k in text for k in ["construction", "building", "infrastructure"]):
        ratio_low = 0.60
        ratio_high = 0.85
        reason = "Construction and infrastructure work generally requires substantial materials, labour, and site mobilisation."
    elif any(k in text for k in ["maintenance", "repair", "servicing"]):
        ratio_low = 0.40
        ratio_high = 0.70
        reason = "Maintenance and repair contracts usually carry labour, tools, materials, and travel costs."
    elif any(k in text for k in ["truck", "vehicle", "fire truck"]):
        ratio_low = 0.70
        ratio_high = 0.92
        reason = "Vehicle and specialized equipment tenders often require high capital outlay before delivery."
    elif any(k in text for k in ["server", "hardware", "storage", "backup appliance"]):
        ratio_low = 0.65
        ratio_high = 0.88
        reason = "Hardware and IT supply contracts typically need significant procurement capital and logistics."
    elif category and category.lower() == "services":
        ratio_low = 0.30
        ratio_high = 0.60
        reason = "Service tenders usually need less equipment spend, but still require staffing, compliance, and delivery overhead."

    low = round(estimated_low * ratio_low, 0)
    high = round(estimated_high * ratio_high, 0)
    mid = round((low + high) / 2, 0)

    return {
      "execution_investment_low": low,
      "execution_investment_high": high,
      "execution_investment_mid": mid,
      "execution_investment_display": f"R{low:,.0f} - R{high:,.0f}",
      "execution_investment_reason": reason
    }


def enrich_tender(item, profile_keywords=None):
    tender = item.get("tender", {}) if isinstance(item, dict) else {}
    buyer = item.get("buyer", {}) if isinstance(item, dict) else {}
    tender_period = tender.get("tenderPeriod", {}) if isinstance(tender, dict) else {}
    value = tender.get("value", {}) if isinstance(tender, dict) else {}

    description = tender.get("description", "") or ""
    title = tender.get("title", "") or ""
    buyer_name = buyer.get("name", "") or ""
    category = tender.get("mainProcurementCategory", "") or ""
    combined_text = f"{title} {description} {buyer_name} {category}"

    if profile_keywords is None:
        fit_score = 0
        fit_band = "Low fit"
        matched_keywords = []
    else:
        fit_score, fit_band, matched_keywords = score_tender(profile_keywords, combined_text, category)

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

    execution = estimate_execution_investment(
        title=title,
        description=description,
        category=category,
        estimated_low=estimated_value_low,
        estimated_high=estimated_value_high
    )

    return {
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
        "matched_keywords": matched_keywords,
        "execution_investment_low": execution["execution_investment_low"],
        "execution_investment_high": execution["execution_investment_high"],
        "execution_investment_mid": execution["execution_investment_mid"],
        "execution_investment_display": execution["execution_investment_display"],
        "execution_investment_reason": execution["execution_investment_reason"]
    }


def fetch_tenders(date_from, date_to, page_number, page_size):
    url = "https://ocds-api.etenders.gov.za/api/OCDSReleases"
    params = {
        "PageNumber": page_number,
        "PageSize": page_size,
        "dateFrom": date_from,
        "dateTo": date_to
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    releases = extract_releases(data)
    return releases, params


@app.post("/score")
def score():
    profile_text, profile_source = extract_profile_text()

    date_from = get_request_value("date_from", "2026-01-01")
    date_to = get_request_value("date_to", "2026-03-17")
    page_number = int(get_request_value("page_number", 1))
    page_size = int(get_request_value("page_size", 10))

    profile_keywords = tokenize(profile_text)[:25]

    try:
        releases, params = fetch_tenders(date_from, date_to, page_number, page_size)
        tenders = [enrich_tender(item, profile_keywords=profile_keywords) for item in releases]
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


@app.get("/tenders")
def tenders():
    try:
        date_from = request.args.get("date_from", "2026-01-01")
        date_to = request.args.get("date_to", "2026-03-17")
        page_number = int(request.args.get("page_number", 1))
        page_size = int(request.args.get("page_size", 20))

        releases, params = fetch_tenders(date_from, date_to, page_number, page_size)
        tender_rows = [enrich_tender(item, profile_keywords=None) for item in releases]

        return jsonify({
            "status": "ok",
            "request_used": params,
            "summary": {
                "returned_tenders": len(tender_rows)
            },
            "tenders": tender_rows
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500


@app.post("/advise")
def advise():
    try:
        body = request.get_json(silent=True) or {}
        tender = body.get("tender", {})
        profile_keywords = body.get("profile_keywords", []) or []
        profile_text = body.get("profile_text", "") or ""

        title = str(tender.get("title", ""))
        description = str(tender.get("description", ""))
        category = str(tender.get("category", ""))
        matched_keywords = tender.get("matched_keywords", []) or []

        advice = []
        recommended_documents = []

        if not matched_keywords:
            advice.append("Strengthen your bid with a sharper capability statement that mirrors the tender language more directly.")
        else:
            advice.append("Mirror the strongest matched keywords in your executive summary, methodology, and pricing narrative.")

        if "generator" in f"{title} {description}".lower():
            advice.append("Show specific generator installation references, electrical compliance credentials, and technical maintenance capability.")
            recommended_documents.extend([
                "Electrical compliance certificate",
                "Generator installation references",
                "Project methodology"
            ])

        if category.lower() == "works":
            advice.append("Include site execution methodology, safety planning, project supervision structure, and proof of delivery capacity.")
            recommended_documents.extend([
                "Construction methodology",
                "Health and safety file",
                "Site mobilisation plan"
            ])

        if category.lower() == "services":
            advice.append("Demonstrate staffing capacity, turnaround times, service levels, and geographic reach.")
            recommended_documents.extend([
                "Service level plan",
                "Team CVs",
                "Operational response plan"
            ])

        if not profile_text:
            advice.append("Upload a richer supplier profile or capability statement so the AI can compare more precise business signals.")
        else:
            advice.append("Use your profile strengths in a tailored cover letter that explains exactly why your business is fit for this tender.")

        advice.append("Validate delivery funding early, because your estimated execution investment suggests material upfront spend before payment is received.")

        if not recommended_documents:
            recommended_documents = [
                "Capability statement",
                "Client references",
                "Execution methodology",
                "Compliance pack"
            ]

        recommended_documents = list(dict.fromkeys(recommended_documents))

        return jsonify({
            "status": "ok",
            "advice": advice,
            "recommended_documents": recommended_documents
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500


@app.post("/service-request")
def service_request():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "Unknown")
    company = body.get("company", "Unknown")
    tender = body.get("tender", {}) or {}

    reference = f"SAW-{abs(hash((name, company, tender.get('ocid', 'NA')))) % 1000000:06d}"

    return jsonify({
        "status": "ok",
        "reference": reference
    })
