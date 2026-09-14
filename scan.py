#!/usr/bin/env python3
"""Mac Storage Cleaner — 扫描磁盘空间，生成交互式 HTML 报告，支持清理。"""

import argparse
import http.server
import json
import os
import shutil
import sys
import time
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ─── 文件分类规则 ───────────────────────────────────────────────

CATEGORIES = {
    "日志文件": {
        "extensions": {".log", ".log.gz", ".log.bz2", ".log.xz", ".log.zst"},
        "path_patterns": ["/var/log", "/Library/Logs", "logs/"],
        "color": "#e74c3c",
    },
    "缓存文件": {
        "extensions": set(),
        "path_patterns": [
            "/Library/Caches",
            "Cache",
            "cache",
            ".cache",
            "__pycache__",
            ".pytest_cache",
        ],
        "color": "#e67e22",
    },
    "开发构建产物": {
        "extensions": {".o", ".a", ".dylib", ".class", ".pyc", ".pyo", ".wasm"},
        "path_patterns": [
            "node_modules",
            ".gradle",
            "build/",
            "dist/",
            "target/",
            ".next",
            ".nuxt",
            "Pods",
            "DerivedData",
            ".build",
        ],
        "color": "#9b59b6",
    },
    "下载文件": {
        "extensions": set(),
        "path_patterns": ["Downloads"],
        "color": "#3498db",
    },
    "应用数据": {
        "extensions": set(),
        "path_patterns": ["/Library/Application Support", "/Library/Containers"],
        "color": "#1abc9c",
    },
    "废纸篓": {
        "extensions": set(),
        "path_patterns": [".Trash"],
        "color": "#7f8c8d",
    },
    "压缩包": {
        "extensions": {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".dmg", ".iso", ".pkg"},
        "color": "#2ecc71",
    },
    "视频文件": {
        "extensions": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"},
        "color": "#f39c12",
    },
    "Docker 数据": {
        "extensions": set(),
        "path_patterns": ["Docker/", "docker/", ".docker"],
        "color": "#0db7ed",
    },
    "Git 仓库": {
        "extensions": set(),
        "path_patterns": [".git/"],
        "color": "#f05032",
    },
}

SKIP_DIRS = {
    "/dev", "/proc", "/sys", "/private/var/vm",
    "/System/Volumes/Data/.Spotlight-V100",
    "/System/Volumes/VM",
    "/.Spotlight-V100",
    "/.fseventsd",
    "/System/Volumes/Preboot",
    "/System/Volumes/xarts",
    "/System/Volumes/iSCPreboot",
    "/System/Volumes/Hardware",
}


def classify_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    for cat_name, rules in CATEGORIES.items():
        if ext in rules.get("extensions", set()):
            return cat_name
        for pattern in rules.get("path_patterns", []):
            if pattern in filepath:
                return cat_name
    return None


# ─── 扫描引擎 ───────────────────────────────────────────────────

