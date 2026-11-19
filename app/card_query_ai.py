from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .importer import ImportProblem

_SENTINEL = "__MS_CARD_QUERY_RESULT__"
_CAPTCHA_TTL_SECONDS = 300
_PENDING_LOCK = threading.Lock()
_PENDING_SESSIONS: dict[str, dict[str, Any]] = {}

_16688_URL = "https://www.16688.com.cn/order"
_WZYP_URL = "https://wzyp.cn/order"


def find_ego_browser() -> str:
    env_bin = os.environ.get("MULTISTORE_EGO_BROWSER", "").strip()
    if env_bin and Path(env_bin).exists():
        return env_bin
    on_path = shutil.which("ego-browser")
    if on_path:
        return on_path
    candidate = Path.home() / ".local" / "share" / "ego" / "active_version_dir" / "ego-browser.exe"
    if candidate.exists():
        return str(candidate)
    raise ImportProblem("未找到 ego-browser，请确认已安装 Ego Lite 浏览器客户端")


def _run_ego_script(script_body: str, *, timeout: int = 120) -> dict[str, Any]:
    ego_exe = find_ego_browser()
    temp_dir = Path(tempfile.gettempdir()) / "multistore-ego"
    temp_dir.mkdir(parents=True, exist_ok=True)
    script_path = temp_dir / f"job-{uuid4().hex}.mjs"
    script_path.write_text(script_body, encoding="utf-8")
    try:
        proc = subprocess.run(
            [ego_exe, "nodejs", "-e", f"await import('{script_path.as_uri()}')"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ImportProblem("浏览器查询卡网超时，请重试或改用粘贴导入") from None
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    for line in reversed(combined.splitlines()):
        if line.startswith(_SENTINEL):
            try:
                return json.loads(line[len(_SENTINEL):])
            except json.JSONDecodeError:
                break
    err_tail = ((proc.stderr or "") or (proc.stdout or "")).strip()[-400:]
    raise ImportProblem(f"浏览器执行卡网查询失败：{err_tail or '未返回有效结果'}")


def _finish_space(space_name: str) -> None:
    script = f"""
const SENTINEL = {json.dumps(_SENTINEL)};
const spaceName = {json.dumps(space_name)};
try {{
  const spaces = await listTaskSpaces();
  const entry = spaces.find((s) => s.name === spaceName);
  if (entry) {{
    const task = entry.ownership === "user"
      ? await claimTaskSpace(entry.id)
      : entry.ownership === "agentDelegatedToUser"
        ? await takeOverTaskSpace(entry.id)
        : await taskSpace(entry.id);
    await task.finish({{ keep: [] }});
  }}
  console.log(SENTINEL + JSON.stringify({{ ok: true }}));
}} catch (err) {{
  console.log(SENTINEL + JSON.stringify({{ ok: false, error: String(err) }}));
}}
"""
    try:
        _run_ego_script(script, timeout=30)
    except Exception:
        pass


def _cleanup_expired_sessions() -> None:
    now = time.monotonic()
    expired: list[str] = []
    with _PENDING_LOCK:
        for key, item in list(_PENDING_SESSIONS.items()):
            if now - item.get("created_at", 0) > _CAPTCHA_TTL_SECONDS:
                expired.append(item["space_name"])
                _PENDING_SESSIONS.pop(key, None)
    for space_name in expired:
        _finish_space(space_name)


def _solve_wzyp_captcha_vision(data_url: str) -> str:
    base = os.environ.get("MULTISTORE_MODEL_URL", "").strip()
    key = os.environ.get("MULTISTORE_MODEL_API_KEY", "").strip()
    model = os.environ.get("MULTISTORE_MODEL_NAME", "").strip()
    if not (base and key and model):
        return ""
    parsed = urlsplit(base)
    if not parsed.hostname or (
        parsed.scheme != "https"
        and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))
    ):
        return ""
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=5), follow_redirects=False, trust_env=False) as client:
            resp = client.post(
                base.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "请识别图片中的4位英文或数字验证码。只输出验证码字符本身，不要任何解释、空格或标点。",
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": data_url},
                                },
                            ],
                        }
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(content or ""))
    return cleaned[:6]


