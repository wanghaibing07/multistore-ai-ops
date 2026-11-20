from __future__ import annotations

import csv
import io
import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from .card_query_ai import find_ego_browser
from . import importer


QIANNIU_ORDER_URL = "https://myseller.taobao.com/home.htm/trade-platform/tp/sold"
_BROWSER_SENTINEL = "__MS_QN_ORDER_IMPORT__"
_MAX_BROWSER_PAGES = 100


def _build_qianniu_order_script() -> str:
    sentinel = json.dumps(_BROWSER_SENTINEL, ensure_ascii=False)
    order_url = json.dumps(QIANNIU_ORDER_URL)
    return f"""
const SENTINEL = {sentinel};
const ORDER_URL = {order_url};
const MAX_PAGES = {_MAX_BROWSER_PAGES};

function extractPage() {{
  const extractMoney = (value) => {{
    const match = String(value || "").replaceAll(",", "").match(/-?\\d+(?:\\.\\d{{1,2}})?/);
    return match ? match[0] : "0.00";
  }};
  const tables = Array.from(document.querySelectorAll("table.next-table-row"));
  const rows = [];
  for (const table of tables) {{
    const header = table.querySelector(".sold_table-header-box__BlrO5")?.innerText || "";
    const orderMatch = header.match(/订单号：\\s*([^\\s]+)/);
    const timeMatch = header.match(/创建时间：\\s*(\\d{{4}}-\\d{{2}}-\\d{{2}}\\s+\\d{{2}}:\\d{{2}}:\\d{{2}})/);
    const orderId = orderMatch?.[1] || "";
    const createdAt = timeMatch?.[1] || "";
    if (!orderId) continue;
    const itemRows = Array.from(table.querySelectorAll("tbody > tr:not(.next-table-group-header)"));
    itemRows.forEach((row, index) => {{
      const cells = Array.from(row.querySelectorAll("td[data-next-table-col]"));
      if (cells.length < 6) return;
      const title = cells[0].querySelector(".sold_product-title__NN9T8")?.innerText?.trim() || "";
      const property = cells[0].querySelector(".sold_sku-desc__F8BnF, .sold_extra-desc__A2XJu")?.innerText?.trim() || "";
      const quantity = cells[2]?.innerText?.trim() || "";
      const fulfillment = cells[3]?.innerText?.trim() || "";
      const status = cells[4]?.innerText?.trim() || "";
      const refundStatus = status.includes("退款") || fulfillment.includes("退款") ? (status || fulfillment) : "无退款";
      rows.push({{
        item_id: `${{orderId}}-${{index + 1}}`,
        order_id: orderId,
        title,
        property,
        quantity,
        paid_at: createdAt,
        status: status || fulfillment,
        payment: extractMoney(cells[5]?.innerText || ""),
        refund_status: refundStatus,
        refund_amount: "",
      }});
    }});
  }}
  const next = document.querySelector("button[aria-label^='下一页']");
  const pageText = tables.map((table) => table.innerText || "").join("\\n---\\n");
  return {{
    rows,
    pageText,
    nextDisabled: !next || next.disabled,
    hasOrderPage: rows.length > 0,
    hasLoginPrompt: /登录|请先登录|重新登录/.test(document.body?.innerText || ""),
  }};
}}

let task;
try {{
  task = await taskSpace("multistore qianniu ai order import");
  const page = task.page("p1");
  await page.goto(ORDER_URL);
  await page.waitForLoadState();
  await page.waitForTimeout(1200);
  const first = await page.evaluate(extractPage);
  if (first.hasLoginPrompt && !first.hasOrderPage) {{
    console.log(SENTINEL + JSON.stringify({{ok: false, status: "needs_login"}}));
  }} else if (!first.hasOrderPage) {{
    console.log(SENTINEL + JSON.stringify({{ok: false, error: "当前千牛页面没有可导入订单，请先完成验证并加载订单"}}));
  }} else {{
    const rows = [];
    let pages = 0;
    let current = first;
    while (pages < MAX_PAGES) {{
      rows.push(...current.rows);
      pages += 1;
      if (current.nextDisabled) break;
      const moved = await page.evaluate(() => {{
        const next = document.querySelector("button[aria-label^='下一页']");
        if (!next || next.disabled) return false;
        next.click();
        return true;
      }});
      if (!moved) break;
      await page.waitForFunction((previous) => {{
        const rowText = Array.from(document.querySelectorAll("table.next-table-row tbody > tr:not(.next-table-group-header)"))
          .map((row) => row.innerText || "")
          .join("\\n---\\n");
        return Boolean(rowText) && rowText !== previous.pageText;
      }}, {{pageText: current.pageText}}, {{timeout: 8000}});
      current = await page.evaluate(extractPage);
    }}
    console.log(SENTINEL + JSON.stringify({{ok: true, rows, pages}}));
  }}
}} catch (err) {{
  console.log(SENTINEL + JSON.stringify({{ok: false, error: String(err)}}));
}} finally {{
  if (task) await task.finish({{keep: []}});
}}
"""