def format_size(size_bytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def scan_directory(path, max_depth=6, min_size=1024 * 1024, progress_callback=None):
    category_stats = defaultdict(lambda: {"size": 0, "count": 0, "files": []})
    large_files = []

    def _scan(dir_path, depth):
        if depth > max_depth:
            try:
                total = _fast_du(dir_path)
            except (PermissionError, OSError):
                total = 0
            return {"name": os.path.basename(dir_path), "path": dir_path, "size": total, "children": [], "truncated": True}

        if dir_path in SKIP_DIRS:
            return None

        node = {"name": os.path.basename(dir_path) or dir_path, "path": dir_path, "size": 0, "children": []}
        try:
            entries = list(os.scandir(dir_path))
        except (PermissionError, OSError):
            return None

        for entry in entries:
            try:
                full_path = entry.path
                if entry.is_symlink():
                    continue

                if entry.is_dir(follow_symlinks=False):
                    if full_path in SKIP_DIRS:
                        continue
                    child = _scan(full_path, depth + 1)
                    if child and child["size"] > 0:
                        node["children"].append(child)
                        node["size"] += child["size"]
                elif entry.is_file(follow_symlinks=False):
                    try:
                        st = entry.stat(follow_symlinks=False)
                        fsize = st.st_blocks * 512 if hasattr(st, 'st_blocks') else st.st_size
                    except (PermissionError, OSError):
                        continue

                    node["size"] += fsize

                    cat = classify_file(full_path)
                    if cat:
                        category_stats[cat]["size"] += fsize
                        category_stats[cat]["count"] += 1
                        if fsize > min_size:
                            category_stats[cat]["files"].append({"path": full_path, "size": fsize})

                    if fsize > 50 * 1024 * 1024:
                        large_files.append({"path": full_path, "size": fsize, "category": cat or "其他"})

            except (PermissionError, OSError):
                continue

        if progress_callback and depth <= 1:
            progress_callback(dir_path, node["size"])

        node["children"].sort(key=lambda c: c["size"], reverse=True)
        return node

    root = _scan(path, 0)
    if root is None:
        root = {"name": path, "path": path, "size": 0, "children": []}

    for cat_data in category_stats.values():
        cat_data["files"].sort(key=lambda f: f["size"], reverse=True)
        cat_data["files"] = cat_data["files"][:50]

    large_files.sort(key=lambda f: f["size"], reverse=True)
    large_files = large_files[:100]

    return root, dict(category_stats), large_files


def _fast_du(path):
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    total += st.st_blocks * 512 if hasattr(st, 'st_blocks') else st.st_size
                elif entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                    total += _fast_du(entry.path)
            except (PermissionError, OSError):
                pass
    except (PermissionError, OSError):
        pass
    return total


def _prune_tree(node, min_size_bytes=512 * 1024):
    if not node.get("children"):
        return node
    pruned_children = []
    other_size = 0
    for child in node["children"]:
        if child["size"] >= min_size_bytes:
            pruned_children.append(_prune_tree(child, min_size_bytes))
        else:
            other_size += child["size"]
    if other_size > 0:
        pruned_children.append({"name": "(其他小文件)", "path": "", "size": other_size, "children": []})
    node["children"] = pruned_children
    return node


# ─── HTML 生成 ──────────────────────────────────────────────────

def generate_html(tree_data, category_stats, large_files, scan_path, output_path):
    import html as html_mod
    cat_colors = {}
    for cat_name, rules in CATEGORIES.items():
        cat_colors[cat_name] = rules.get("color", "#95a5a6")

    html_content = HTML_TEMPLATE.replace("__TREE_DATA__", json.dumps(tree_data, ensure_ascii=False))
    html_content = html_content.replace("__CATEGORY_STATS__", json.dumps(category_stats, ensure_ascii=False))
    html_content = html_content.replace("__LARGE_FILES__", json.dumps(large_files, ensure_ascii=False))
    html_content = html_content.replace("__CATEGORY_COLORS__", json.dumps(cat_colors, ensure_ascii=False))
    html_content = html_content.replace("__SCAN_PATH__", html_mod.escape(scan_path))
    html_content = html_content.replace("__SCAN_TIME__", html_mod.escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mac Storage Cleaner</title>
<style>
:root {
  --bg: #1a1a2e; --surface: #16213e; --surface2: #0f3460;
  --text: #e0e0e0; --text2: #a0a0b0; --accent: #e94560;
  --border: #2a2a4a; --success: #2ecc71; --danger: #e74c3c;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', sans-serif; background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
header { padding: 12px 20px; background: var(--surface); border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 16px; flex-shrink: 0; }
header h1 { font-size: 18px; font-weight: 600; }
header .meta { font-size: 12px; color: var(--text2); }
.tabs { display: flex; gap: 2px; padding: 0 20px; background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0; }
.tab { padding: 10px 20px; cursor: pointer; font-size: 13px; color: var(--text2); border-bottom: 2px solid transparent; transition: all .2s; user-select: none; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.main { flex: 1; display: flex; overflow: hidden; }
.panel { flex: 1; overflow: hidden; display: none; }
.panel.active { display: flex; flex-direction: column; }

.breadcrumb { padding: 8px 16px; font-size: 12px; color: var(--text2); background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0; }
.breadcrumb span { cursor: pointer; color: var(--accent); }
.breadcrumb span:hover { text-decoration: underline; }
#treemap-container { flex: 1; position: relative; overflow: hidden; }
canvas#treemap { width: 100%; height: 100%; display: block; }
.tooltip { position: fixed; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; font-size: 12px; pointer-events: none; z-index: 100; max-width: 400px; box-shadow: 0 4px 20px rgba(0,0,0,.5); }

.categories-panel { padding: 20px; overflow-y: auto; flex: 1; }
.cat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; margin-bottom: 24px; }
.cat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; cursor: pointer; transition: all .2s; }
.cat-card:hover { border-color: var(--accent); transform: translateY(-2px); }
.cat-card .cat-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.cat-card .cat-name { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px; }
.cat-card .cat-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.cat-card .cat-size { font-size: 18px; font-weight: 700; color: var(--accent); }
.cat-card .cat-count { font-size: 12px; color: var(--text2); }
.cat-card .cat-bar { height: 4px; background: var(--border); border-radius: 2px; margin-top: 8px; overflow: hidden; }
.cat-card .cat-bar-fill { height: 100%; border-radius: 2px; transition: width .5s; }
.cat-detail { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-top: 12px; display: none; }
.cat-detail.open { display: block; }
.file-list { list-style: none; }
.file-item { display: flex; align-items: center; gap: 10px; padding: 8px 4px; border-bottom: 1px solid var(--border); font-size: 13px; }
.file-item:last-child { border-bottom: none; }
.file-item input[type="checkbox"] { accent-color: var(--accent); width: 16px; height: 16px; cursor: pointer; flex-shrink: 0; }
.file-item .fi-path { flex: 1; word-break: break-all; color: var(--text2); }
.file-item .fi-size { white-space: nowrap; font-weight: 600; color: var(--accent); min-width: 80px; text-align: right; }
.file-item .fi-cat { font-size: 11px; padding: 2px 6px; border-radius: 4px; white-space: nowrap; }

.large-panel { padding: 20px; overflow-y: auto; flex: 1; }
.large-panel h2 { font-size: 16px; margin-bottom: 16px; }

.cleanup-bar { padding: 10px 20px; background: var(--surface2); border-top: 1px solid var(--border); display: flex; align-items: center; gap: 16px; flex-shrink: 0; }
.cleanup-bar .sel-count { font-size: 13px; color: var(--text2); }
.cleanup-bar .sel-size { font-size: 15px; font-weight: 700; color: var(--accent); }
.cleanup-bar button { padding: 8px 24px; border: none; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all .2s; }
.btn-danger { background: var(--danger); color: white; }
.btn-danger:hover { background: #c0392b; }
.btn-danger:disabled { opacity: .4; cursor: not-allowed; }
.btn-clear { background: transparent; color: var(--text2); border: 1px solid var(--border) !important; }
.spacer { flex: 1; }

.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 200; display: flex; align-items: center; justify-content: center; }
.modal { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; max-width: 500px; width: 90%; max-height: 70vh; overflow-y: auto; }
.modal h3 { margin-bottom: 12px; }
.modal .modal-list { max-height: 200px; overflow-y: auto; margin: 12px 0; font-size: 12px; color: var(--text2); }
.modal .modal-list div { padding: 4px 0; border-bottom: 1px solid var(--border); word-break: break-all; }
.modal .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
.modal button { padding: 8px 20px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; }
.modal .btn-cancel { background: var(--border); color: var(--text); }
.hidden { display: none !important; }
</style>
</head>
<body>

<header>
  <h1>Mac Storage Cleaner</h1>
  <div class="meta">扫描路径: __SCAN_PATH__ | 扫描时间: __SCAN_TIME__</div>
</header>

<div class="tabs">
  <div class="tab active" data-tab="treemap">空间地图</div>
  <div class="tab" data-tab="categories">文件分类</div>
  <div class="tab" data-tab="large">大文件</div>
</div>

<div class="main">
  <div class="panel active" id="panel-treemap">
    <div class="breadcrumb" id="breadcrumb"></div>
    <div id="treemap-container"><canvas id="treemap"></canvas></div>
  </div>
  <div class="panel" id="panel-categories">
    <div class="categories-panel">
      <div class="cat-grid" id="cat-grid"></div>
      <div class="cat-detail" id="cat-detail"></div>
    </div>
  </div>
  <div class="panel" id="panel-large">
    <div class="large-panel">
      <h2>大文件排行 (&gt;50MB)</h2>
      <div class="file-list" id="large-list"></div>
    </div>
  </div>
</div>

<div class="cleanup-bar">
  <span class="sel-count" id="sel-count">已选 0 项</span>
  <span class="sel-size" id="sel-size">0 B</span>
  <span class="spacer"></span>
  <button class="btn-clear" id="btn-clear">清空选择</button>
  <button class="btn-danger" id="btn-delete" disabled>移到废纸篓</button>
</div>

<div class="modal-overlay hidden" id="modal-overlay"></div>

<div class="tooltip hidden" id="tooltip"></div>

<script>
const TREE = __TREE_DATA__;
const CAT_STATS = __CATEGORY_STATS__;
const LARGE_FILES = __LARGE_FILES__;
const CAT_COLORS = __CATEGORY_COLORS__;

const selected = new Map();
let currentNode = TREE;
let navStack = [];
let hoveredRect = null;
let treemapRects = [];

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function fmtSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b/1048576).toFixed(1) + ' MB';
  if (b < 1099511627776) return (b/1073741824).toFixed(1) + ' GB';
  return (b/1099511627776).toFixed(1) + ' TB';
}

