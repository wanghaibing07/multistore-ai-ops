import fs from "node:fs/promises";
import path from "node:path";

// Run inside ego-browser (`ego run scripts/sync-thatim-ego.mjs`) with an already logged-in Thatim session.
const taskSpaceId = Number(process.env.THATIM_TASK_SPACE || 10);
const pageName = process.env.THATIM_PAGE_ID || "p1";
const baseUrl = (process.env.THATIM_BASE_URL || "https://thatim.vn").replace(/\/+$/, "");
const maxPagesCap = Math.max(1, Number(process.env.THATIM_MAX_PAGES || 20));
const outPath = path.resolve(process.env.THATIM_OUT_PATH || "data/thatim-orders.json");

const task = await taskSpace(taskSpaceId);
const page = task.page(pageName);

async function extractPage(pageNum) {
  await page.goto(`${baseUrl}/product-purchased?page=${pageNum}`, {
    waitUntil: "domcontentloaded",
    timeout: 30000,
  });
  await page.waitForTimeout(2500);
  const currentUrl = String(page.url() || "");
  if (/\/(?:login|signin|sign-in|auth)(?:[/?#]|$)/i.test(currentUrl)) {
    throw new Error(`Thatim 登录状态已失效（当前页：${currentUrl}），请先在浏览器重新登录后再同步`);
  }
  return await page.evaluate(() => {
    const cards = [...document.querySelectorAll(".product-card")];
    const currencies = window.AVAILABLE_CURRENCIES || [];
    const usdObj = currencies.find((c) => c.code === "USD");
    const vndPerUsd = usdObj ? Number(usdObj.rate) : 27000;
    const items = cards.map((card) => {
      const h6s = [...card.querySelectorAll("h6")].map((el) => el.innerText.trim());
      const orderId = (h6s[0] || "").replace(/^#/, "").trim();
      const productName = h6s[1] || "";
      const variantName = card.querySelector(".small.text-secondary")?.innerText?.trim() || "";
      const badge = card.querySelector(".badge");
      const badgeClass = badge?.className || "";
      const badgeText = badge?.innerText?.trim() || "";
      const createdAt = card.querySelector("small.text-muted")?.innerText?.trim() || "";
      const amtEl = card.querySelector(".dhAmount");
      const amountVnd = amtEl?.getAttribute("data-amount") ? Number(amtEl.getAttribute("data-amount")) : null;
      const amountDisplay = amtEl?.innerText?.trim() || "";
      let status = "unknown";
      if (badgeClass.includes("bg-success")) status = "completed";
      else if (badgeClass.includes("bg-danger")) status = "cancelled";
      else if (badgeClass.includes("bg-warning")) status = "pending";
      return {
        orderId,
        status,
        badgeText,
        createdAt,
        amountVnd,
        amountUsd: amountVnd != null && vndPerUsd > 0 ? Number((amountVnd / vndPerUsd).toFixed(4)) : null,
        vndPerUsd,
        amountDisplay,
        productName,
        variantName,
      };
    });
    const nextPages = [...document.querySelectorAll(".pagination a")]
      .map((a) => a.href)
      .filter((href) => href.includes("page="));
    return { vndPerUsd, count: items.length, nextPages, items };
  });
}

function discoverMaxPage(nextPages, currentMax) {
  let discovered = currentMax;
  for (const href of nextPages || []) {
    const match = String(href).match(/page=(\d+)/);
    if (match) discovered = Math.max(discovered, Number(match[1]));
  }
  return Math.min(discovered, maxPagesCap);
}

const firstPage = await extractPage(1);
let maxPage = discoverMaxPage(firstPage.nextPages, 1);

const seen = new Set();
const allOrders = [];
function appendItems(items) {
  for (const item of items) {
    if (!item.orderId || seen.has(item.orderId)) continue;
    seen.add(item.orderId);
    allOrders.push(item);
  }
}
appendItems(firstPage.items);
for (let pageNum = 2; pageNum <= maxPage; pageNum += 1) {
  const res = await extractPage(pageNum);
  maxPage = discoverMaxPage(res.nextPages, maxPage);
  appendItems(res.items);
}

if (allOrders.length === 0) {
  throw new Error("未抓取到任何 Thatim 订单（可能是登录已过期或页面结构变化），已中止并保留原输出文件");
}

await fs.mkdir(path.dirname(outPath), { recursive: true });
const payload = {
  source: "thatim",
  fetchedAt: new Date().toISOString(),
  vndPerUsd: firstPage.vndPerUsd,
  count: allOrders.length,
  orders: allOrders,
};
await fs.writeFile(outPath, JSON.stringify(payload, null, 2), "utf8");

const completedCount = allOrders.filter((o) => o.status === "completed").length;
const cancelledCount = allOrders.filter((o) => o.status === "cancelled").length;
console.log(
  JSON.stringify(
    {
      saved: outPath,
      totalPages: maxPage,
      count: allOrders.length,
      completedCount,
      cancelledCount,
    },
    null,
    2,
  ),
);