def _run_browser_probe(script_body: str, *, timeout: int = 100) -> dict[str, Any]:
    ego_exe = find_ego_browser()
    temp_dir = Path(tempfile.gettempdir()) / "multistore-ego"
    temp_dir.mkdir(parents=True, exist_ok=True)
    script_path = temp_dir / f"qn-order-{uuid4().hex}.mjs"
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
        raise importer.ImportProblem("读取千牛订单超时，请缩小千牛筛选范围后重试") from None
    finally:
        script_path.unlink(missing_ok=True)

    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    for line in reversed(combined.splitlines()):
        if line.startswith(_BROWSER_SENTINEL):
            try:
                return json.loads(line[len(_BROWSER_SENTINEL) :])
            except json.JSONDecodeError:
                break
    detail = combined.strip()[-400:]
    raise importer.ImportProblem(f"读取千牛订单失败：{detail or '浏览器未返回有效结果'}")


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "item_id",
            "order_id",
            "sku",
            "title",
            "quantity",
            "paid_at",
            "status",
            "payment",
            "refund_status",
            "refund_amount",
        ],
    )
    writer.writeheader()
    for row in rows:
        title = str(row.get("title") or "").strip()
        property_text = str(row.get("property") or "").strip()
        sku = " | ".join(value for value in (title, property_text) if value)
        writer.writerow({
            key: sku if key == "sku" else str(row.get(key) or "")
            for key in writer.fieldnames
        })
    return output.getvalue().encode("utf-8-sig")


def _dedupe_browser_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep the latest row for each browser-generated child-order key."""
    unique: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    duplicate_count = 0
    for row in rows:
        item_id = str(row.get("item_id") or "").strip()
        if not item_id:
            unique.append(row)
            continue
        if item_id not in positions:
            positions[item_id] = len(unique)
            unique.append(row)
            continue
        duplicate_count += 1
        unique[positions[item_id]] = row
    return unique, duplicate_count


def preview_from_browser(
    db: Session,
    store_id: int,
    basis: str = "gross",
    *,
    runner: Callable[[str], dict[str, Any]] = _run_browser_probe,
) -> dict[str, Any]:
    result = runner(_build_qianniu_order_script())
    if result.get("status") == "needs_login":
        raise importer.ImportProblem("请先在千牛完成登录，再点击 AI 导入千牛订单")
    if not result.get("ok"):
        raise importer.ImportProblem(str(result.get("error") or "千牛页面没有返回订单数据"))
    source_rows = [row for row in result.get("rows", []) if isinstance(row, dict)]
    rows, duplicate_count = _dedupe_browser_rows(source_rows)
    if not rows:
        raise importer.ImportProblem("当前千牛页面没有可导入订单，请先筛选并加载订单")
    filename = f"qianniu-ai-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    preview = importer.preview(db, store_id, "orders", filename, _csv_bytes(rows), basis)
    preview.update({
        "file_name": filename,
        "source": "qianniu-ai",
        "source_pages": int(result.get("pages") or 0),
        "source_rows": len(source_rows),
        "source_duplicate_rows": duplicate_count,
    })
    return preview