function createEl(tag, attrs, children) {
  const el = document.createElement(tag);
  if (attrs) Object.entries(attrs).forEach(([k,v]) => {
    if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  });
  if (children) {
    if (typeof children === 'string') el.textContent = children;
    else if (Array.isArray(children)) children.forEach(c => { if (c) el.appendChild(c); });
    else el.appendChild(children);
  }
  return el;
}

// ─── Tabs ──────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'treemap') requestAnimationFrame(drawTreemap);
  });
});

// ─── Treemap ───────────────────────────────────
const canvas = document.getElementById('treemap');
const ctx = canvas.getContext('2d');
const tooltip = document.getElementById('tooltip');

function resizeCanvas() {
  const container = document.getElementById('treemap-container');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = container.clientWidth * dpr;
  canvas.height = container.clientHeight * dpr;
  ctx.scale(dpr, dpr);
  canvas.style.width = container.clientWidth + 'px';
  canvas.style.height = container.clientHeight + 'px';
}

const palette = [
  '#e94560','#0f3460','#533483','#e67e22','#2ecc71',
  '#3498db','#9b59b6','#1abc9c','#f39c12','#e74c3c',
  '#00b894','#6c5ce7','#fd79a8','#00cec9','#fab1a0',
];

function squarify(children, x, y, w, h) {
  const rects = [];
  if (!children.length || w <= 0 || h <= 0) return rects;
  const total = children.reduce((s, c) => s + c.size, 0);
  if (total <= 0) return rects;

  let items = children.map((c, i) => ({...c, _idx: i}));
  let cx = x, cy = y, cw = w, ch = h;

  while (items.length > 0) {
    const side = ch > cw ? cw : ch;
    const totalRemaining = items.reduce((s, c) => s + c.size, 0);
    let row = [items[0]];
    let bestAspect = worst(row, side, totalRemaining, cw * ch);

    for (let i = 1; i < items.length; i++) {
      const tryRow = [...row, items[i]];
      const tryAspect = worst(tryRow, side, totalRemaining, cw * ch);
      if (tryAspect < bestAspect) {
        row = tryRow;
        bestAspect = tryAspect;
      } else break;
    }

    const rowSum = row.reduce((s, c) => s + c.size, 0);
    const rowFrac = rowSum / totalRemaining;
    const vertical = ch > cw;

    if (vertical) {
      const rowH = ch * rowFrac;
      let rx = cx;
      for (const item of row) {
        const itemW = cw * (item.size / rowSum);
        rects.push({x: rx, y: cy, w: itemW, h: rowH, node: item});
        rx += itemW;
      }
      cy += rowH; ch -= rowH;
    } else {
      const rowW = cw * rowFrac;
      let ry = cy;
      for (const item of row) {
        const itemH = ch * (item.size / rowSum);
        rects.push({x: cx, y: ry, w: rowW, h: itemH, node: item});
        ry += itemH;
      }
      cx += rowW; cw -= rowW;
    }
    items = items.slice(row.length);
  }
  return rects;
}