def _build_16688_script(space_name: str, contact_email: str, *, keep_open: bool = False) -> str:
    url = os.environ.get("MULTISTORE_CARD_16688_URL", _16688_URL)
    return f"""
const SENTINEL = {json.dumps(_SENTINEL)};
const SPACE_NAME = {json.dumps(space_name)};
const EMAIL = {json.dumps(contact_email)};
const URL_16688 = {json.dumps(url)};
const KEEP_OPEN = {json.dumps(keep_open)};

async function openSpace(name) {{
  const spaces = await listTaskSpaces();
  const entry = spaces.find((s) => s.name === name);
  if (!entry) return await taskSpace(name);
  if (entry.ownership === "user") return await claimTaskSpace(entry.id);
  if (entry.ownership === "agentDelegatedToUser") return await takeOverTaskSpace(entry.id);
  return await taskSpace(entry.id);
}}

async function solveSliderOnce(page) {{
  const solvedX = await page.evaluate(async () => {{
    const bgImg = document.querySelector("img._bg_1cnla_74");
    const pcImg = document.querySelector("img._piece_1cnla_79");
    if (!bgImg || !pcImg) return {{ error: "no_modal" }};
    const load = (img) => new Promise((res) => {{
      if (img.complete && img.naturalWidth > 0) return res();
      img.onload = () => res();
      setTimeout(() => res(), 3000);
    }});
    await Promise.all([load(bgImg), load(pcImg)]);
    const top = parseInt(pcImg.style.top || "0", 10) || Math.round(pcImg.getBoundingClientRect().y - bgImg.getBoundingClientRect().y);
    const bw = bgImg.naturalWidth, bh = bgImg.naturalHeight;
    const pw = pcImg.naturalWidth, ph = pcImg.naturalHeight;
    if (!bw || !bh || !pw || !ph) return {{ error: "zero_image" }};

    const cBg = document.createElement("canvas");
    cBg.width = bw; cBg.height = bh;
    const ctxBg = cBg.getContext("2d");
    ctxBg.drawImage(bgImg, 0, 0);
    const bgData = ctxBg.getImageData(0, 0, bw, bh).data;

    const cPc = document.createElement("canvas");
    cPc.width = pw; cPc.height = ph;
    const ctxPc = cPc.getContext("2d");
    ctxPc.drawImage(pcImg, 0, 0);
    const pcData = ctxPc.getImageData(0, 0, pw, ph).data;

    const edges = [];
    for (let y = 0; y < ph; y++) {{
      for (let x = 0; x < pw; x++) {{
        const a = pcData[(y * pw + x) * 4 + 3];
        if (a > 200) {{
          let isEdge = false;
          for (const [dx, dy] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) {{
            const nx = x + dx, ny = y + dy;
            const na = (nx >= 0 && ny >= 0 && nx < pw && ny < ph) ? pcData[(ny * pw + nx) * 4 + 3] : 0;
            if (na <= 128) {{ isEdge = true; break; }}
          }}
          if (isEdge) edges.push([x, y]);
        }}
      }}
    }}
    if (!edges.length) return {{ error: "no_edges" }};

    let bestEdge = {{ sx: -1, edgeLum: 1e9 }};
    for (let sx = 0; sx <= bw - pw; sx++) {{
      let edgeSum = 0;
      for (let k = 0; k < edges.length; k++) {{
        const [x, y] = edges[k];
        const ty = Math.min(bh - 1, Math.max(0, top + y));
        const bi = (ty * bw + (sx + x)) * 4;
        edgeSum += 0.299 * bgData[bi] + 0.587 * bgData[bi + 1] + 0.114 * bgData[bi + 2];
      }}
      const edgeLum = edgeSum / edges.length;
      if (edgeLum < bestEdge.edgeLum) bestEdge = {{ sx, edgeLum }};
    }}
    return {{ sx: bestEdge.sx }};
  }});

  if (solvedX.error || solvedX.sx < 0) return false;
  const boxes = await page.evaluate(() => ({{
    thumb: document.querySelector("div._thumb_1cnla_118")?.getBoundingClientRect().toJSON() || null,
    piece: document.querySelector("img._piece_1cnla_79")?.getBoundingClientRect().toJSON() || null,
    bg: document.querySelector("img._bg_1cnla_74")?.getBoundingClientRect().toJSON() || null,
    rail: document.querySelector("div._rail_1cnla_105")?.getBoundingClientRect().toJSON() || null,
  }}));
  if (!boxes.thumb || !boxes.piece || !boxes.bg) return false;

  const startX = boxes.thumb.x + boxes.thumb.width / 2;
  const startY = boxes.thumb.y + boxes.thumb.height / 2;
  const bgTravel = (boxes.bg.width - boxes.piece.width) || 248;
  const railTravel = boxes.rail ? (boxes.rail.width - boxes.thumb.width) : bgTravel;
  let totalMouseDx = solvedX.sx * (railTravel / bgTravel);

  await page.mouse.move(startX, startY);
  await page.waitForTimeout(150);
  await page.mouse.down();
  await page.waitForTimeout(110);
  const steps = 38;
  let curMouseX = startX;
  for (let i = 1; i <= steps; i++) {{
    const t = i / steps;
    const ease = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    if (i === 32) {{
      const curState = await page.evaluate(() => {{
        const bg = document.querySelector("img._bg_1cnla_74")?.getBoundingClientRect();
        const piece = document.querySelector("img._piece_1cnla_79")?.getBoundingClientRect();
        return (bg && piece) ? (piece.x - bg.x) : 0;
      }});
      const movedMouse = curMouseX - startX;
      if (curState > 5 && movedMouse > 5) {{
        totalMouseDx = solvedX.sx / (curState / movedMouse);
      }}
    }}
    const x = startX + totalMouseDx * ease;
    const y = startY + Math.sin(t * Math.PI) * 2.2 + ((i % 3) - 1) * 0.4;
    curMouseX = x;
    await page.mouse.move(x, y);
    await page.waitForTimeout(18 + (i > steps - 8 ? 14 : 0));
  }}
  await page.waitForTimeout(140);
  await page.mouse.up();

  for (let w = 0; w < 14; w++) {{
    await page.waitForTimeout(300);
    const stillModal = await page.evaluate(() => !!document.querySelector(".captcha-modal"));
    if (!stillModal) return true;
  }}
  return false;
}}

let task = null;
try {{
  task = await openSpace(SPACE_NAME);
  const page = (await task.pages())[0] ?? task.page("p1");
  await page.goto(URL_16688, {{ waitUntil: "domcontentloaded", timeout: 25000 }});
  await page.waitForSelector("css:input.search-input", {{ timeout: 15000 }});
  await page.fill("css:input.search-input", EMAIL);
  await page.click("css:button.search-btn", {{ label: "查询 16688 订单" }});

  for (let w = 0; w < 20; w++) {{
    const ready = await page.evaluate(() => {{
      if (document.querySelector(".captcha-modal img._bg_1cnla_74")) return "captcha";
      if (document.querySelector(".card-head")) return "cards";
      const txt = document.body.innerText || "";
      if (txt.includes("暂无") || txt.includes("没有找到")) return "empty";
      return null;
    }});
    if (ready) break;
    await page.waitForTimeout(400);
  }}

  const needsCaptcha = await page.evaluate(() => !!document.querySelector(".captcha-modal img._bg_1cnla_74"));
  if (needsCaptcha) {{
    let passed = false;
    for (let attempt = 0; attempt < 3; attempt++) {{
      passed = await solveSliderOnce(page);
      if (passed) break;
      await page.evaluate(() => {{
        const refresh = document.querySelector(".captcha-modal [class*='refresh'], .captcha-modal button");
        if (refresh) refresh.click();
      }});
      await page.waitForTimeout(900);
    }}
    if (!passed) {{
      throw new Error("16688 滑块验证未通过，请重试或改用复制粘贴导入");
    }}
  }}

  await page.waitForTimeout(1000);
  const result = await page.evaluate(async () => {{
    const cards = [...document.querySelectorAll(".card-head")].map((head) => {{
      const card = head.parentElement;
      const store = head.querySelector(".store-name")?.innerText?.trim() || "";
      const status = head.querySelector(".card-status")?.innerText?.trim() || "";
      const title = card.querySelector(".card-name")?.innerText?.trim() || "";
      const meta = card.querySelector(".card-meta")?.innerText?.trim() || "";
      const price = card.querySelector(".card-price")?.innerText?.trim() || "";
      const qtyText = card.querySelector(".card-qty, .card-num, .card-count")?.innerText?.trim() || "";
      const bodyText = card.innerText?.trim() || "";
      return {{ store, status, title, meta, price, qtyText, refunded: bodyText.includes("已退款") }};
    }});
    let apiList = [];
    try {{
      const resp = await fetch("/shopApi/order/query", {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        body: JSON.stringify({{}}),
      }});
      const json = await resp.json();
      if (Array.isArray(json?.data?.list)) apiList = json.data.list;
    }} catch {{}}
    return {{ cards, apiList }};
  }});
  if (!KEEP_OPEN) {{
    await task.finish({{ keep: [] }});
  }}
  console.log(SENTINEL + JSON.stringify({{ ok: true, site: "16688", ...result }}));
}} catch (err) {{
  if (task && !KEEP_OPEN) {{
    try {{ await task.finish({{ keep: [] }}); }} catch {{}}
  }}
  console.log(SENTINEL + JSON.stringify({{ ok: false, error: err?.message || String(err) }}));
}}
"""


