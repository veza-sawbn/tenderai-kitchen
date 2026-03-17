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
  <title>TenderAI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #07110b;
      --bg-2: #0b1610;
      --panel: rgba(10, 19, 14, 0.72);
      --panel-solid: #0e1913;
      --border: rgba(126, 211, 161, 0.11);
      --text: #edf7f0;
      --muted: #9fb5a8;
      --accent: #73f0a6;
      --accent-2: #43c97b;
      --accent-3: #b7ffd2;
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.34);
      --radius: 24px;
      --red: #f08f8f;
      --amber: #e8ca7d;
    }

    * {
      box-sizing: border-box;
    }

    html {
      scroll-behavior: smooth;
    }

    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        linear-gradient(180deg, #06100a 0%, #09150f 50%, #08110d 100%);
      min-height: 100vh;
      overflow-x: hidden;
    }

    body::before,
    body::after {
      content: "";
      position: fixed;
      inset: auto;
      width: 34rem;
      height: 34rem;
      border-radius: 50%;
      filter: blur(110px);
      opacity: 0.26;
      pointer-events: none;
      z-index: 0;
    }

    body::before {
      top: -8rem;
      left: -8rem;
      background: radial-gradient(circle, rgba(67, 201, 123, 0.85) 0%, rgba(67, 201, 123, 0) 68%);
      animation: driftA 18s ease-in-out infinite alternate;
    }

    body::after {
      right: -10rem;
      top: 10rem;
      background: radial-gradient(circle, rgba(115, 240, 166, 0.6) 0%, rgba(115, 240, 166, 0) 70%);
      animation: driftB 24s ease-in-out infinite alternate;
    }

    @keyframes driftA {
      0% { transform: translate3d(0, 0, 0) scale(1); }
      100% { transform: translate3d(5rem, 6rem, 0) scale(1.12); }
    }

    @keyframes driftB {
      0% { transform: translate3d(0, 0, 0) scale(1); }
      100% { transform: translate3d(-6rem, 8rem, 0) scale(1.08); }
    }

    .shell {
      position: relative;
      z-index: 1;
      max-width: 1380px;
      margin: 0 auto;
      padding: 20px;
    }

    .topbar {
      position: sticky;
      top: 16px;
      z-index: 20;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 14px 18px;
      margin-bottom: 18px;
      background: rgba(8, 16, 12, 0.72);
      backdrop-filter: blur(18px);
      border: 1px solid rgba(115, 240, 166, 0.08);
      border-radius: 20px;
      box-shadow: var(--shadow);
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
      filter: drop-shadow(0 0 22px rgba(115, 240, 166, 0.2));
    }

    .brand-title {
      font-size: 20px;
      font-weight: 800;
      letter-spacing: 0.01em;
    }

    .brand-sub {
      color: var(--muted);
      font-size: 13px;
      margin-top: 2px;
    }

    .nav-links {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }

    .nav-link {
      color: var(--muted);
      text-decoration: none;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.04);
      background: rgba(255,255,255,0.02);
      font-size: 13px;
      font-weight: 700;
      transition: 0.2s ease;
    }

    .nav-link:hover {
      color: var(--accent-3);
      border-color: rgba(115, 240, 166, 0.16);
    }

    .hero {
      position: relative;
      overflow: hidden;
      min-height: 74vh;
      padding: 3.8rem 2rem 2.8rem 2rem;
      border-radius: 32px;
      background:
        linear-gradient(180deg, rgba(10, 21, 15, 0.72) 0%, rgba(9, 16, 12, 0.82) 100%),
        radial-gradient(circle at 20% 20%, rgba(115, 240, 166, 0.06), transparent 28%),
        radial-gradient(circle at 80% 10%, rgba(67, 201, 123, 0.07), transparent 24%),
        linear-gradient(135deg, #08120c 0%, #0b1711 55%, #08120c 100%);
      border: 1px solid rgba(115, 240, 166, 0.08);
      box-shadow: var(--shadow);
      margin-bottom: 22px;
    }

    .hero::before,
    .hero::after {
      content: "";
      position: absolute;
      inset: auto;
      border-radius: 50%;
      filter: blur(80px);
      opacity: 0.22;
      pointer-events: none;
    }

    .hero::before {
      width: 22rem;
      height: 22rem;
      left: 6%;
      top: 5%;
      background: radial-gradient(circle, rgba(115, 240, 166, 0.75) 0%, rgba(115, 240, 166, 0) 70%);
      animation: heroGlowA 16s ease-in-out infinite alternate;
    }

    .hero::after {
      width: 26rem;
      height: 26rem;
      right: 2%;
      bottom: -5%;
      background: radial-gradient(circle, rgba(67, 201, 123, 0.62) 0%, rgba(67, 201, 123, 0) 72%);
      animation: heroGlowB 20s ease-in-out infinite alternate;
    }

    @keyframes heroGlowA {
      0% { transform: translate3d(0, 0, 0) scale(1); }
      100% { transform: translate3d(4rem, 4rem, 0) scale(1.15); }
    }

    @keyframes heroGlowB {
      0% { transform: translate3d(0, 0, 0) scale(1); }
      100% { transform: translate3d(-4rem, -3rem, 0) scale(1.1); }
    }

    .grid-lines {
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(rgba(115, 240, 166, 0.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(115, 240, 166, 0.035) 1px, transparent 1px);
      background-size: 42px 42px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.8), transparent 85%);
      pointer-events: none;
    }

    .hero-content {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 2rem;
      align-items: end;
      height: 100%;
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--accent-3);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      margin-bottom: 16px;
    }

    .hero h1 {
      margin: 0 0 16px 0;
      font-size: clamp(3rem, 6vw, 5.2rem);
      line-height: 0.96;
      letter-spacing: -0.045em;
      max-width: 760px;
    }

    .hero p {
      margin: 0;
      max-width: 720px;
      color: var(--muted);
      line-height: 1.75;
      font-size: 17px;
    }

    .hero-actions {
      margin-top: 28px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }

    .btn {
      border: none;
      cursor: pointer;
      font-weight: 800;
      border-radius: 16px;
      padding: 14px 18px;
      font-family: inherit;
      transition: 0.2s ease;
    }

    .btn:hover {
      transform: translateY(-1px);
    }

    .btn-primary {
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #041008;
      box-shadow: 0 14px 32px rgba(67, 201, 123, 0.22);
    }

    .btn-secondary {
      color: var(--text);
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(115, 240, 166, 0.08);
    }

    .hero-stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      align-self: center;
    }

    .hero-stat {
      padding: 18px;
      border-radius: 20px;
      background: rgba(255,255,255,0.035);
      border: 1px solid rgba(115, 240, 166, 0.08);
      backdrop-filter: blur(12px);
    }

    .hero-stat-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 8px;
    }

    .hero-stat-value {
      font-size: 30px;
      font-weight: 900;
      margin-bottom: 4px;
    }

    .hero-stat-sub {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .section {
      margin-bottom: 20px;
    }

    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 12px;
      margin-bottom: 14px;
    }

    .section-head h2 {
      margin: 0;
      font-size: 28px;
      letter-spacing: -0.02em;
    }

    .section-head p {
      margin: 6px 0 0 0;
      color: var(--muted);
      font-size: 14px;
    }

    .features-strip {
      position: relative;
      overflow: hidden;
      border-radius: 28px;
      padding: 18px 0 10px 0;
      margin-bottom: 22px;
    }

    .carousel-track {
      display: flex;
      gap: 18px;
      width: max-content;
      animation: scrollTrack 28s linear infinite;
      padding: 0 6px;
    }

    .features-strip:hover .carousel-track {
      animation-play-state: paused;
    }

    @keyframes scrollTrack {
      0% { transform: translateX(0); }
      100% { transform: translateX(-50%); }
    }

    .feature-card {
      flex: 0 0 360px;
      min-height: 260px;
      border-radius: 26px;
      overflow: hidden;
      position: relative;
      border: 1px solid rgba(115, 240, 166, 0.08);
      background: #0d1712;
      box-shadow: var(--shadow);
    }

    .feature-image {
      position: absolute;
      inset: 0;
      background-size: cover;
      background-position: center;
      transform: scale(1.02);
      filter: saturate(1.08) brightness(0.72);
    }

    .feature-overlay {
      position: absolute;
      inset: 0;
      background:
        linear-gradient(180deg, rgba(5, 10, 8, 0.12) 0%, rgba(7, 13, 10, 0.78) 72%, rgba(7, 13, 10, 0.96) 100%);
    }

    .feature-copy {
      position: absolute;
      inset: auto 0 0 0;
      padding: 24px;
      z-index: 1;
    }

    .feature-copy h3 {
      margin: 0 0 8px 0;
      font-size: 22px;
      letter-spacing: -0.02em;
    }

    .feature-copy p {
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }

    .assistant-layout {
      display: grid;
      grid-template-columns: 0.92fr 1.08fr;
      gap: 18px;
      align-items: start;
    }

    .card {
      padding: 20px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 26px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
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
      width: 16px;
      height: 16px;
      border-radius: 50%;
      flex: 0 0 16px;
      margin-top: 14px;
      background: rgba(115, 240, 166, 0.28);
      box-shadow: 0 0 14px rgba(115, 240, 166, 0.28);
    }

    .message.user .avatar {
      background: rgba(255, 255, 255, 0.12);
      box-shadow: none;
    }

    .message-bubble {
      width: calc(100% - 28px);
      padding: 18px;
      background: var(--panel-solid);
      border: 1px solid rgba(115, 240, 166, 0.08);
      border-radius: 22px;
    }

    .message-bubble h3,
    .message-bubble h4 {
      margin-top: 0;
    }

    .message-bubble p {
      margin: 0 0 10px 0;
      line-height: 1.68;
      color: var(--text);
    }

    .message-bubble p:last-child {
      margin-bottom: 0;
    }

    label {
      display: block;
      margin-bottom: 8px;
      font-size: 13px;
      color: var(--accent-3);
      font-weight: 700;
      letter-spacing: 0.02em;
    }

    input,
    textarea,
    button,
    select {
      width: 100%;
      border-radius: 16px;
      border: 1px solid rgba(115, 240, 166, 0.10);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      padding: 13px 14px;
      font-size: 14px;
      outline: none;
      transition: 0.2s ease;
      font-family: inherit;
    }

    input:focus,
    textarea:focus,
    select:focus {
      border-color: rgba(115, 240, 166, 0.28);
      box-shadow: 0 0 0 4px rgba(115, 240, 166, 0.08);
    }

    textarea {
      min-height: 120px;
      resize: vertical;
    }

    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }

    .field {
      margin-bottom: 14px;
    }

    .hint {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      margin-top: 8px;
    }

    .actions {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 14px;
      margin-bottom: 16px;
    }

    .metric {
      padding: 18px;
      border-radius: 20px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(115, 240, 166, 0.08);
    }

    .metric-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      margin-bottom: 8px;
    }

    .metric-value {
      font-size: 30px;
      font-weight: 900;
      margin-bottom: 4px;
    }

    .metric-sub {
      color: var(--muted);
      font-size: 13px;
    }

    .loading-overlay {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(4, 9, 7, 0.54);
      backdrop-filter: blur(10px);
      z-index: 60;
      padding: 20px;
    }

    .loading-modal {
      width: min(420px, 100%);
      padding: 28px;
      border-radius: 28px;
      background: rgba(10, 19, 14, 0.92);
      border: 1px solid rgba(115, 240, 166, 0.12);
      box-shadow: var(--shadow);
      text-align: center;
    }

    .loading-orb {
      width: 90px;
      height: 90px;
      margin: 0 auto 20px auto;
      border-radius: 50%;
      position: relative;
      border: 1px solid rgba(115, 240, 166, 0.10);
      background: radial-gradient(circle at center, rgba(115, 240, 166, 0.06), transparent 68%);
    }

    .loading-orb::before {
      content: "";
      position: absolute;
      inset: 10px;
      border-radius: 50%;
      border: 1px dashed rgba(115, 240, 166, 0.18);
    }

    .loading-orb::after {
      content: "";
      position: absolute;
      top: 6px;
      left: 50%;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: linear-gradient(135deg, var(--accent-3), var(--accent-2));
      transform: translateX(-50%);
      transform-origin: 0 39px;
      box-shadow: 0 0 24px rgba(115, 240, 166, 0.95);
      animation: orbit 1.1s linear infinite;
    }

    @keyframes orbit {
      from { transform: rotate(0deg) translateX(-50%); }
      to { transform: rotate(360deg) translateX(-50%); }
    }

    .loading-title {
      font-size: 20px;
      font-weight: 800;
      margin-bottom: 8px;
    }

    .loading-step {
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }

    .results-list,
    .tender-list {
      display: grid;
      gap: 14px;
    }

    .tender-card {
      padding: 20px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
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
      font-size: 22px;
      line-height: 1.25;
    }

    .tender-meta {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.65;
    }

    .band {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 10px;
    }

    .band.high {
      color: var(--accent-3);
      background: rgba(115, 240, 166, 0.10);
      border: 1px solid rgba(115, 240, 166, 0.18);
    }

    .band.medium {
      color: #f2d993;
      background: rgba(232, 202, 125, 0.10);
      border: 1px solid rgba(232, 202, 125, 0.18);
    }

    .band.low {
      color: #efb4b4;
      background: rgba(240, 143, 143, 0.08);
      border: 1px solid rgba(240, 143, 143, 0.16);
    }

    .score-number {
      font-size: 30px;
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
      grid-template-columns: 1.05fr 0.95fr;
      gap: 14px;
      margin-top: 14px;
    }

    .mini {
      padding: 16px;
      border-radius: 18px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(115, 240, 166, 0.08);
    }

    .mini h4 {
      margin: 0 0 10px 0;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent-3);
    }

    .value-big {
      font-size: 30px;
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
      line-height: 1.55;
      font-size: 14px;
    }

    .check {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: rgba(115, 240, 166, 0.10);
      color: var(--accent-3);
      font-weight: 900;
      flex: 0 0 22px;
      box-shadow: 0 0 18px rgba(115, 240, 166, 0.12);
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
      background: rgba(115, 240, 166, 0.08);
      border: 1px solid rgba(115, 240, 166, 0.12);
      color: var(--accent-3);
      font-size: 12px;
      font-weight: 700;
    }

    .empty {
      padding: 24px;
      text-align: center;
      color: var(--muted);
      border: 1px dashed rgba(115, 240, 166, 0.10);
      border-radius: 20px;
      background: rgba(255,255,255,0.02);
    }

    .explorer-layout {
      display: grid;
      grid-template-columns: 0.88fr 1.12fr;
      gap: 18px;
      align-items: start;
    }

    .list-card,
    .detail-card {
      padding: 20px;
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
      padding-right: 4px;
    }

    .list-item {
      padding: 16px;
      border-radius: 18px;
      border: 1px solid rgba(115, 240, 166, 0.08);
      background: rgba(255,255,255,0.03);
      margin-bottom: 10px;
      cursor: pointer;
      transition: 0.2s ease;
    }

    .list-item:hover,
    .list-item.active {
      border-color: rgba(115, 240, 166, 0.20);
      box-shadow: 0 14px 30px rgba(115, 240, 166, 0.08);
      background: rgba(255,255,255,0.04);
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
      flex-wrap: wrap;
      margin-bottom: 14px;
    }

    .detail-head h2 {
      margin: 0 0 6px 0;
      font-size: 28px;
      line-height: 1.2;
    }

    .detail-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-top: 14px;
    }

    .section-block {
      margin-top: 18px;
    }

    .section-block h3 {
      margin: 0 0 10px 0;
      font-size: 18px;
    }

    .advice-box,
    .service-box {
      padding: 16px;
      border-radius: 18px;
      border: 1px solid rgba(115, 240, 166, 0.08);
      background: rgba(255,255,255,0.03);
      margin-top: 12px;
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
      line-height: 1.6;
      margin-top: 8px;
    }

    @media (max-width: 1180px) {
      .hero-content,
      .assistant-layout,
      .explorer-layout,
      .summary-grid,
      .toolbar,
      .two-col,
      .detail-grid {
        grid-template-columns: 1fr;
      }

      .features-strip {
        overflow: auto;
      }

      .carousel-track {
        animation: none;
      }
    }

    @media (max-width: 760px) {
      .shell {
        padding: 14px;
      }

      .topbar {
        flex-direction: column;
        align-items: flex-start;
      }

      .hero {
        min-height: auto;
        padding: 2.6rem 1.2rem 2rem 1.2rem;
      }

      .hero-actions {
        flex-direction: column;
      }

      .form-grid,
      .service-form {
        grid-template-columns: 1fr;
      }

      .tender-top {
        flex-direction: column;
      }

      .score-number,
      .score-caption {
        text-align: left;
      }

      .feature-card {
        flex-basis: 300px;
      }
    }
  </style>