function worst(row, side, totalAll, totalArea) {
  let mx = 0;
  const rowSum = row.reduce((s, c) => s + c.size, 0);
  for (const c of row) {
    const area = (c.size / totalAll) * totalArea;
    const rowLen = (rowSum / totalAll) * totalArea / side;
    if (rowLen === 0) continue;
    const other = area / rowLen;
    const aspect = Math.max(rowLen / other, other / rowLen);
    mx = Math.max(mx, aspect);
  }
  return mx;
}

function drawTreemap() {
  resizeCanvas();
  const container = document.getElementById('treemap-container');
  const w = container.clientWidth;
  const h = container.clientHeight;
  ctx.clearRect(0, 0, w, h);

  const children = currentNode.children || [];
  if (!children.length) {
    ctx.fillStyle = '#555';
    ctx.font = '14px -apple-system';
    ctx.textAlign = 'center';
    ctx.fillText('此目录下无可显示的子项', w/2, h/2);
    treemapRects = [];
    return;
  }

  treemapRects = squarify(children, 2, 2, w - 4, h - 4);

  treemapRects.forEach((r, i) => {
    const isSelected = selected.has(r.node.path);
    const color = palette[i % palette.length];

    ctx.fillStyle = color;
    ctx.globalAlpha = isSelected ? 1 : 0.75;
    roundRect(ctx, r.x + 1, r.y + 1, r.w - 2, r.h - 2, 4);
    ctx.fill();
    ctx.globalAlpha = 1;

    if (isSelected) {
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 2;
      roundRect(ctx, r.x + 1, r.y + 1, r.w - 2, r.h - 2, 4);
      ctx.stroke();
    }

    if (r.w > 50 && r.h > 30) {
      ctx.fillStyle = '#fff';
      ctx.font = 'bold 12px -apple-system';
      ctx.textAlign = 'left';
      ctx.fillText(truncText(r.node.name, r.w - 12), r.x + 6, r.y + 18);
      ctx.font = '11px -apple-system';
      ctx.fillStyle = 'rgba(255,255,255,.8)';
      ctx.fillText(fmtSize(r.node.size), r.x + 6, r.y + 33);
    } else if (r.w > 30 && r.h > 18) {
      ctx.fillStyle = '#fff';
      ctx.font = '10px -apple-system';
      ctx.textAlign = 'left';
      ctx.fillText(truncText(r.node.name, r.w - 8), r.x + 4, r.y + 13);
    }
  });

  updateBreadcrumb();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.arcTo(x + w, y, x + w, y + r, r);
  ctx.lineTo(x + w, y + h - r);
  ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
  ctx.lineTo(x + r, y + h);
  ctx.arcTo(x, y + h, x, y + h - r, r);
  ctx.lineTo(x, y + r);
  ctx.arcTo(x, y, x + r, y, r);
  ctx.closePath();
}