def _build_wzyp_open_script(space_name: str, contact_email: str) -> str:
    url = os.environ.get("MULTISTORE_CARD_WZYP_URL", _WZYP_URL)
    return f"""
const SENTINEL = {json.dumps(_SENTINEL)};
const SPACE_NAME = {json.dumps(space_name)};
const EMAIL = {json.dumps(contact_email)};
const URL_WZYP = {json.dumps(url)};

async function openSpace(name) {{
  const spaces = await listTaskSpaces();
  const entry = spaces.find((s) => s.name === name);
  if (!entry) return await taskSpace(name);
  if (entry.ownership === "user") return await claimTaskSpace(entry.id);
  if (entry.ownership === "agentDelegatedToUser") return await takeOverTaskSpace(entry.id);
  return await taskSpace(entry.id);
}}

async function readAllPages(page) {{
  async function readCur() {{
    return await page.evaluate(() => {{
      const activeEl = document.querySelector("li.arco-pagination-item-active");
      const activePage = activeEl ? activeEl.innerText.trim() : "1";
      const rows = [...document.querySelectorAll("tbody tr")].map((tr) =>
        [...tr.querySelectorAll("td")].map((td) => (td.innerText || "").trim())
      ).filter((r) => r.length >= 3);
      const nextEl = document.querySelector(".arco-pagination-item-next");
      const nextDisabled = !nextEl || nextEl.classList.contains("arco-pagination-item-disabled");
      return {{ activePage, nextDisabled, rows }};
    }});
  }}
  const pages = [];
  let first = await readCur();
  pages.push({{ page: first.activePage, rows: first.rows }});
  for (let i = 0; i < 20; i++) {{
    const cur = await readCur();
    if (cur.nextDisabled) break;
    const prevPage = cur.activePage;
    const prevFirst = JSON.stringify(cur.rows[0] || []);
    await page.click("css:.arco-pagination-item-next", {{ label: "链动小铺下一页" }});
    await page.waitForTimeout(700);
    let loaded = null;
    for (let w = 0; w < 10; w++) {{
      const snap = await readCur();
      if (snap.activePage !== prevPage && JSON.stringify(snap.rows[0] || []) !== prevFirst) {{
        loaded = snap;
        break;
      }}
      await page.waitForTimeout(350);
    }}
    if (!loaded) loaded = await readCur();
    pages.push({{ page: loaded.activePage, rows: loaded.rows }});
    if (loaded.nextDisabled) break;
  }}
  return pages;
}}

let task = null;
try {{
  task = await openSpace(SPACE_NAME);
  const page = (await task.pages())[0] ?? task.page("p1");
  await page.goto(URL_WZYP, {{ waitUntil: "domcontentloaded", timeout: 25000 }});
  await page.waitForTimeout(1200);
  const init = await page.evaluate(() => ({{
    hasInput: !!document.querySelector('input[placeholder="请输入预留联系方式/订单号"]'),
    hasTab: [...document.querySelectorAll("button")].some((b) => (b.innerText || "").includes("订单查询")),
  }}));
  if (!init.hasInput && init.hasTab) {{
    await page.click("button:has-text('订单查询')", {{ label: "打开链动小铺查询" }});
    await page.waitForSelector('css:input[placeholder="请输入预留联系方式/订单号"]', {{ timeout: 10000 }});
  }}
  await page.fill('css:input[placeholder="请输入预留联系方式/订单号"]', EMAIL);
  await page.click("button:has-text('查询订单')", {{ label: "提交链动小铺查询" }});

  let state = {{ status: "unknown" }};
  for (let w = 0; w < 18; w++) {{
    await page.waitForTimeout(400);
    state = await page.evaluate((tick) => {{
      const capInp = document.querySelector('input[placeholder="输入验证码"]');
      const capVisible = !!(capInp && (capInp.offsetParent !== null || capInp.getClientRects().length > 0));
      const capImg = [...document.querySelectorAll("img")].find((i) => i.src && i.src.includes("captchaImg"));
      if (capVisible || capImg) {{
        if (!capVisible || !capImg || !capImg.complete || capImg.naturalWidth <= 0) return {{ status: "loading" }};
        const c = document.createElement("canvas");
        c.width = capImg.naturalWidth;
        c.height = capImg.naturalHeight;
        c.getContext("2d").drawImage(capImg, 0, 0);
        return {{ status: "captcha", dataUrl: c.toDataURL("image/png") }};
      }}
      const rows = document.querySelectorAll("tbody tr").length;
      const empty = document.body.innerText.includes("暂无数据") || !!document.querySelector(".arco-empty");
      if (rows > 0) return {{ status: "rows" }};
      if (empty && tick >= 6) return {{ status: "empty" }};
      return {{ status: "unknown" }};
    }}, w);
    if (state.status === "captcha" || state.status === "rows" || state.status === "empty") break;
  }}

  if (state.status === "captcha") {{
    console.log(SENTINEL + JSON.stringify({{ ok: true, status: "captcha", dataUrl: state.dataUrl }}));
  }} else if (state.status === "rows" || state.status === "empty") {{
    const pages = state.status === "rows" ? await readAllPages(page) : [];
    await task.finish({{ keep: [] }});
    console.log(SENTINEL + JSON.stringify({{ ok: true, status: "done", pages }}));
  }} else {{
    throw new Error("链动小铺查询页未出现结果或验证码");
  }}
}} catch (err) {{
  if (task) {{
    try {{ await task.finish({{ keep: [] }}); }} catch {{}}
  }}
  console.log(SENTINEL + JSON.stringify({{ ok: false, error: err?.message || String(err) }}));
}}
"""


