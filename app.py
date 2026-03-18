from flask import Flask, jsonify, request, render_template_string
import requests
import re
import io
import uuid
from datetime import datetime, timezone
from collections import Counter
from pypdf import PdfReader

app = Flask(__name__)

PROFILE_STORE = {}


HTML_PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TenderAI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #060b08;
      --bg-2: #0a120e;
      --panel: rgba(12, 18, 14, 0.72);
      --panel-solid: rgba(11, 17, 13, 0.92);
      --panel-soft: rgba(255,255,255,0.03);
      --border: rgba(132, 233, 167, 0.10);
      --text: #edf6ef;
      --muted: #9caf9f;
      --accent: #7ef0ab;
      --accent-2: #42cc7e;
      --accent-3: #c7ffd9;
      --amber: #e8c874;
      --red: #f29a9a;
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      --radius: 28px;
      --maxw: 1380px;
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
        radial-gradient(circle at 12% 12%, rgba(66, 204, 126, 0.08), transparent 24%),
        radial-gradient(circle at 88% 10%, rgba(126, 240, 171, 0.08), transparent 20%),
        linear-gradient(180deg, #050907 0%, #08100c 50%, #07100b 100%);
      min-height: 100vh;
      overflow-x: hidden;
    }

    body::before,
    body::after {
      content: "";
      position: fixed;
      width: 34rem;
      height: 34rem;
      border-radius: 50%;
      filter: blur(120px);
      opacity: 0.22;
      pointer-events: none;
      z-index: 0;
    }

    body::before {
      top: -10rem;
      left: -10rem;
      background: radial-gradient(circle, rgba(66, 204, 126, 0.9) 0%, rgba(66, 204, 126, 0) 70%);
      animation: driftA 18s ease-in-out infinite alternate;
    }

    body::after {
      right: -12rem;
      top: 10rem;
      background: radial-gradient(circle, rgba(126, 240, 171, 0.72) 0%, rgba(126, 240, 171, 0) 72%);
      animation: driftB 24s ease-in-out infinite alternate;
    }

    @keyframes driftA {
      0% { transform: translate3d(0, 0, 0) scale(1); }
      100% { transform: translate3d(5rem, 6rem, 0) scale(1.1); }
    }

    @keyframes driftB {
      0% { transform: translate3d(0, 0, 0) scale(1); }
      100% { transform: translate3d(-6rem, 8rem, 0) scale(1.08); }
    }

    .page {
      position: relative;
      z-index: 1;
    }

    .topbar {
      position: sticky;
      top: 16px;
      z-index: 30;
      width: min(var(--maxw), calc(100% - 32px));
      margin: 18px auto 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-radius: 22px;
      background: rgba(8, 12, 10, 0.68);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }

    .brand {
      font-size: 22px;
      font-weight: 800;
      letter-spacing: -0.02em;
    }

    .brand span {
      color: var(--accent-3);
    }

    .nav {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }

    .nav button {
      color: var(--muted);
      background: rgba(255,255,255,0.02);
      border: 1px solid rgba(255,255,255,0.04);
      font-size: 13px;
      font-weight: 700;
      padding: 10px 14px;
      border-radius: 999px;
      cursor: pointer;
      transition: 0.2s ease;
    }

    .nav button:hover,
    .nav button.active {
      color: var(--accent-3);
      border-color: rgba(126, 240, 171, 0.18);
    }

    .hero {
      position: relative;
      overflow: hidden;
      min-height: 84vh;
      margin: 20px auto 24px auto;
      width: min(100%, calc(100vw - 24px));
      border-radius: 34px;
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      background:
        linear-gradient(180deg, rgba(9, 14, 11, 0.72) 0%, rgba(9, 14, 11, 0.88) 100%),
        linear-gradient(135deg, #08110c 0%, #0b1712 55%, #08110c 100%);
    }

    .hero::before,
    .hero::after {
      content: "";
      position: absolute;
      border-radius: 50%;
      filter: blur(100px);
      opacity: 0.18;
      pointer-events: none;
    }

    .hero::before {
      width: 26rem;
      height: 26rem;
      left: 7%;
      top: 8%;
      background: radial-gradient(circle, rgba(126, 240, 171, 0.92) 0%, rgba(126, 240, 171, 0) 70%);
      animation: heroGlowA 20s ease-in-out infinite alternate;
    }

    .hero::after {
      width: 28rem;
      height: 28rem;
      right: 4%;
      bottom: -8%;
      background: radial-gradient(circle, rgba(66, 204, 126, 0.86) 0%, rgba(66, 204, 126, 0) 72%);
      animation: heroGlowB 24s ease-in-out infinite alternate;
    }

    @keyframes heroGlowA {
      0% { transform: translate3d(0, 0, 0) scale(1); }
      100% { transform: translate3d(4rem, 5rem, 0) scale(1.12); }
    }

    @keyframes heroGlowB {
      0% { transform: translate3d(0, 0, 0) scale(1); }
      100% { transform: translate3d(-5rem, -4rem, 0) scale(1.08); }
    }

    .hero-grid {
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(rgba(126, 240, 171, 0.028) 1px, transparent 1px),
        linear-gradient(90deg, rgba(126, 240, 171, 0.028) 1px, transparent 1px);
      background-size: 48px 48px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.9), transparent 86%);
      pointer-events: none;
    }

    .hero-inner {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: 1.08fr 0.92fr;
      gap: 28px;
      align-items: end;
      min-height: 84vh;
      padding: 5rem 2.2rem 2.6rem 2.2rem;
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
      margin-bottom: 18px;
    }

    .eyebrow::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 16px rgba(126, 240, 171, 0.95);
    }

    .hero-copy h1 {
      margin: 0 0 16px 0;
      font-size: clamp(3rem, 6vw, 5.4rem);
      line-height: 0.96;
      letter-spacing: -0.05em;
      max-width: 820px;
    }

    .hero-copy p {
      margin: 0;
      max-width: 760px;
      color: var(--muted);
      line-height: 1.8;
      font-size: 17px;
    }

    .hero-actions {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 28px;
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
      color: #061008;
      box-shadow: 0 14px 32px rgba(66, 204, 126, 0.24);
    }

    .btn-secondary {
      color: var(--text);
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(126, 240, 171, 0.08);
    }

    .hero-panel-stack {
      display: grid;
      gap: 14px;
      align-self: center;
    }

    .hero-panel {
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(126, 240, 171, 0.08);
      backdrop-filter: blur(12px);
    }

    .hero-panel-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      margin-bottom: 8px;
    }

    .hero-panel-value {
      font-size: 30px;
      font-weight: 900;
      margin-bottom: 6px;
    }

    .hero-panel-copy {
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
    }

    .view {
      display: none;
      width: min(var(--maxw), calc(100% - 32px));
      margin: 0 auto 26px auto;
    }

    .view.active {
      display: block;
    }

    .features-wrap {
      overflow: hidden;
      border-radius: 30px;
      margin-bottom: 24px;
    }

    .carousel-track {
      display: flex;
      gap: 18px;
      width: max-content;
      animation: marquee 32s linear infinite;
      padding: 0 4px;
    }

    .features-wrap:hover .carousel-track {
      animation-play-state: paused;
    }

    @keyframes marquee {
      0% { transform: translateX(0); }
      100% { transform: translateX(-50%); }
    }

    .feature-card {
      position: relative;
      flex: 0 0 380px;
      min-height: 260px;
      border-radius: 28px;
      overflow: hidden;
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      background: #0d1712;
    }

    .feature-image {
      position: absolute;
      inset: 0;
      background-size: cover;
      background-position: center;
      filter: brightness(0.72) saturate(1.02);
      transform: scale(1.02);
    }

    .feature-overlay {
      position: absolute;
      inset: 0;
      background:
        linear-gradient(180deg, rgba(8, 12, 10, 0.12) 0%, rgba(8, 12, 10, 0.70) 66%, rgba(8, 12, 10, 0.96) 100%);
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
      font-size: 14px;
      line-height: 1.6;
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

    .card {
      padding: 20px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 26px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }

    .prompt-card {
      max-width: 980px;
    }

    .prompt-box {
      margin-top: 18px;
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(126, 240, 171, 0.08);
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
      border: 1px solid rgba(126, 240, 171, 0.10);
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
      border-color: rgba(126, 240, 171, 0.28);
      box-shadow: 0 0 0 4px rgba(126, 240, 171, 0.08);
    }

    textarea {
      min-height: 140px;
      resize: vertical;
    }

    .compact-grid {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr 1fr;
      gap: 14px;
      margin-top: 14px;
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
      margin-top: 14px;
    }

    .profiles-grid {
      display: grid;
      grid-template-columns: 420px 1fr;
      gap: 18px;
      align-items: start;
    }

    .upload-box {
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(126, 240, 171, 0.08);
    }

    .profiles-list {
      display: grid;
      gap: 12px;
    }

    .profile-card {
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(126, 240, 171, 0.08);
    }

    .profile-card.active {
      border-color: rgba(126, 240, 171, 0.24);
      box-shadow: 0 0 0 1px rgba(126, 240, 171, 0.08) inset;
    }

    .profile-title-row {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 12px;
      margin-bottom: 10px;
    }

    .profile-title {
      font-size: 18px;
      font-weight: 800;
    }

    .profile-meta {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }

    .profile-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
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
      border: 1px solid rgba(126, 240, 171, 0.08);
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

    .feed-layout {
      display: grid;
      grid-template-columns: 0.95fr 1.05fr;
      gap: 18px;
      align-items: start;
    }

    .insight-stack {
      display: grid;
      gap: 14px;
    }

    .insight-card {
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(126, 240, 171, 0.08);
    }

    .insight-card h3 {
      margin-top: 0;
      margin-bottom: 10px;
      font-size: 18px;
    }

    .bar-list {
      display: grid;
      gap: 10px;
    }

    .bar-row {
      display: grid;
      grid-template-columns: 120px 1fr 52px;
      gap: 10px;
      align-items: center;
    }

    .bar-label {
      color: var(--muted);
      font-size: 13px;
    }

    .bar-track {
      height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      overflow: hidden;
    }

    .bar-fill {
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent-2), var(--accent));
    }

    .bar-value {
      text-align: right;
      font-size: 13px;
      color: var(--text);
    }

    .results-list {
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
      background: rgba(126, 240, 171, 0.10);
      border: 1px solid rgba(126, 240, 171, 0.18);
    }

    .band.medium {
      color: #f2d993;
      background: rgba(232, 200, 116, 0.10);
      border: 1px solid rgba(232, 200, 116, 0.18);
    }

    .band.low {
      color: #efb4b4;
      background: rgba(242, 154, 154, 0.08);
      border: 1px solid rgba(242, 154, 154, 0.16);
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
      border: 1px solid rgba(126, 240, 171, 0.08);
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
      background: rgba(126, 240, 171, 0.10);
      color: var(--accent-3);
      font-weight: 900;
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
      background: rgba(126, 240, 171, 0.08);
      border: 1px solid rgba(126, 240, 171, 0.12);
      color: var(--accent-3);
      font-size: 12px;
      font-weight: 700;
    }

    .feed-toolbar {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 14px;
    }

    .explorer-layout {
      display: grid;
      grid-template-columns: 0.88fr 1.12fr;
      gap: 18px;
      align-items: start;
    }

    .list-scroll {
      max-height: 760px;
      overflow: auto;
      padding-right: 4px;
    }

    .list-item {
      padding: 16px;
      border-radius: 18px;
      border: 1px solid rgba(126, 240, 171, 0.08);
      background: rgba(255,255,255,0.03);
      margin-bottom: 10px;
      cursor: pointer;
      transition: 0.2s ease;
    }

    .list-item:hover,
    .list-item.active {
      border-color: rgba(126, 240, 171, 0.20);
      box-shadow: 0 14px 30px rgba(126, 240, 171, 0.08);
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
      border: 1px solid rgba(126, 240, 171, 0.08);
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
      background: rgba(10, 19, 14, 0.94);
      border: 1px solid rgba(126, 240, 171, 0.12);
      box-shadow: var(--shadow);
      text-align: center;
    }

    .loading-orb {
      width: 90px;
      height: 90px;
      margin: 0 auto 20px auto;
      border-radius: 50%;
      position: relative;
      border: 1px solid rgba(126, 240, 171, 0.10);
      background: radial-gradient(circle at center, rgba(126, 240, 171, 0.06), transparent 68%);
    }

    .loading-orb::before {
      content: "";
      position: absolute;
      inset: 10px;
      border-radius: 50%;
      border: 1px dashed rgba(126, 240, 171, 0.18);
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
      box-shadow: 0 0 24px rgba(126, 240, 171, 0.95);
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

    .empty {
      padding: 24px;
      text-align: center;
      color: var(--muted);
      border: 1px dashed rgba(126, 240, 171, 0.10);
      border-radius: 20px;
      background: rgba(255,255,255,0.02);
    }

    @media (max-width: 1180px) {
      .hero-inner,
      .profiles-grid,
      .assistant-layout,
      .feed-layout,
      .explorer-layout,
      .summary-grid,
      .feed-toolbar,
      .compact-grid,
      .two-col,
      .detail-grid,
      .service-form {
        grid-template-columns: 1fr;
      }

      .features-wrap {
        overflow: auto;
      }

      .carousel-track {
        animation: none;
      }
    }

    @media (max-width: 760px) {
      .topbar {
        width: calc(100% - 20px);
        flex-direction: column;
        align-items: flex-start;
      }

      .hero {
        width: calc(100% - 10px);
        min-height: auto;
      }

      .hero-inner {
        min-height: auto;
        padding: 3rem 1.2rem 2rem 1.2rem;
      }

      .hero-actions {
        flex-direction: column;
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

  <div class="page">
    <div class="topbar">
      <div class="brand">Tender<span>AI</span></div>
      <div class="nav">
        <button class="nav-btn active" data-view="homeView">Home</button>
        <button class="nav-btn" data-view="profilesView">Profiles</button>
        <button class="nav-btn" data-view="feedView">Feed</button>
      </div>
    </div>

    <section id="homeView" class="view active">
      <section class="hero">
        <div class="hero-grid"></div>
        <div class="hero-inner">
          <div class="hero-copy">
            <div class="eyebrow">Procurement intelligence platform</div>
            <h1>The strategic layer for public procurement opportunity discovery.</h1>
            <p>
              TenderAI helps businesses understand where to bid, why a tender matters, what it may take to execute,
              and how to position more intelligently. Select a company profile, ask a procurement question,
              and get tailored tender intelligence.
            </p>

            <div class="prompt-box">
              <label for="home_prompt">Ask TenderAI</label>
              <textarea id="home_prompt" placeholder="What tenders should I pursue this week for my construction business in Gauteng?"></textarea>

              <div class="compact-grid">
                <div>
                  <label for="home_profile_select">Business profile</label>
                  <select id="home_profile_select"></select>
                </div>
                <div>
                  <label for="home_date_from">From date</label>
                  <input type="date" id="home_date_from" value="2026-01-01">
                </div>
                <div>
                  <label for="home_date_to">To date</label>
                  <input type="date" id="home_date_to" value="2026-03-17">
                </div>
                <div>
                  <label for="home_page_size">Scan size</label>
                  <input type="number" id="home_page_size" value="10" min="1" max="100">
                </div>
              </div>

              <div class="actions">
                <button class="btn btn-primary" id="runPromptBtn" type="button">Run analysis</button>
                <button class="btn btn-secondary" id="goProfilesBtn" type="button">Manage profiles</button>
              </div>
              <div class="hint">The homepage stays minimal. Detailed analysis, tender feed, and profile management live on separate pages.</div>
            </div>
          </div>

          <div class="hero-panel-stack">
            <div class="hero-panel">
              <div class="hero-panel-label">Product positioning</div>
              <div class="hero-panel-value">Procurement intelligence</div>
              <div class="hero-panel-copy">A strategic system for understanding opportunities, not just listing them.</div>
            </div>
            <div class="hero-panel">
              <div class="hero-panel-label">Intelligence layer</div>
              <div class="hero-panel-value">Profile-led</div>
              <div class="hero-panel-copy">Scores live tenders against company profile evidence and likely bid-readiness.</div>
            </div>
            <div class="hero-panel">
              <div class="hero-panel-label">Operational insight</div>
              <div class="hero-panel-value">Decision-ready</div>
              <div class="hero-panel-copy">Estimate value, effort, risk, and what it may cost to deliver the contract.</div>
            </div>
          </div>
        </div>
      </section>

      <section class="features-wrap">
        <div class="carousel-track">
          <div class="feature-card">
            <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1516321318423-f06f85e504b3?auto=format&fit=crop&w=1200&q=80');"></div>
            <div class="feature-overlay"></div>
            <div class="feature-copy">
              <h3>Profile understanding</h3>
              <p>Extract business signals, service focus, and capability language from supplier profiles and PDF documents.</p>
            </div>
          </div>
          <div class="feature-card">
            <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&w=1200&q=80');"></div>
            <div class="feature-overlay"></div>
            <div class="feature-copy">
              <h3>Opportunity ranking</h3>
              <p>Identify the tenders that align most strongly with your profile, category, scope, and likely execution fit.</p>
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
              <p>Extract business signals, service focus, and capability language from supplier profiles and PDF documents.</p>
            </div>
          </div>
          <div class="feature-card">
            <div class="feature-image" style="background-image:url('https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&w=1200&q=80');"></div>
            <div class="feature-overlay"></div>
            <div class="feature-copy">
              <h3>Opportunity ranking</h3>
              <p>Identify the tenders that align most strongly with your profile, category, scope, and likely execution fit.</p>
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
    </section>

    <section id="profilesView" class="view">
      <div class="container">
        <div class="section-head">
          <div>
            <h2>Business profiles</h2>
            <p>Upload and manage company profiles. Select one as active for analysis.</p>
          </div>
        </div>

        <div class="profiles-grid">
          <div class="card">
            <div class="upload-box">
              <label for="profile_upload">Upload company profile PDF</label>
              <input type="file" id="profile_upload" accept=".pdf">
              <div class="hint">Profiles are stored in memory in this single-file version. After a restart or redeploy, upload them again.</div>
              <div class="actions">
                <button class="btn btn-primary" id="uploadProfileBtn" type="button">Upload profile</button>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="section-head" style="margin-bottom:12px;">
              <div>
                <h2 style="font-size:22px;">Saved profiles</h2>
                <p style="margin-top:4px;">Use one active profile when prompting TenderAI.</p>
              </div>
            </div>
            <div id="profilesList" class="profiles-list">
              <div class="empty">No profiles uploaded yet.</div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="feedView" class="view">
      <div class="container">
        <div class="section-head">
          <div>
            <h2>Procurement feed</h2>
            <p>Recommendation engine, market analytics, and detailed tender intelligence.</p>
          </div>
        </div>

        <div class="summary-grid" id="summaryGrid" style="display:none;">
          <div class="metric">
            <div class="metric-label">Available tenders</div>
            <div class="metric-value" id="mTotal">0</div>
            <div class="metric-sub">Scanned opportunities</div>
          </div>
          <div class="metric">
            <div class="metric-label">High-fit tenders</div>
            <div class="metric-value" id="mHigh">0</div>
            <div class="metric-sub">Priority targets</div>
          </div>
          <div class="metric">
            <div class="metric-label">Closing soon</div>
            <div class="metric-value" id="mSoon">0</div>
            <div class="metric-sub">Within 7 days</div>
          </div>
          <div class="metric">
            <div class="metric-label">Average contract value</div>
            <div class="metric-value" id="mAvgValue">R0</div>
            <div class="metric-sub">Estimated / published</div>
          </div>
        </div>

        <div class="feed-layout">
          <div class="insight-stack">
            <div class="card">
              <div class="section-head" style="margin-bottom:12px;">
                <div>
                  <h2 style="font-size:22px;">Best opportunities for you</h2>
                  <p style="margin-top:4px;">Tailored to your selected profile and prompt.</p>
                </div>
              </div>
              <div id="bestOpportunities" class="results-list">
                <div class="empty">Run an analysis from the homepage to populate recommendations.</div>
              </div>
            </div>

            <div class="card">
              <div class="section-head" style="margin-bottom:12px;">
                <div>
                  <h2 style="font-size:22px;">Procurement intelligence dashboard</h2>
                  <p style="margin-top:4px;">Sector and province patterns from the current feed.</p>
                </div>
              </div>
              <div class="insight-stack">
                <div class="insight-card">
                  <h3>Tenders by sector</h3>
                  <div id="sectorBars" class="bar-list">
                    <div class="empty">No chart data yet.</div>
                  </div>
                </div>
                <div class="insight-card">
                  <h3>Tenders by province</h3>
                  <div id="provinceBars" class="bar-list">
                    <div class="empty">No chart data yet.</div>
                  </div>
                </div>
                <div class="insight-card">
                  <h3>Trend insights</h3>
                  <div id="trendInsights" class="bar-list">
                    <div class="empty">No insights yet.</div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="section-head" style="margin-bottom:12px;">
              <div>
                <h2 style="font-size:22px;">Tender detail</h2>
                <p style="margin-top:4px;">Select an opportunity to inspect summary, requirements, risks, and advice.</p>
              </div>
            </div>
            <div id="detailPanel">
              <div class="empty">Select a recommended tender to inspect its intelligence breakdown.</div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>

  <script>
    let profiles = [];
    let activeProfileId = localStorage.getItem("tenderai_active_profile") || "";
    let latestScan = null;
    let latestTenders = [];
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

    function formatMoney(value) {
      return "R" + Number(value || 0).toLocaleString();
    }

    function formatCount(value) {
      return Number(value || 0).toLocaleString();
    }

    function bandClass(band) {
      if (band === "High fit") return "band high";
      if (band === "Medium fit") return "band medium";
      return "band low";
    }

    function showView(viewId) {
      document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
      document.querySelectorAll(".nav-btn").forEach(v => v.classList.remove("active"));
      document.getElementById(viewId).classList.add("active");
      document.querySelector(`.nav-btn[data-view="${viewId}"]`)?.classList.add("active");
      window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function setLoading(show, firstMessage) {
      const overlay = document.getElementById("loadingOverlay");
      overlay.style.display = show ? "flex" : "none";
      if (firstMessage) {
        document.getElementById("loadingStepText").textContent = firstMessage;
      }
    }

    function startLoadingTicker() {
      let idx = 0;
      document.getElementById("loadingStepText").textContent = loadingMessages[0];
      window.loadingTicker = setInterval(() => {
        idx = (idx + 1) % loadingMessages.length;
        document.getElementById("loadingStepText").textContent = loadingMessages[idx];
      }, 1300);
    }

    function stopLoadingTicker() {
      clearInterval(window.loadingTicker);
    }

    async function loadProfiles() {
      const res = await fetch("/api/profiles");
      const data = await res.json();
      profiles = data.profiles || [];

      if (!profiles.find(p => p.id === activeProfileId)) {
        activeProfileId = profiles[0]?.id || "";
        localStorage.setItem("tenderai_active_profile", activeProfileId);
      }

      renderProfiles();
      renderProfileSelects();
    }

    function renderProfileSelects() {
      const select = document.getElementById("home_profile_select");
      if (!profiles.length) {
        select.innerHTML = '<option value="">No profile uploaded</option>';
        return;
      }
      select.innerHTML = profiles.map(profile => `
        <option value="${profile.id}" ${profile.id === activeProfileId ? "selected" : ""}>
          ${escapeHtml(profile.name)}
        </option>
      `).join("");
    }

    function renderProfiles() {
      const wrap = document.getElementById("profilesList");
      if (!profiles.length) {
        wrap.innerHTML = '<div class="empty">No profiles uploaded yet.</div>';
        return;
      }

      wrap.innerHTML = profiles.map(profile => `
        <div class="profile-card ${profile.id === activeProfileId ? "active" : ""}">
          <div class="profile-title-row">
            <div>
              <div class="profile-title">${escapeHtml(profile.name)}</div>
              <div class="profile-meta">
                ${escapeHtml(profile.company_name || "Unknown company")}<br>
                B-BBEE: ${escapeHtml(profile.bbbee_level || "Unknown")} •
                Provinces: ${escapeHtml((profile.provinces || []).join(", ") || "Unknown")}
              </div>
            </div>
            <div class="band ${profile.id === activeProfileId ? "high" : "medium"}">
              ${profile.id === activeProfileId ? "Active" : "Available"}
            </div>
          </div>

          <div class="keyword-list">
            ${(profile.keywords || []).slice(0, 8).map(k => `<span class="chip">${escapeHtml(k)}</span>`).join("")}
          </div>

          <div class="profile-actions">
            <button class="btn btn-primary" onclick="setActiveProfile('${profile.id}')">Use this profile</button>
            <button class="btn btn-secondary" onclick="deleteProfile('${profile.id}')">Delete</button>
          </div>
        </div>
      `).join("");
    }

    async function uploadProfile() {
      const fileInput = document.getElementById("profile_upload");
      const file = fileInput.files[0];
      if (!file) return;

      setLoading(true, "Reading company profile...");
      try {
        const formData = new FormData();
        formData.append("profile_pdf", file);

        const res = await fetch("/api/profiles", {
          method: "POST",
          body: formData
        });
        const data = await res.json();

        if (data.status !== "ok") {
          alert(data.error || "Upload failed");
          return;
        }

        fileInput.value = "";
        await loadProfiles();
      } finally {
        setLoading(false);
      }
    }

    async function deleteProfile(profileId) {
      await fetch(`/api/profiles/${profileId}`, { method: "DELETE" });
      if (activeProfileId === profileId) {
        activeProfileId = "";
        localStorage.removeItem("tenderai_active_profile");
      }
      await loadProfiles();
    }

    function setActiveProfile(profileId) {
      activeProfileId = profileId;
      localStorage.setItem("tenderai_active_profile", profileId);
      renderProfiles();
      renderProfileSelects();
      showView("homeView");
    }

    function buildWhyMatched(t) {
      const items = [];

      if (t.matched_keywords && t.matched_keywords.length) {
        items.push("Matched capability keywords: " + t.matched_keywords.join(", "));
      }
      if (t.key_requirements && t.key_requirements.length) {
        items.push("Key requirements identified: " + t.key_requirements.slice(0, 3).join(", "));
      }
      if (t.preferential_model) {
        items.push("Estimated preference framework: " + t.preferential_model);
      }
      if (t.estimation_reason) {
        items.push("Tender value inference: " + t.estimation_reason);
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
              <div class="score-caption">Win probability proxy</div>
            </div>
          </div>

          <div class="mini" style="margin-bottom: 14px;">
            <h4>AI summary</h4>
            <div>${escapeHtml(safeText(t.ai_summary, "No summary available."))}</div>
          </div>

          <div class="two-col">
            <div class="mini">
              <h4>Why this matters</h4>
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

          <div class="actions">
            <button class="btn btn-primary" onclick='openTenderDetail(${JSON.stringify(t).replace(/'/g, "&#39;")})'>Open tender detail</button>
          </div>
        </div>
      `;
    }

    function renderBars(targetId, items) {
      const target = document.getElementById(targetId);
      if (!items || !items.length) {
        target.innerHTML = '<div class="empty">No data available.</div>';
        return;
      }

      const max = Math.max(...items.map(x => x.value), 1);
      target.innerHTML = items.map(item => `
        <div class="bar-row">
          <div class="bar-label">${escapeHtml(item.label)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${(item.value / max) * 100}%"></div></div>
          <div class="bar-value">${escapeHtml(item.value)}</div>
        </div>
      `).join("");
    }

    function renderTrendInsights(summary) {
      const target = document.getElementById("trendInsights");
      if (!summary) {
        target.innerHTML = '<div class="empty">No insights available.</div>';
        return;
      }

      target.innerHTML = `
        <div class="why-item"><div class="check">✓</div><div>${escapeHtml(summary.top_category_insight)}</div></div>
        <div class="why-item"><div class="check">✓</div><div>${escapeHtml(summary.top_province_insight)}</div></div>
        <div class="why-item"><div class="check">✓</div><div>${escapeHtml(summary.value_insight)}</div></div>
      `;
    }

    function renderFeedAnalytics(data) {
      document.getElementById("summaryGrid").style.display = "grid";
      document.getElementById("mTotal").textContent = formatCount(data.summary.returned_tenders);
      document.getElementById("mHigh").textContent = formatCount(data.summary.high_fit);
      document.getElementById("mSoon").textContent = formatCount(data.summary.closing_soon);
      document.getElementById("mAvgValue").textContent = formatMoney(data.summary.average_estimated_value_mid);

      renderBars("sectorBars", data.analytics.by_sector || []);
      renderBars("provinceBars", data.analytics.by_province || []);
      renderTrendInsights(data.analytics.trend_insights || {});
    }

    async function runPromptAnalysis() {
      const profileId = document.getElementById("home_profile_select").value;
      const prompt = document.getElementById("home_prompt").value.trim();

      if (!profileId) {
        alert("Upload a business profile first.");
        showView("profilesView");
        return;
      }

      setLoading(true);
      startLoadingTicker();

      try {
        const res = await fetch("/api/score", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            profile_id: profileId,
            prompt: prompt,
            date_from: document.getElementById("home_date_from").value,
            date_to: document.getElementById("home_date_to").value,
            page_number: 1,
            page_size: Number(document.getElementById("home_page_size").value)
          })
        });

        const data = await res.json();
        if (data.status !== "ok") {
          alert(data.error || "Analysis failed");
          return;
        }

        latestScan = data;
        latestTenders = data.tenders || [];
        renderFeedAnalytics(data);

        const bestOpportunities = document.getElementById("bestOpportunities");
        if (!latestTenders.length) {
          bestOpportunities.innerHTML = '<div class="empty">No tenders found for this scan.</div>';
        } else {
          bestOpportunities.innerHTML = latestTenders.map(renderTenderCard).join("");
          renderTenderDetail(latestTenders[0]);
        }

        showView("feedView");
      } finally {
        stopLoadingTicker();
        setLoading(false);
      }
    }

    function openTenderDetail(tender) {
      selectedTender = tender;
      renderTenderDetail(tender);
      showView("feedView");
      window.scrollTo({ top: 0, behavior: "smooth" });
    }

    async function getTenderAdvice() {
      if (!selectedTender) return;

      const res = await fetch("/api/advise", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tender: selectedTender,
          profile_id: activeProfileId
        })
      });

      const data = await res.json();
      const box = document.getElementById("adviceResult");

      if (data.status !== "ok") {
        box.innerHTML = '<div class="empty">Unable to generate advice.</div>';
        return;
      }

      box.innerHTML = `
        <div class="advice-box">
          <h3 style="margin-top:0;">Should you apply?</h3>
          <div class="why-list">
            <div class="why-item"><div class="check">✓</div><div>${escapeHtml(data.should_apply)}</div></div>
            <div class="why-item"><div class="check">✓</div><div>${escapeHtml(data.risk_comment)}</div></div>
            <div class="why-item"><div class="check">✓</div><div>${escapeHtml(data.competitor_assumption)}</div></div>
          </div>

          <h3 style="margin-top:16px;">Strategic advice</h3>
          <div class="why-list">
            ${data.advice.map(item => `
              <div class="why-item"><div class="check">✓</div><div>${escapeHtml(item)}</div></div>
            `).join("")}
          </div>

          <h3 style="margin-top:16px;">Required capabilities checklist</h3>
          <div class="keyword-list">
            ${data.required_capabilities.map(item => `<span class="chip">${escapeHtml(item)}</span>`).join("")}
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

      const res = await fetch("/api/service-request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      const data = await res.json();
      document.getElementById("serviceResult").textContent =
        data.status === "ok"
          ? "Request captured. Reference: " + data.reference
          : "Unable to submit request.";
    }

    function renderRequirementRows(requirements) {
      if (!requirements || !requirements.length) {
        return '<div class="empty">No structured requirements identified.</div>';
      }

      return requirements.map(item => `
        <div class="why-item">
          <div class="check">✓</div>
          <div><strong>${escapeHtml(item.name)}:</strong> ${escapeHtml(item.status)} — ${escapeHtml(item.comment)}</div>
        </div>
      `).join("");
    }

    function renderTenderDetail(t) {
      selectedTender = t;

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
          <h4>AI tender breakdown</h4>
          <div>${escapeHtml(safeText(t.ai_summary))}</div>
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
          <h3>Requirements and specifications</h3>
          <div class="mini">${renderRequirementRows(t.requirement_checks)}</div>
        </div>

        <div class="detail-grid">
          <div class="mini">
            <h4>Risk level</h4>
            <div class="value-big">${escapeHtml(safeText(t.risk_level))}</div>
            <div class="tender-meta">${escapeHtml(safeText(t.risk_reason))}</div>
          </div>
          <div class="mini">
            <h4>Estimated difficulty to win</h4>
            <div class="value-big">${escapeHtml(safeText(t.difficulty_level))}</div>
            <div class="tender-meta">${escapeHtml(safeText(t.difficulty_reason))}</div>
          </div>
        </div>

        <div class="detail-grid">
          <div class="mini">
            <h4>Preference framework estimate</h4>
            <div class="value-big">${escapeHtml(safeText(t.preferential_model))}</div>
            <div class="tender-meta">${escapeHtml(safeText(t.preference_comment))}</div>
          </div>
          <div class="mini">
            <h4>Bid-readiness</h4>
            <div class="value-big">${escapeHtml(safeText(t.bid_readiness))}</div>
            <div class="tender-meta">${escapeHtml(safeText(t.bid_readiness_comment))}</div>
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

    document.querySelectorAll(".nav-btn").forEach(btn => {
      btn.addEventListener("click", () => showView(btn.dataset.view));
    });

    document.getElementById("runPromptBtn").addEventListener("click", runPromptAnalysis);
    document.getElementById("goProfilesBtn").addEventListener("click", () => showView("profilesView"));
    document.getElementById("uploadProfileBtn").addEventListener("click", uploadProfile);
    document.getElementById("home_profile_select").addEventListener("change", (e) => {
      activeProfileId = e.target.value;
      localStorage.setItem("tenderai_active_profile", activeProfileId);
      renderProfiles();
    });

    loadProfiles();
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


def extract_company_name(text):
    patterns = [
        r"Legal Name\\s*:?\\s*(.+)",
        r"Company Name\\s*:?\\s*(.+)",
        r"Trading Name\\s*:?\\s*(.+)"
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).split("\\n")[0].strip()[:120]

    for line in text.splitlines():
        clean = line.strip()
        if 4 <= len(clean) <= 90 and not re.search(r"(summary|registration|supplier|report|database)", clean, re.I):
            return clean
    return "Unknown company"


def parse_profile_metadata(text):
    provinces = [
        "gauteng", "western cape", "eastern cape", "kwazulu-natal",
        "free state", "limpopo", "mpumalanga", "north west", "northern cape"
    ]

    bbbee_match = re.search(r"b[- ]?bbee(?:\\s+status\\s+level|\\s+level)?\\s*[:\\-]?\\s*(\\d)", text, re.I)
    cidb_match = re.search(r"cidb[^\\n]{0,30}?([1-9][A-Z]{1,2})", text, re.I)

    found_provinces = [p.title() for p in provinces if p in text.lower()]
    keywords = tokenize(text)[:25]

    return {
        "company_name": extract_company_name(text),
        "bbbee_level": bbbee_match.group(1) if bbbee_match else "Unknown",
        "cidb": cidb_match.group(1).upper() if cidb_match else "Unknown",
        "provinces": found_provinces,
        "keywords": keywords
    }


def infer_province(text):
    mapping = [
        ("Gauteng", ["gauteng", "johannesburg", "tshwane", "ekurhuleni"]),
        ("Western Cape", ["western cape", "cape town"]),
        ("Eastern Cape", ["eastern cape", "gqeberha", "east london", "mthatha"]),
        ("KwaZulu-Natal", ["kwazulu", "kzn", "durban", "pietermaritzburg"]),
        ("Free State", ["free state", "bloemfontein"]),
        ("Limpopo", ["limpopo", "polokwane", "vhembe"]),
        ("Mpumalanga", ["mpumalanga", "mbombela"]),
        ("North West", ["north west", "mahikeng", "potchefstroom"]),
        ("Northern Cape", ["northern cape", "kimberley"])
    ]
    lower = text.lower()
    for province, keys in mapping:
        if any(k in lower for k in keys):
            return province
    return "Unspecified"


def estimate_tender_value(title, description, category):
    text = f"{title} {description}".lower()

    low = 50000
    high = 300000
    confidence = "Low"
    reason = "Generic service estimate based on tender wording."

    if "generator" in text:
        low, high = 800000, 3000000
        confidence = "Medium"
        reason = "Generator installations typically fall within this range."
    elif any(k in text for k in ["construction", "building", "infrastructure"]):
        low, high = 500000, 5000000
        confidence = "Medium"
        reason = "Construction and infrastructure tenders are usually medium to high value."
    elif any(k in text for k in ["maintenance", "repair", "servicing"]):
        low, high = 100000, 1000000
        confidence = "Medium"
        reason = "Maintenance and repair contracts vary with scope and contract term."
    elif any(k in text for k in ["truck", "vehicle", "fire truck"]):
        low, high = 1000000, 8000000
        confidence = "High"
        reason = "Specialized vehicles are typically high-value procurements."
    elif any(k in text for k in ["server", "hardware", "storage", "backup appliance"]):
        low, high = 200000, 2000000
        confidence = "Medium"
        reason = "IT infrastructure procurement depends on scale and specification."
    elif category and category.lower() == "goods":
        low, high = 50000, 1000000
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
        ratio_low, ratio_high = 0.55, 0.82
        reason = "Generator supply and installation usually require significant equipment, transport, and technical delivery spend."
    elif any(k in text for k in ["construction", "building", "infrastructure"]):
        ratio_low, ratio_high = 0.60, 0.85
        reason = "Construction and infrastructure work generally requires substantial materials, labour, and site mobilisation."
    elif any(k in text for k in ["maintenance", "repair", "servicing"]):
        ratio_low, ratio_high = 0.40, 0.70
        reason = "Maintenance and repair contracts usually carry labour, tools, materials, and travel costs."
    elif any(k in text for k in ["truck", "vehicle", "fire truck"]):
        ratio_low, ratio_high = 0.70, 0.92
        reason = "Vehicle and specialized equipment tenders often require high capital outlay before delivery."
    elif any(k in text for k in ["server", "hardware", "storage", "backup appliance"]):
        ratio_low, ratio_high = 0.65, 0.88
        reason = "Hardware and IT supply contracts typically need significant procurement capital and logistics."
    elif category and category.lower() == "services":
        ratio_low, ratio_high = 0.30, 0.60
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


def infer_requirements(tender_text, profile_text):
    checks = []
    text = tender_text.lower()
    profile = profile_text.lower()

    rules = [
        ("CSD registration", ["csd"]),
        ("Tax compliance", ["tax"]),
        ("B-BBEE evidence", ["b-bbee", "bbbee"]),
        ("CIDB", ["cidb"]),
        ("Compulsory briefing", ["briefing", "site meeting"]),
        ("Local content forms", ["local content", "sbd 6.2"]),
        ("Professional registration", ["professional registration", "sacpcmp", "ecsa", "preng"]),
        ("Health and safety", ["safety", "ohs", "health and safety"]),
    ]

    for name, keys in rules:
        if any(k in text for k in keys):
            if any(k in profile for k in keys if k not in ["briefing", "site meeting", "local content", "sbd 6.2"]):
                status = "Likely met"
                comment = "Related evidence appears in the profile."
            elif name in ["Compulsory briefing", "Local content forms"]:
                status = "Action required"
                comment = "TenderAI detected this in the tender. Confirm attendance/forms during bid preparation."
            else:
                status = "Check"
                comment = "TenderAI detected the requirement but could not confirm evidence from the profile."
            checks.append({
                "name": name,
                "status": status,
                "comment": comment
            })

    return checks[:8]


def build_ai_summary(title, description, buyer, category):
    desc = (description or "").strip()
    if desc:
        short_desc = desc[:240] + ("..." if len(desc) > 240 else "")
        return f"{title} issued by {buyer} appears to be a {category.lower() if category else 'procurement'} opportunity focused on: {short_desc}"
    return f"{title} issued by {buyer} appears to be a {category.lower() if category else 'procurement'} opportunity with limited public description."


def infer_risk_and_difficulty(description, requirement_checks):
    text = (description or "").lower()
    risk_score = 0
    diff_score = 0

    if any(k in text for k in ["compulsory briefing", "site meeting", "mandatory", "compulsory"]):
        risk_score += 2
        diff_score += 1
    if any(k in text for k in ["cidb", "local content", "electrical", "generator", "specialized", "specialised"]):
        risk_score += 2
        diff_score += 2
    if any(k in text for k in ["construction", "infrastructure", "server", "hardware", "truck"]):
        diff_score += 2
    if len(requirement_checks) >= 4:
        risk_score += 1
        diff_score += 1

    if risk_score >= 4:
        risk_level = "High"
        risk_reason = "The tender appears to include multiple conditions, specialized requirements, or mandatory bid risks."
    elif risk_score >= 2:
        risk_level = "Medium"
        risk_reason = "The tender has some conditions that may increase compliance or delivery risk."
    else:
        risk_level = "Low"
        risk_reason = "The tender appears relatively straightforward based on the available notice content."

    if diff_score >= 4:
        difficulty_level = "High"
        difficulty_reason = "The tender likely requires stronger capability proof and tighter delivery planning."
    elif diff_score >= 2:
        difficulty_level = "Medium"
        difficulty_reason = "The tender seems achievable but may require stronger documentation and positioning."
    else:
        difficulty_level = "Low"
        difficulty_reason = "The tender appears comparatively accessible based on the available text."

    return risk_level, risk_reason, difficulty_level, difficulty_reason


def infer_preference_model(value_mid, profile_text):
    model = "Estimated 80/20" if value_mid <= 50000000 else "Estimated 90/10"
    if "bbbee" in profile_text.lower() or "b-bbee" in profile_text.lower():
        comment = "Profile appears to include B-BBEE-related evidence, which may support specific-goal scoring if the tender documents allow it."
    else:
        comment = "TenderAI could not confirm B-BBEE-specific evidence from the profile. Confirm the tender's specific goals and proof rules."
    return model, comment


def calculate_fit(profile_keywords, prompt_keywords, tender_text, category, requirement_checks):
    tokens = set(tokenize(tender_text))
    combined = list(dict.fromkeys((profile_keywords or []) + (prompt_keywords or [])))
    matched = sorted(set(combined).intersection(tokens))

    base_score = (len(matched) / max(len(set(combined)), 1)) * 100
    bonus = 0

    if category and category.lower() in ["works", "services"]:
        bonus += 10

    intent_keywords = ["installation", "maintenance", "repair", "construction", "electrical", "generator", "supply"]
    bonus += sum(1 for k in intent_keywords if k in tokens) * 4

    if len(requirement_checks) >= 3:
        bonus += 4

    score = round(min(base_score + bonus, 100), 1)

    if score >= 70:
        band = "High fit"
    elif score >= 40:
        band = "Medium fit"
    else:
        band = "Low fit"

    return score, band, matched


def compute_bid_readiness(requirement_checks):
    if not requirement_checks:
        return "Early-stage", "Limited tender-document requirements were detected from the available notice text."

    action_required = sum(1 for r in requirement_checks if r["status"] == "Action required")
    checks = sum(1 for r in requirement_checks if r["status"] == "Check")

    if action_required == 0 and checks <= 1:
        return "Strong", "The profile appears broadly aligned with the detected requirement set."
    if action_required <= 1 and checks <= 3:
        return "Moderate", "Some requirements need confirmation or bid preparation work."
    return "Needs work", "Several requirements or actions need attention before submission."


def enrich_tender(item, profile=None, prompt=""):
    tender = item.get("tender", {}) if isinstance(item, dict) else {}
    buyer = item.get("buyer", {}) if isinstance(item, dict) else {}
    tender_period = tender.get("tenderPeriod", {}) if isinstance(tender, dict) else {}
    value = tender.get("value", {}) if isinstance(tender, dict) else {}

    description = tender.get("description", "") or ""
    title = tender.get("title", "") or ""
    buyer_name = buyer.get("name", "") or ""
    category = tender.get("mainProcurementCategory", "") or ""
    province = infer_province(f"{buyer_name} {description}")
    combined_text = f"{title} {description} {buyer_name} {category}"

    profile_text = profile["text"] if profile else ""
    profile_keywords = profile["meta"]["keywords"] if profile else []
    prompt_keywords = tokenize(prompt)[:10]
    requirement_checks = infer_requirements(combined_text, profile_text)
    fit_score, fit_band, matched_keywords = calculate_fit(
        profile_keywords=profile_keywords,
        prompt_keywords=prompt_keywords,
        tender_text=combined_text,
        category=category,
        requirement_checks=requirement_checks
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

    execution = estimate_execution_investment(
        title=title,
        description=description,
        category=category,
        estimated_low=estimated_value_low,
        estimated_high=estimated_value_high
    )

    ai_summary = build_ai_summary(title, description, buyer_name, category)
    risk_level, risk_reason, difficulty_level, difficulty_reason = infer_risk_and_difficulty(description, requirement_checks)
    preferential_model, preference_comment = infer_preference_model(estimated_value_mid, profile_text)
    bid_readiness, bid_readiness_comment = compute_bid_readiness(requirement_checks)

    win_probability = max(10, min(92, round(fit_score - (5 if risk_level == "High" else 0) + (4 if bid_readiness == "Strong" else 0), 0)))

    return {
        "ocid": item.get("ocid") if isinstance(item, dict) else None,
        "title": title,
        "buyer": buyer_name,
        "description": description,
        "status": tender.get("status"),
        "category": category,
        "province": province,
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
        "win_probability": win_probability,
        "matched_keywords": matched_keywords,
        "execution_investment_low": execution["execution_investment_low"],
        "execution_investment_high": execution["execution_investment_high"],
        "execution_investment_mid": execution["execution_investment_mid"],
        "execution_investment_display": execution["execution_investment_display"],
        "execution_investment_reason": execution["execution_investment_reason"],
        "ai_summary": ai_summary,
        "key_requirements": [r["name"] for r in requirement_checks],
        "requirement_checks": requirement_checks,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "difficulty_level": difficulty_level,
        "difficulty_reason": difficulty_reason,
        "preferential_model": preferential_model,
        "preference_comment": preference_comment,
        "bid_readiness": bid_readiness,
        "bid_readiness_comment": bid_readiness_comment,
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


def build_analytics(tenders):
    sector_counter = Counter([t.get("category") or "Unspecified" for t in tenders])
    province_counter = Counter([t.get("province") or "Unspecified" for t in tenders])

    by_sector = [{"label": k, "value": v} for k, v in sector_counter.most_common(6)]
    by_province = [{"label": k, "value": v} for k, v in province_counter.most_common(6)]

    avg_value = round(sum(t.get("estimated_value_mid", 0) for t in tenders) / max(len(tenders), 1), 0)

    top_category = by_sector[0]["label"] if by_sector else "No dominant sector"
    top_category_share = round((by_sector[0]["value"] / max(len(tenders), 1)) * 100, 0) if by_sector else 0

    top_province = by_province[0]["label"] if by_province else "No dominant province"
    top_province_share = round((by_province[0]["value"] / max(len(tenders), 1)) * 100, 0) if by_province else 0

    return {
        "by_sector": by_sector,
        "by_province": by_province,
        "trend_insights": {
            "top_category_insight": f"{top_category} accounts for roughly {top_category_share}% of the current opportunity set.",
            "top_province_insight": f"{top_province} contributes roughly {top_province_share}% of the observed opportunities.",
            "value_insight": f"The average estimated contract value in the current scan is about R{avg_value:,.0f}."
        }
    }


def count_closing_soon(tenders):
    count = 0
    now = datetime.now(timezone.utc)
    for t in tenders:
        close_date = t.get("close_date")
        if not close_date:
            continue
        try:
            dt = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
            delta_days = (dt - now).days
            if 0 <= delta_days <= 7:
                count += 1
        except Exception:
            pass
    return count


@app.get("/api/profiles")
def api_profiles():
    profiles = list(PROFILE_STORE.values())
    return jsonify({
        "status": "ok",
        "profiles": [
            {
                "id": p["id"],
                "name": p["name"],
                "company_name": p["meta"]["company_name"],
                "bbbee_level": p["meta"]["bbbee_level"],
                "cidb": p["meta"]["cidb"],
                "provinces": p["meta"]["provinces"],
                "keywords": p["meta"]["keywords"][:12],
                "uploaded_at": p["uploaded_at"]
            }
            for p in profiles
        ]
    })


@app.post("/api/profiles")
def api_upload_profile():
    if "profile_pdf" not in request.files:
        return jsonify({"status": "error", "error": "No PDF uploaded"}), 400

    file = request.files["profile_pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"status": "error", "error": "Upload a PDF file"}), 400

    text = extract_pdf_text(file)
    meta = parse_profile_metadata(text)

    profile_id = str(uuid.uuid4())
    PROFILE_STORE[profile_id] = {
        "id": profile_id,
        "name": file.filename,
        "text": text,
        "meta": meta,
        "uploaded_at": datetime.now(timezone.utc).isoformat()
    }

    return jsonify({"status": "ok", "profile_id": profile_id})


@app.delete("/api/profiles/<profile_id>")
def api_delete_profile(profile_id):
    PROFILE_STORE.pop(profile_id, None)
    return jsonify({"status": "ok"})


@app.post("/api/score")
def api_score():
    body = request.get_json(silent=True) or {}
    profile_id = body.get("profile_id")
    prompt = body.get("prompt", "")
    date_from = body.get("date_from", "2026-01-01")
    date_to = body.get("date_to", "2026-03-17")
    page_number = int(body.get("page_number", 1))
    page_size = int(body.get("page_size", 10))

    profile = PROFILE_STORE.get(profile_id)
    if not profile:
        return jsonify({"status": "error", "error": "Profile not found"}), 404

    try:
        releases, params = fetch_tenders(date_from, date_to, page_number, page_size)
        tenders = [enrich_tender(item, profile=profile, prompt=prompt) for item in releases]
        tenders = sorted(tenders, key=lambda x: (x["fit_score"], x["win_probability"]), reverse=True)

        analytics = build_analytics(tenders)

        return jsonify({
            "status": "ok",
            "profile_name": profile["name"],
            "prompt": prompt,
            "summary": {
                "returned_tenders": len(tenders),
                "high_fit": sum(1 for t in tenders if t["fit_band"] == "High fit"),
                "medium_fit": sum(1 for t in tenders if t["fit_band"] == "Medium fit"),
                "low_fit": sum(1 for t in tenders if t["fit_band"] == "Low fit"),
                "closing_soon": count_closing_soon(tenders),
                "average_estimated_value_mid": round(sum(t["estimated_value_mid"] for t in tenders) / max(len(tenders), 1), 0)
            },
            "analytics": analytics,
            "tenders": tenders
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.post("/api/advise")
def api_advise():
    body = request.get_json(silent=True) or {}
    tender = body.get("tender", {}) or {}
    profile_id = body.get("profile_id")
    profile = PROFILE_STORE.get(profile_id)

    profile_text = profile["text"] if profile else ""
    title = str(tender.get("title", ""))
    description = str(tender.get("description", ""))
    category = str(tender.get("category", ""))
    matched_keywords = tender.get("matched_keywords", []) or []

    advice = []
    required_capabilities = tender.get("key_requirements", []) or []

    if not matched_keywords:
        advice.append("Sharpen your capability statement so it mirrors the tender language more directly.")
    else:
        advice.append("Reflect the strongest matched keywords in your executive summary, methodology, and pricing narrative.")

    if "generator" in f"{title} {description}".lower():
        advice.append("Include generator-specific references, electrical compliance evidence, and technical delivery capability.")
        required_capabilities.extend(["Electrical compliance", "Generator installation references", "Technical delivery methodology"])

    if category.lower() == "works":
        advice.append("Show site methodology, supervision structure, safety planning, and mobilisation readiness.")
        required_capabilities.extend(["Health and safety file", "Site mobilisation plan", "Project supervision structure"])

    if category.lower() == "services":
        advice.append("Show turnaround times, staffing depth, response processes, and geographic operating capacity.")
        required_capabilities.extend(["Service delivery plan", "Operational response plan", "Staffing capacity"])

    if "bbbee" not in profile_text.lower() and "b-bbee" not in profile_text.lower():
        advice.append("Confirm whether you have current B-BBEE evidence available if the tender allocates points to specific goals.")

    if tender.get("bid_readiness") == "Needs work":
        advice.append("Do not treat this as submission-ready yet. Close the missing evidence gaps before committing bid resources.")

    should_apply = "Apply if you can close the highlighted compliance and documentation gaps quickly." if tender.get("fit_score", 0) >= 55 else "Monitor rather than apply immediately unless you have stronger supporting evidence than TenderAI could detect."
    risk_comment = f"Current risk view: {tender.get('risk_level', 'Unknown')} risk. {tender.get('risk_reason', '')}"
    competitor_assumption = "Expect competition from suppliers with stronger reference portfolios, complete compliance packs, and closer scope alignment."

    required_capabilities = list(dict.fromkeys(required_capabilities))[:10]

    return jsonify({
        "status": "ok",
        "should_apply": should_apply,
        "risk_comment": risk_comment,
        "competitor_assumption": competitor_assumption,
        "advice": advice,
        "required_capabilities": required_capabilities
    })


@app.post("/api/service-request")
def api_service_request():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "Unknown")
    company = body.get("company", "Unknown")
    tender = body.get("tender", {}) or {}
    reference = f"TAI-{abs(hash((name, company, tender.get('ocid', 'NA')))) % 1000000:06d}"

    return jsonify({
        "status": "ok",
        "reference": reference
    })