function truncText(text, maxW) {
  if (ctx.measureText(text).width <= maxW) return text;
  while (text.length > 1 && ctx.measureText(text + '...').width > maxW) text = text.slice(0, -1);
  return text + '...';
}

function updateBreadcrumb() {
  const bc = document.getElementById('breadcrumb');
  bc.textContent = '';
  const parts = [{node: TREE, label: TREE.name}];
  for (const n of navStack) parts.push({node: n, label: n.name});
  parts.forEach((p, i) => {
    if (i < parts.length - 1) {
      const span = createEl('span', {onclick: () => navigateTo(i)}, p.label);
      bc.appendChild(span);
      bc.appendChild(document.createTextNode(' / '));
    } else {
      const strong = createEl('strong', null, p.label + ' (' + fmtSize(p.node.size) + ')');
      bc.appendChild(strong);
    }
  });
}

function navigateTo(idx) {
  if (idx === 0) { currentNode = TREE; navStack = []; }
  else { navStack = navStack.slice(0, idx); currentNode = navStack[navStack.length - 1]; }
  drawTreemap();
}

canvas.addEventListener('mousemove', (e) => {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  let found = null;
  for (const r of treemapRects) {
    if (mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + r.h) found = r;
  }
  if (found) {
    hoveredRect = found;
    tooltip.classList.remove('hidden');
    tooltip.textContent = '';
    tooltip.appendChild(createEl('div', {style: {fontWeight: '600', marginBottom: '4px'}}, found.node.name));
    tooltip.appendChild(createEl('div', {style: {color: 'var(--accent)'}}, fmtSize(found.node.size)));
    tooltip.appendChild(createEl('div', {style: {color: 'var(--text2)', wordBreak: 'break-all', marginTop: '4px'}}, found.node.path));
    tooltip.appendChild(createEl('div', {style: {marginTop: '6px', fontSize: '11px', color: 'var(--text2)'}}, '点击进入 | 右键选择清理'));
    let tx = e.clientX + 12, ty = e.clientY + 12;
    if (tx + 300 > window.innerWidth) tx = e.clientX - 310;
    if (ty + 100 > window.innerHeight) ty = e.clientY - 110;
    tooltip.style.left = tx + 'px';
    tooltip.style.top = ty + 'px';
  } else {
    hoveredRect = null;
    tooltip.classList.add('hidden');
  }
});