def _build_wzyp_submit_script(space_name: str, captcha_code: str, *, refresh_on_fail: bool = True) -> str:
    return f"""
const SENTINEL = {json.dumps(_SENTINEL)};
const SPACE_NAME = {json.dumps(space_name)};
const CODE = {json.dumps(captcha_code)};
const REFRESH_ON_FAIL = {json.dumps(refresh_on_fail)};

async function openSpace(name) {{
  const spaces = await listTaskSpaces();
  const entry = spaces.find((s) => s.name === name);
  if (!entry) throw new Error("验证码会话已过期，请重新发起查询");
  if (entry.ownership === "user") return await claimTaskSpace(entry.id);
  if (entry.ownership === "agentDelegatedToUser") return await takeOverTaskSpace(entry.id);
  return await taskSpace(entry.id);
}}

async function readAllPages(page) {{
  async function readCur() {{
    return await page.evaluate(() => {{
      const activeEl = document.querySelector("li.arco-pagination-item-active");
      const activePage = activeEl ? activeEl.innerText.trim() : "1";
      const rows = [...document.querySelectorAll("tbody tr")].map((tr) =>
        [...tr.querySelectorAll("td")].map((td) => (td.innerText || "").trim())
      ).filter((r) => r.length >= 3);
      const nextEl = document.querySelector(".arco-pagination-item-next");
      const nextDisabled = !nextEl || nextEl.classList.contains("arco-pagination-item-disabled");
      return {{ activePage, nextDisabled, rows }};
    }});
  }}
  const pages = [];
  let first = await readCur();
  pages.push({{ page: first.activePage, rows: first.rows }});
  for (let i = 0; i < 20; i++) {{
    const cur = await readCur();
    if (cur.nextDisabled) break;
    const prevPage = cur.activePage;
    const prevFirst = JSON.stringify(cur.rows[0] || []);
    await page.click("css:.arco-pagination-item-next", {{ label: "链动小铺下一页" }});
    await page.waitForTimeout(700);
    let loaded = null;
    for (let w = 0; w < 10; w++) {{
      const snap = await readCur();
      if (snap.activePage !== prevPage && JSON.stringify(snap.rows[0] || []) !== prevFirst) {{
        loaded = snap;
        break;
      }}
      await page.waitForTimeout(350);
    }}
    if (!loaded) loaded = await readCur();
    pages.push({{ page: loaded.activePage, rows: loaded.rows }});
    if (loaded.nextDisabled) break;
  }}
  return pages;
}}

let task = null;
try {{
  task = await openSpace(SPACE_NAME);
  const page = (await task.pages())[0] ?? task.page("p1");
  await page.fill('css:input[placeholder="输入验证码"]', CODE);
  await page.click("button:has-text('确定')", {{ label: "提交链动小铺验证码" }});

  let outcome = "waiting";
  let emptyTicks = 0;
  for (let w = 0; w < 15; w++) {{
    await page.waitForTimeout(400);
    const snap = await page.evaluate(() => {{
      const capInp = document.querySelector('input[placeholder="输入验证码"]');
      const hasCapInput = !!(capInp && (capInp.offsetParent !== null || capInp.getClientRects().length > 0));
      const hasRows = document.querySelectorAll("tbody tr").length > 0;
      const hasEmpty = !!document.querySelector(".arco-empty") || document.body.innerText.includes("暂无数据");
      if (!hasCapInput && hasRows) return "rows";
      if (!hasCapInput && hasEmpty) return "empty";
      return "captcha";
    }});
    if (snap === "rows") {{
      outcome = "rows";
      break;
    }}
    if (snap === "empty") {{
      emptyTicks += 1;
      if (emptyTicks >= 3) {{
        outcome = "empty";
        break;
      }}
    }} else {{
      emptyTicks = 0;
      outcome = snap;
    }}
  }}

  if (outcome === "rows" || outcome === "empty") {{
    const pages = outcome === "rows" ? await readAllPages(page) : [];
    await task.finish({{ keep: [] }});
    console.log(SENTINEL + JSON.stringify({{ ok: true, status: "done", pages }}));
  }} else {{
    if (REFRESH_ON_FAIL) {{
      const oldSrc = await page.evaluate(() => {{
        const capImg = [...document.querySelectorAll("img")].find((i) => i.src && i.src.includes("captchaImg"));
        const prev = capImg ? capImg.src : "";
        if (capImg) capImg.click();
        return prev;
      }});
      for (let w = 0; w < 12; w++) {{
        await page.waitForTimeout(300);
        const ready = await page.evaluate((prev) => {{
          const capImg = [...document.querySelectorAll("img")].find((i) => i.src && i.src.includes("captchaImg"));
          return !!(capImg && capImg.complete && capImg.naturalWidth > 0 && (!prev || capImg.src !== prev));
        }}, oldSrc);
        if (ready) break;
      }}
    }}
    const dataUrl = await page.evaluate(() => {{
      const capImg = [...document.querySelectorAll("img")].find((i) => i.src && i.src.includes("captchaImg"));
      if (!capImg || !capImg.complete || capImg.naturalWidth <= 0) return "";
      const c = document.createElement("canvas");
      c.width = capImg.naturalWidth;
      c.height = capImg.naturalHeight;
      c.getContext("2d").drawImage(capImg, 0, 0);
      return c.toDataURL("image/png");
    }});
    console.log(SENTINEL + JSON.stringify({{ ok: true, status: "captcha", dataUrl }}));
  }}
}} catch (err) {{
  if (task) {{
    try {{ await task.finish({{ keep: [] }}); }} catch {{}}
  }}
  console.log(SENTINEL + JSON.stringify({{ ok: false, error: err?.message || String(err) }}));
}}
"""