</head>
<body>
  <div class="loading-overlay" id="loadingOverlay">
    <div class="loading-modal">
      <div class="loading-orb"></div>
      <div class="loading-title">TenderAI is analyzing</div>
      <div class="loading-step" id="loadingStepText">Reading supplier profile and scanning tender releases...</div>
    </div>
  </div>

  <div class="shell">
    <div class="topbar">
      <div class="brand">
        <img src="https://static.wixstatic.com/media/7193cf_13ca777ff7cc4c79a68caa1b2024f707~mv2.png" alt="Logo">
        <div>
          <div class="brand-title">TenderAI</div>
          <div class="brand-sub">Procurement intelligence and execution planning</div>
        </div>
      </div>

      <div class="nav-links">
        <a class="nav-link" href="#assistant">Assistant</a>
        <a class="nav-link" href="#matches">Matches</a>
        <a class="nav-link" href="#explorer">Explorer</a>
      </div>
    </div>

    <section class="hero">
      <div class="grid-lines"></div>
      <div class="hero-content">
        <div>
          <div class="eyebrow">Procurement intelligence platform</div>
          <h1>Discover, evaluate, and plan for the tenders your business is built to pursue.</h1>
          <p>
            TenderAI reads your supplier profile, compares your business signals against live public tenders,
            estimates likely contract value and delivery investment, and helps you understand where to focus your effort.
          </p>
          <div class="hero-actions">
            <button class="btn btn-primary" onclick="document.getElementById('assistant').scrollIntoView({behavior:'smooth'})">Start analysis</button>
            <button class="btn btn-secondary" onclick="document.getElementById('explorer').scrollIntoView({behavior:'smooth'})">Browse tenders</button>
          </div>
        </div>

        <div class="hero-stats">
          <div class="hero-stat">
            <div class="hero-stat-label">Supplier profile understanding</div>
            <div class="hero-stat-value">PDF + AI</div>
            <div class="hero-stat-sub">Reads supplier profiles and extracts capability signals.</div>
          </div>
          <div class="hero-stat">
            <div class="hero-stat-label">Opportunity ranking</div>
            <div class="hero-stat-value">Live</div>
            <div class="hero-stat-sub">Scores public tenders using profile, category, and scope alignment.</div>
          </div>
          <div class="hero-stat">
            <div class="hero-stat-label">Execution planning</div>
            <div class="hero-stat-value">Estimated</div>
            <div class="hero-stat-sub">Forecasts project value and likely investment requirements.</div>
          </div>
          <div class="hero-stat">
            <div class="hero-stat-label">Support services</div>
            <div class="hero-stat-value">On demand</div>
            <div class="hero-stat-sub">Request logistics and tender support for selected opportunities.</div>
          </div>
        </div>
      </div>
    </section>

    <section class="features-strip">
      <div class="carousel-track">
        <div class="feature-card">
          <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1516321318423-f06f85e504b3?auto=format&fit=crop&w=1200&q=80');"></div>
          <div class="feature-overlay"></div>
          <div class="feature-copy">
            <h3>Profile understanding</h3>
            <p>Extract business signals, trade focus, and capability language from profile documents.</p>
          </div>
        </div>

        <div class="feature-card">
          <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&w=1200&q=80');"></div>
          <div class="feature-overlay"></div>
          <div class="feature-copy">
            <h3>Opportunity ranking</h3>
            <p>Surface the tenders that align most strongly with your business capabilities and delivery intent.</p>
          </div>
        </div>

        <div class="feature-card">
          <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1520607162513-77705c0f0d4a?auto=format&fit=crop&w=1200&q=80');"></div>
          <div class="feature-overlay"></div>
          <div class="feature-copy">
            <h3>Execution support</h3>
            <p>Estimate delivery investment, request advice, and route logistics support into your operating process.</p>
          </div>
        </div>

        <div class="feature-card">
          <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1516321318423-f06f85e504b3?auto=format&fit=crop&w=1200&q=80');"></div>
          <div class="feature-overlay"></div>
          <div class="feature-copy">
            <h3>Profile understanding</h3>
            <p>Extract business signals, trade focus, and capability language from profile documents.</p>
          </div>
        </div>

        <div class="feature-card">
          <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&w=1200&q=80');"></div>
          <div class="feature-overlay"></div>
          <div class="feature-copy">
            <h3>Opportunity ranking</h3>
            <p>Surface the tenders that align most strongly with your business capabilities and delivery intent.</p>
          </div>
        </div>

        <div class="feature-card">
          <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1520607162513-77705c0f0d4a?auto=format&fit=crop&w=1200&q=80');"></div>
          <div class="feature-overlay"></div>
          <div class="feature-copy">
            <h3>Execution support</h3>
            <p>Estimate delivery investment, request advice, and route logistics support into your operating process.</p>
          </div>
        </div>
      </div>
    </section>

    <section id="assistant" class="section">
      <div class="section-head">
        <div>
          <h2>Assistant</h2>
          <p>Run a profile-led tender analysis through a guided AI workflow.</p>
        </div>
      </div>

      <div class="assistant-layout">
        <div class="card">
          <div class="chat-thread">
            <div class="message">
              <div class="avatar"></div>
              <div class="message-bubble">
                <h3>Start with your business profile.</h3>
                <p>Upload a supplier profile PDF or paste business capability text. TenderAI will interpret your profile, scan public tenders, rank relevant opportunities, and estimate what delivery might require.</p>
              </div>
            </div>

            <div class="message user">
              <div class="avatar"></div>
              <div class="message-bubble">
                <div class="form-grid">
                  <div class="field">
                    <label for="profile_pdf">Supplier profile PDF</label>
                    <input type="file" id="profile_pdf" accept=".pdf">
                    <div class="hint">CSD summaries, company profiles, and capability statements work best.</div>
                  </div>

                  <div class="field">
                    <label for="profile_text">Or paste profile text</label>
                    <textarea id="profile_text" placeholder="Use this only if you are not uploading a PDF."></textarea>
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
                  <button class="btn btn-primary" id="runScanBtn" type="button">Run TenderAI analysis</button>
                  <button class="btn btn-secondary" id="clearBtn" type="button">Clear</button>
                </div>
              </div>
            </div>

            <div class="message">
              <div class="avatar"></div>
              <div class="message-bubble" id="assistantResponse">
                <p>Once the scan completes, TenderAI will summarize what it understood from your profile and show your ranked opportunities below.</p>
              </div>
            </div>
          </div>
        </div>

        <div class="card">
          <h3 style="margin-top:0;">What TenderAI understood</h3>
          <div id="scanUnderstanding" class="empty">Run a scan to see extracted keywords and a concise profile interpretation.</div>
        </div>
      </div>
    </section>

    <section id="matches" class="section">
      <div class="section-head">
        <div>
          <h2>Best-fit matches</h2>
          <p>Ranked opportunities based on your profile and the tender scope.</p>
        </div>
      </div>

      <div class="summary-grid" id="summaryGrid" style="display:none;">
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

      <div id="resultsList" class="results-list">
        <div class="empty">Your ranked opportunity matches will appear here after an analysis.</div>
      </div>
    </section>

    <section id="explorer" class="section">
      <div class="section-head">
        <div>
          <h2>Tender Explorer</h2>
          <p>Browse all tenders manually, inspect details, request advice, and request support.</p>
        </div>
      </div>

      <div class="explorer-layout">
        <div class="card list-card">
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

          <div id="explorerList" class="list-scroll">
            <div class="empty">Load tenders to browse the market manually.</div>
          </div>
        </div>

        <div class="card detail-card">
          <div id="detailPanel">
            <div class="empty">Select a tender to view its overview, AI advice, value estimate, execution investment, and support options.</div>
          </div>
        </div>
      </div>
    </section>
  </div>

  <script>
    let latestScan = null;
    let explorerTenders = [];
    let selectedTender = null;

    const loadingMessages = [
      "Reading supplier profile and extracting business signals...",
      "Scanning public tender releases and filtering opportunities...",
      "Scoring tender relevance and estimating contract value...",
      "Calculating likely execution investment and readiness needs..."
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

    function buildWhyMatched(t) {
      const items = [];

      if (t.matched_keywords && t.matched_keywords.length) {
        items.push("Matched capability keywords: " + t.matched_keywords.join(", "));
      }
      if (t.category && ["works", "services"].includes(String(t.category).toLowerCase())) {
        items.push("Tender category aligns with operational delivery work.");
      }
      if (t.estimation_reason) {
        items.push("Tender value inference: " + t.estimation_reason);
      }
      if (t.execution_investment_reason) {
        items.push("Execution planning signal: " + t.execution_investment_reason);
      }
      if (!items.length) {
        items.push("TenderAI found limited direct fit signals in this opportunity.");
      }

      return items;
    }

    function renderTenderCard(t) {
      const whyHtml = buildWhyMatched(t).map(item => `
        <div class="why-item">
          <div class="check">✓</div>
          <div>${escapeHtml(item)}</div>
        </div>
      `).join("");

      return `
        <div class="tender-card">
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
              <div class="why-list">${whyHtml}</div>
              <div class="keyword-list">
                ${(t.matched_keywords || []).map(k => `<span class="chip">${escapeHtml(k)}</span>`).join("")}
              </div>
            </div>

            <div class="mini">
              <h4>Value and investment outlook</h4>
              <div class="value-big">${escapeHtml(safeText(t.value_display))}</div>
              <div class="tender-meta">
                Source: ${escapeHtml(safeText(t.value_source))} •
                Confidence: ${escapeHtml(safeText(t.estimation_confidence))}
              </div>
              <div style="margin-top: 10px;">${escapeHtml(safeText(t.estimation_reason))}</div>
              <div style="margin-top: 14px; font-weight: 800;">
                Expected execution investment: ${escapeHtml(safeText(t.execution_investment_display))}
              </div>
              <div class="tender-meta">${escapeHtml(safeText(t.execution_investment_reason))}</div>
            </div>
          </div>
        </div>
      `;
    }

    function renderScanUnderstanding(data) {
      const keywords = (data.profile_keywords || []).map(k => `<span class="chip">${escapeHtml(k)}</span>`).join("");

      document.getElementById("scanUnderstanding").innerHTML = `
        <div class="message">
          <div class="avatar"></div>
          <div class="message-bubble" style="width:100%;">
            <h4>Profile interpretation</h4>
            <p>I used your submitted profile to extract capability signals and compare them to tender descriptions, procurement categories, and likely delivery requirements.</p>
            <div class="keyword-list">${keywords || '<span class="chip">No keywords extracted</span>'}</div>
          </div>
        </div>

        <div class="message" style="margin-top: 14px;">
          <div class="avatar"></div>
          <div class="message-bubble" style="width:100%;">
            <h4>Profile preview</h4>
            <p>${escapeHtml(data.profile_text_preview || "No preview available.")}</p>
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
        list.innerHTML = '<div class="empty">No tenders were found for this request.</div>';
      } else {
        list.innerHTML = data.tenders.map(renderTenderCard).join("");
      }

      renderScanUnderstanding(data);
    }

    function setLoadingState(show) {
      document.getElementById("loadingOverlay").style.display = show ? "flex" : "none";
    }

    function startLoadingMessages() {
      let idx = 0;
      document.getElementById("loadingStepText").textContent = loadingMessages[0];
      window.loadingTicker = setInterval(() => {
        idx = (idx + 1) % loadingMessages.length;
        document.getElementById("loadingStepText").textContent = loadingMessages[idx];
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
          return;
        }

        latestScan = data;
        renderAssistantResults(data);
        document.getElementById("assistantResponse").innerHTML = `
          <p>I completed the analysis and ranked <strong>${escapeHtml(data.summary.returned_tenders)}</strong> tender opportunities.</p>
          <p><strong>${escapeHtml(data.summary.high_fit)}</strong> were classified as high-fit matches using the profile you submitted.</p>
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
      setLoadingState(true);
      document.getElementById("loadingStepText").textContent = "Loading live tender market data...";

      const list = document.getElementById("explorerList");
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
          list.innerHTML = `<div class="empty">No tenders found for this date range.</div>`;
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
        setLoadingState(false);
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
          <h3 style="margin-top:0;">How to improve your score</h3>
          <div class="why-list">
            ${data.advice.map(item => `
              <div class="why-item">
                <div class="check">✓</div>
                <div>${escapeHtml(item)}</div>
              </div>
            `).join("")}
          </div>
          <div style="margin-top: 14px;">
            <strong>Recommended supporting documents</strong>
            <div class="keyword-list">
              ${data.recommended_documents.map(item => `<span class="chip">${escapeHtml(item)}</span>`).join("")}
            </div>
          </div>
        </div>
      `;
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
      document.getElementById("serviceResult").textContent =
        data.status === "ok"
          ? "Request captured. Reference: " + data.reference
          : "Unable to submit request.";
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
          <div class="${bandClass(t.fit_band || "Low fit")}">${escapeHtml(safeText(t.fit_band || "Unscored"))}</div>
        </div>

        <div class="mini">
          <h4>Tender overview</h4>
          <div>${escapeHtml(safeText(t.description, "No description supplied."))}</div>
        </div>

        <div class="detail-grid">
          <div class="mini">
            <h4>Estimated tender value</h4>
            <div class="value-big">${escapeHtml(safeText(t.value_display))}</div>
            <div class="tender-meta">
              Source: ${escapeHtml(safeText(t.value_source))} •
              Confidence: ${escapeHtml(safeText(t.estimation_confidence))}
            </div>
            <div style="margin-top:10px;">${escapeHtml(safeText(t.estimation_reason))}</div>
          </div>

          <div class="mini">
            <h4>Expected execution investment</h4>
            <div class="value-big">${escapeHtml(safeText(t.execution_investment_display))}</div>
            <div class="tender-meta">${escapeHtml(safeText(t.execution_investment_reason))}</div>
          </div>
        </div>

        <div class="section-block">
          <h3>Ask TenderAI for advice</h3>
          <div class="actions">
            <button class="btn btn-primary" id="adviceBtn" type="button">How can I score better for this tender?</button>
          </div>
          <div id="adviceResult" style="margin-top:12px;">
            <div class="empty">Request tailored bid advice for this tender.</div>
          </div>
        </div>

        <div class="section-block">
          <h3>Request logistics services</h3>
          <div class="service-box">
            <div class="tender-meta">
              Request bid logistics support, supplier coordination, response preparation, project readiness planning, or execution support.
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
                <textarea id="service_notes" placeholder="Describe the support you want for this tender."></textarea>
              </div>
            </div>

            <div class="actions">
              <button class="btn btn-primary" id="requestServiceBtn" type="button">Request support</button>
            </div>
            <div id="serviceResult" class="notice"></div>
          </div>
        </div>
      `;

      document.getElementById("adviceBtn").addEventListener("click", getTenderAdvice);
      document.getElementById("requestServiceBtn").addEventListener("click", submitServiceRequest);
    }

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
            advice.append("Sharpen your capability statement so it mirrors the exact tender language more directly.")
        else:
            advice.append("Reflect the strongest matched keywords in your executive summary, methodology, and pricing narrative.")

        if "generator" in f"{title} {description}".lower():
            advice.append("Include generator-specific references, electrical compliance evidence, and technical delivery capability.")
            recommended_documents.extend([
                "Electrical compliance certificate",
                "Generator installation references",
                "Technical methodology"
            ])

        if category.lower() == "works":
            advice.append("Show site methodology, supervision structure, safety planning, and mobilisation readiness.")
            recommended_documents.extend([
                "Health and safety file",
                "Construction methodology",
                "Site mobilisation plan"
            ])

        if category.lower() == "services":
            advice.append("Show turnaround times, staffing depth, response processes, and geographic operating capacity.")
            recommended_documents.extend([
                "Service delivery plan",
                "Team CVs",
                "Operational response plan"
            ])

        if not profile_text:
            advice.append("Submit a richer supplier profile or capability statement so TenderAI can compare more precise signals.")
        else:
            advice.append("Tailor your cover letter so it directly links your business strengths to the tender scope and delivery risk.")

        advice.append("Validate working capital early, because the estimated execution investment suggests meaningful upfront spend before payment is received.")

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

    reference = f"TAI-{abs(hash((name, company, tender.get('ocid', 'NA')))) % 1000000:06d}"

    return jsonify({
        "status": "ok",
        "reference": reference
    })