canvas.addEventListener('mouseleave', () => { tooltip.classList.add('hidden'); });

canvas.addEventListener('click', (e) => {
  if (!hoveredRect) return;
  const node = hoveredRect.node;
  if (node.children && node.children.length > 0) {
    navStack.push(node);
    currentNode = node;
    drawTreemap();
  }
});

canvas.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  if (!hoveredRect) return;
  toggleSelect(hoveredRect.node.path, hoveredRect.node.size);
  drawTreemap();
});

window.addEventListener('resize', () => { drawTreemap(); });

// ─── Categories ────────────────────────────────
function renderCategories() {
  const grid = document.getElementById('cat-grid');
  grid.textContent = '';
  const totalSize = TREE.size;
  const cats = Object.entries(CAT_STATS).sort((a, b) => b[1].size - a[1].size);

  cats.forEach(([name, data]) => {
    const pct = (data.size / totalSize * 100).toFixed(1);
    const color = CAT_COLORS[name] || '#95a5a6';

    const card = createEl('div', {class: 'cat-card', onclick: () => showCatDetail(name)}, [
      createEl('div', {class: 'cat-header'}, [
        createEl('div', {class: 'cat-name'}, [
          createEl('span', {class: 'cat-dot', style: {background: color}}),
          document.createTextNode(name),
        ]),
        createEl('div', {class: 'cat-size'}, fmtSize(data.size)),
      ]),
      createEl('div', {class: 'cat-count'}, data.count + ' 个文件 · 占总空间 ' + pct + '%'),
      (() => {
        const bar = createEl('div', {class: 'cat-bar'});
        const fill = createEl('div', {class: 'cat-bar-fill', style: {width: pct + '%', background: color}});
        bar.appendChild(fill);
        return bar;
      })(),
    ]);
    grid.appendChild(card);
  });
}

function showCatDetail(catName) {
  const detail = document.getElementById('cat-detail');
  const data = CAT_STATS[catName];
  if (!data || !data.files.length) {
    detail.classList.remove('open');
    return;
  }
  detail.classList.add('open');
  detail.textContent = '';
  detail.appendChild(createEl('h3', null, catName + ' — 可清理文件 Top 50'));

  const list = createEl('div', {class: 'file-list'});
  data.files.forEach(f => {
    const cb = createEl('input', {type: 'checkbox'});
    cb.checked = selected.has(f.path);
    cb.addEventListener('change', () => {
      toggleSelect(f.path, f.size);
      cb.checked = selected.has(f.path);
    });
    const item = createEl('div', {class: 'file-item'}, [
      cb,
      createEl('span', {class: 'fi-path'}, f.path),
      createEl('span', {class: 'fi-size'}, fmtSize(f.size)),
    ]);
    list.appendChild(item);
  });
  detail.appendChild(list);
  detail.scrollIntoView({behavior: 'smooth'});
}