def _normalize_16688_payload(raw: dict[str, Any]) -> list[dict[str, Any]]:
    cards = raw.get("cards") or []
    api_list = raw.get("apiList") or []
    by_trade_no: dict[str, dict[str, Any]] = {}
    for item in api_list:
        if isinstance(item, dict) and item.get("trade_no"):
            by_trade_no[str(item["trade_no"]).strip()] = item

    orders: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        meta = str(card.get("meta") or "")
        m = re.search(r"([A-Za-z0-9_-]{6,})\s*[·•\s]+(\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2})", meta)
        trade_no = m.group(1) if m else ""
        paid_at = m.group(2) if m else ""
        api_item = by_trade_no.get(trade_no) or {}
        qty = api_item.get("goods_num") or api_item.get("quantity") or api_item.get("buy_num") or api_item.get("order_num") or api_item.get("num") or api_item.get("count")
        if not qty:
            qty_probe = " ".join(str(card.get(k) or "") for k in ("qtyText", "price"))
            qm = re.search(r"(?:共\s*(\d+)\s*件|(?<![A-Za-z0-9])[×xX]\s*(\d+)\b)", qty_probe)
            qty = int(qm.group(1) or qm.group(2)) if qm else 1
        status = str(card.get("status") or api_item.get("status_text") or "").strip()
        if card.get("refunded") and "退款" not in status:
            status = "已退款"
        if trade_no:
            seen.add(trade_no)
            orders.append({
                "order_id": trade_no,
                "product": card.get("title") or api_item.get("goods_name") or "",
                "seller": card.get("store") or (api_item.get("store") or {}).get("name") or "",
                "quantity": qty,
                "amount": card.get("price") or api_item.get("total_amount") or "",
                "refund": api_item.get("refund_amount") or 0,
                "paid_at": paid_at or api_item.get("create_time") or "",
                "status": status,
                "_source": "16688 AI查询",
            })
    for trade_no, api_item in by_trade_no.items():
        if trade_no in seen:
            continue
        qty = api_item.get("goods_num") or api_item.get("quantity") or api_item.get("buy_num") or api_item.get("order_num") or api_item.get("num") or api_item.get("count") or 1
        orders.append({
            "order_id": trade_no,
            "product": api_item.get("goods_name") or "",
            "seller": (api_item.get("store") or {}).get("name") or "",
            "quantity": qty,
            "amount": api_item.get("total_amount") or "",
            "refund": api_item.get("refund_amount") or 0,
            "paid_at": api_item.get("create_time") or "",
            "status": str(api_item.get("status_text") or api_item.get("status") or ""),
            "_source": "16688 AI查询",
        })
    return orders