// ─── Large Files ───────────────────────────────
function renderLargeFiles() {
  const list = document.getElementById('large-list');
  list.textContent = '';
  LARGE_FILES.forEach(f => {
    const cb = createEl('input', {type: 'checkbox'});
    cb.checked = selected.has(f.path);
    cb.addEventListener('change', () => {
      toggleSelect(f.path, f.size);
      cb.checked = selected.has(f.path);
    });
    const pathSpan = createEl('span', {class: 'fi-path'});
    pathSpan.textContent = f.path + ' ';
    const catSpan = createEl('span', {class: 'fi-cat', style: {color: CAT_COLORS[f.category] || '#95a5a6'}});
    catSpan.textContent = '[' + f.category + ']';
    pathSpan.appendChild(catSpan);

    list.appendChild(createEl('div', {class: 'file-item'}, [
      cb,
      pathSpan,
      createEl('span', {class: 'fi-size'}, fmtSize(f.size)),
    ]));
  });
}

// ─── Selection ─────────────────────────────────
function toggleSelect(path, size) {
  if (!path) return;
  if (selected.has(path)) selected.delete(path); else selected.set(path, size);
  updateSelectionBar();
}

function clearSelection() {
  selected.clear();
  updateSelectionBar();
  renderLargeFiles();
  renderCategories();
  drawTreemap();
}

function updateSelectionBar() {
  const count = selected.size;
  const totalSize = Array.from(selected.values()).reduce((a, b) => a + b, 0);
  document.getElementById('sel-count').textContent = '已选 ' + count + ' 项';
  document.getElementById('sel-size').textContent = fmtSize(totalSize);
  document.getElementById('btn-delete').disabled = count === 0;
}

document.getElementById('btn-clear').addEventListener('click', clearSelection);
document.getElementById('btn-delete').addEventListener('click', showDeleteModal);

// ─── Delete Modal ──────────────────────────────
function showDeleteModal() {
  if (selected.size === 0) return;
  const totalSize = Array.from(selected.values()).reduce((a, b) => a + b, 0);
  const overlay = document.getElementById('modal-overlay');
  overlay.classList.remove('hidden');
  overlay.textContent = '';

  const modal = createEl('div', {class: 'modal'});
  modal.appendChild(createEl('h3', null, '确认移到废纸篓？'));
  modal.appendChild(createEl('p', {style: {color: 'var(--text2)', fontSize: '13px', marginBottom: '8px'}},
    '以下 ' + selected.size + ' 个项目将被移到废纸篓，共 ' + fmtSize(totalSize)));

  const listDiv = createEl('div', {class: 'modal-list'});
  Array.from(selected.keys()).forEach(p => {
    listDiv.appendChild(createEl('div', null, p));
  });
  modal.appendChild(listDiv);

  const actions = createEl('div', {class: 'modal-actions'});
  actions.appendChild(createEl('button', {class: 'btn-cancel', onclick: hideModal}, '取消'));
  actions.appendChild(createEl('button', {class: 'btn-danger', onclick: executeDelete}, '确认删除'));
  modal.appendChild(actions);
  overlay.appendChild(modal);
}

function hideModal() { document.getElementById('modal-overlay').classList.add('hidden'); }

async function executeDelete() {
  const paths = Array.from(selected.keys());
  try {
    const resp = await fetch('/api/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paths})
    });
    const result = await resp.json();
    if (result.success) {
      const overlay = document.getElementById('modal-overlay');
      overlay.textContent = '';
      const modal = createEl('div', {class: 'modal'});
      modal.appendChild(createEl('h3', null, '清理完成'));
      modal.appendChild(createEl('p', {style: {color: 'var(--success)', margin: '12px 0'}},
        '成功删除 ' + result.deleted + ' 项，释放 ' + fmtSize(result.freed) + ' 空间'));
      if (result.errors.length) {
        modal.appendChild(createEl('p', {style: {color: 'var(--danger)', fontSize: '12px'}},
          result.errors.length + ' 项删除失败'));
        const errList = createEl('div', {class: 'modal-list'});
        result.errors.forEach(e => errList.appendChild(createEl('div', null, e)));
        modal.appendChild(errList);
      }
      const actions = createEl('div', {class: 'modal-actions'});
      actions.appendChild(createEl('button', {class: 'btn-cancel', onclick: hideModal}, '关闭'));
      modal.appendChild(actions);
      overlay.appendChild(modal);
      selected.clear();
      updateSelectionBar();
    }
  } catch (e) {
    alert('删除请求失败: ' + e.message);
  }
}