def _normalize_wzyp_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .card_reconciliation import _parse_wzyp_cells
    orders: list[dict[str, Any]] = []
    for page in pages:
        for cells in page.get("rows") or []:
            if isinstance(cells, list):
                row = _parse_wzyp_cells(cells)
                if row:
                    row["_source"] = "链动小铺 AI查询"
                    orders.append(row)
    return orders


def query_16688(
    contact_email: str,
    *,
    runner=_run_ego_script,
) -> list[dict[str, Any]]:
    space_name = f"ms-16688-{uuid4().hex[:8]}"
    try:
        raw = runner(_build_16688_script(space_name, contact_email), timeout=120)
    except Exception:
        _finish_space(space_name)
        raise
    if not raw.get("ok"):
        raise ImportProblem(raw.get("error") or "16688 查询失败")
    return _normalize_16688_payload(raw)


def query_wzyp(
    contact_email: str,
    *,
    store_id: int,
    captcha_code: str | None = None,
    captcha_session_id: str | None = None,
    runner=_run_ego_script,
    captcha_solver=_solve_wzyp_captcha_vision,
) -> dict[str, Any]:
    _cleanup_expired_sessions()
    if captcha_session_id:
        with _PENDING_LOCK:
            session = _PENDING_SESSIONS.pop(captcha_session_id, None)
        if not session or session.get("store_id") != store_id:
            raise ImportProblem("验证码会话已失效，请重新点“AI 查询订单”")
        space_name = session["space_name"]
        code = re.sub(r"\s+", "", str(captcha_code or ""))
        if not code:
            _finish_space(space_name)
            raise ImportProblem("请输入图形验证码")
        res = runner(_build_wzyp_submit_script(space_name, code, refresh_on_fail=True), timeout=90)
        if not res.get("ok"):
            _finish_space(space_name)
            raise ImportProblem(res.get("error") or "链动小铺验证码提交失败")
        if res.get("status") == "done":
            return {"status": "done", "orders": _normalize_wzyp_pages(res.get("pages") or [])}
        # Re-queue updated captcha for user retry
        new_session_id = uuid4().hex
        with _PENDING_LOCK:
            _PENDING_SESSIONS[new_session_id] = {
                "space_name": space_name,
                "store_id": store_id,
                "contact_email": session["contact_email"],
                "created_at": time.monotonic(),
            }
        return {
            "status": "captcha_required",
            "site": "wzyp",
            "captcha_session_id": new_session_id,
            "captcha_image_data_url": res.get("dataUrl") or "",
            "message": "验证码有误，请看新图重新输入",
        }

    space_name = f"ms-wzyp-{uuid4().hex[:8]}"
    open_res = runner(_build_wzyp_open_script(space_name, contact_email), timeout=90)
    if not open_res.get("ok"):
        _finish_space(space_name)
        raise ImportProblem(open_res.get("error") or "链动小铺查询失败")
    if open_res.get("status") == "done":
        return {"status": "done", "orders": _normalize_wzyp_pages(open_res.get("pages") or [])}

    data_url = open_res.get("dataUrl") or ""
    for _attempt in range(3):
        guess = captcha_solver(data_url) if data_url else ""
        if not guess:
            break
        sub_res = runner(_build_wzyp_submit_script(space_name, guess, refresh_on_fail=True), timeout=90)
        if not sub_res.get("ok"):
            _finish_space(space_name)
            raise ImportProblem(sub_res.get("error") or "链动小铺验证码提交失败")
        if sub_res.get("status") == "done":
            return {"status": "done", "orders": _normalize_wzyp_pages(sub_res.get("pages") or [])}
        data_url = sub_res.get("dataUrl") or ""

    # Fallback: preserve the live captcha session so user can input the 4 characters inline
    session_id = uuid4().hex
    with _PENDING_LOCK:
        _PENDING_SESSIONS[session_id] = {
            "space_name": space_name,
            "store_id": store_id,
            "contact_email": contact_email,
            "created_at": time.monotonic(),
        }
    return {
        "status": "captcha_required",
        "site": "wzyp",
        "captcha_session_id": session_id,
        "captcha_image_data_url": data_url,
        "message": "链动小铺需要输入图中验证码，填写后点确认继续",
    }