document.getElementById('modal-overlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) hideModal();
});

// ─── Init ──────────────────────────────────────
renderCategories();
renderLargeFiles();
requestAnimationFrame(drawTreemap);
</script>
</body>
</html>"""


# ─── 清理服务 ──────────────────────────────────────────────────

class CleanerHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, html_path=None, **kwargs):
        self.html_path = html_path
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(self.html_path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/delete":
            content_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_len))
            paths = body.get("paths", [])

            deleted = 0
            freed = 0
            errors = []

            for p in paths:
                p = os.path.abspath(p)
                if not os.path.exists(p):
                    errors.append(f"不存在: {p}")
                    continue
                try:
                    size = _get_size(p)
                    _trash(p)
                    deleted += 1
                    freed += size
                except Exception as e:
                    errors.append(f"{p}: {e}")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True, "deleted": deleted, "freed": freed, "errors": errors
            }).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def _get_size(path):
    if os.path.isfile(path):
        st = os.lstat(path)
        return st.st_blocks * 512 if hasattr(st, 'st_blocks') else st.st_size
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                st = os.lstat(fp)
                total += st.st_blocks * 512 if hasattr(st, 'st_blocks') else st.st_size
            except OSError:
                pass
    return total


def _trash(path):
    trash_dir = os.path.expanduser("~/.Trash")
    basename = os.path.basename(path)
    dest = os.path.join(trash_dir, basename)
    if os.path.exists(dest):
        name, ext = os.path.splitext(basename)
        dest = os.path.join(trash_dir, f"{name}_{int(time.time())}{ext}")
    shutil.move(path, dest)


def make_handler(html_path):
    def handler(*args, **kwargs):
        return CleanerHandler(*args, html_path=html_path, **kwargs)
    return handler


# ─── 主程序 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Mac Storage Cleaner")
    parser.add_argument("path", nargs="?", default=os.path.expanduser("~"), help="要扫描的路径 (默认 ~)")
    parser.add_argument("--depth", type=int, default=6, help="最大扫描深度 (默认 6)")
    parser.add_argument("--port", type=int, default=8432, help="HTTP 服务端口 (默认 8432)")
    parser.add_argument("--no-serve", action="store_true", help="仅生成 HTML，不启动服务")
    parser.add_argument("--output", default=None, help="输出 HTML 文件路径")
    args = parser.parse_args()

    scan_path = os.path.abspath(args.path)
    output_path = args.output or os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.html")

    print(f"\n  Mac Storage Cleaner")
    print(f"  {'─' * 40}")
    print(f"  扫描路径: {scan_path}")
    print(f"  扫描深度: {args.depth}")
    print()

    start_time = time.time()

    def progress(path, size):
        name = os.path.basename(path) or path
        print(f"  扫描中: {name:40s} {format_size(size):>10s}", flush=True)

    print("  开始扫描文件系统...\n")
    tree, categories, large_files = scan_directory(scan_path, max_depth=args.depth, progress_callback=progress)

    elapsed = time.time() - start_time
    print(f"\n  扫描完成! 耗时 {elapsed:.1f}s")
    print(f"  总大小: {format_size(tree['size'])}")
    print(f"  大文件: {len(large_files)} 个 (>50MB)")
    print(f"  分类统计:")
    for cat_name, data in sorted(categories.items(), key=lambda x: x[1]["size"], reverse=True):
        print(f"    {cat_name:12s} {format_size(data['size']):>10s}  ({data['count']} 个文件)")

    tree = _prune_tree(tree)

    print(f"\n  生成报告: {output_path}")
    generate_html(tree, categories, large_files, scan_path, output_path)

    if args.no_serve:
        print(f"  报告已生成，用浏览器打开: {output_path}")
        return

    port = args.port
    handler = make_handler(output_path)
    http.server.HTTPServer.allow_reuse_address = True
    server = http.server.HTTPServer(("127.0.0.1", port), handler)

    print(f"\n  启动清理服务: http://127.0.0.1:{port}")
    print(f"  按 Ctrl+C 停止服务\n")

    webbrowser.open(f"http://127.0.0.1:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