def query_card_sites(
    *,
    store_id: int,
    site: str,
    contact_email: str,
    captcha_code: str | None = None,
    captcha_session_id: str | None = None,
    runner=_run_ego_script,
    captcha_solver=_solve_wzyp_captcha_vision,
) -> dict[str, Any]:
    orders: list[dict[str, Any]] = []
    site_results: dict[str, Any] = {}
    errors: list[str] = []
    captcha_challenge: dict[str, Any] | None = None

    if captcha_session_id:
        wzyp_res = query_wzyp(
            contact_email,
            store_id=store_id,
            captcha_code=captcha_code,
            captcha_session_id=captcha_session_id,
            runner=runner,
            captcha_solver=captcha_solver,
        )
        if wzyp_res["status"] == "captcha_required":
            return {
                "status": "captcha_required",
                "orders": [],
                "site_results": {"wzyp": {"status": "captcha_required", "count": 0}},
                "errors": [],
                **{k: wzyp_res[k] for k in ("site", "captcha_session_id", "captcha_image_data_url", "message")},
            }
        wzyp_orders = wzyp_res.get("orders") or []
        orders.extend(wzyp_orders)
        site_results["wzyp"] = {"status": "ok", "count": len(wzyp_orders)}
        return {"status": "ok", "orders": orders, "site_results": site_results, "errors": errors}

    if site in ("all", "16688"):
        try:
            o16688 = query_16688(contact_email, runner=runner)
            orders.extend(o16688)
            site_results["16688"] = {"status": "ok", "count": len(o16688)}
        except Exception as exc:
            msg = str(exc)
            site_results["16688"] = {"status": "error", "error": msg}
            errors.append(f"16688：{msg}")

    if site in ("all", "wzyp"):
        try:
            wzyp_res = query_wzyp(
                contact_email,
                store_id=store_id,
                runner=runner,
                captcha_solver=captcha_solver,
            )
            if wzyp_res["status"] == "captcha_required":
                captcha_challenge = wzyp_res
                site_results["wzyp"] = {"status": "captcha_required", "count": 0}
            else:
                wzyp_orders = wzyp_res.get("orders") or []
                orders.extend(wzyp_orders)
                site_results["wzyp"] = {"status": "ok", "count": len(wzyp_orders)}
        except Exception as exc:
            msg = str(exc)
            site_results["wzyp"] = {"status": "error", "error": msg}
            errors.append(f"链动小铺：{msg}")

    if captcha_challenge and not orders:
        return {
            "status": "captcha_required",
            "orders": orders,
            "site_results": site_results,
            "errors": errors,
            **{k: captcha_challenge[k] for k in ("site", "captcha_session_id", "captcha_image_data_url", "message")},
        }

    return {
        "status": "partial_captcha" if captcha_challenge else ("ok" if orders or not errors else "error"),
        "orders": orders,
        "site_results": site_results,
        "errors": errors,
        **({
            "captcha_required": True,
            "site": captcha_challenge["site"],
            "captcha_session_id": captcha_challenge["captcha_session_id"],
            "captcha_image_data_url": captcha_challenge["captcha_image_data_url"],
            "message": captcha_challenge["message"],
        } if captcha_challenge else {}),
    }
