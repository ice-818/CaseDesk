import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import type { ShoppingView, ShoppingViewProduct } from "../types";
import { MessageView } from "./MessageView";
import { ProductCard } from "./ProductCard";
import { ShoppingPanel } from "./ShoppingPanel";

const noop = () => {};
const first: ShoppingViewProduct = {
  sku: "NAV-1", name: "黑色通勤鞋", category: "服饰", position: 2, price_cents: 22505,
  inventory: 7, tags: ["color:黑色", "size:42"],
  feature_scores: { relevance: 1, price_fit: 0.91, inventory: 0.84, tag_match: 1 },
  total_score: 0.96,
};
const second = { ...first, sku: "NAV-2", name: "轻便通勤鞋", position: 1 };
const view: ShoppingView = {
  view: "list", display_index: 1, stop_reason: "completed", notice: "已到首位",
  selected_sku: null, compare_skus: [], products: [first, second],
  conditions: { max_price_cents: 30000, color: "黑色", size: "42", evidence_tags: [] },
};

it("renders a numbered product with a two-decimal price and all scoring fields", () => {
  const html = renderToStaticMarkup(<ProductCard product={first} onSelect={noop} />);
  for (const value of ["第 2 个商品：黑色通勤鞋", "NAV-1", "225.05 元", "库存 7 件", "size:42",
    "相关性", "价格符合度", "库存评分", "标签匹配", "总分", "0.960", 'aria-label="选择第 2 个商品：黑色通勤鞋"']) {
    expect(html).toContain(value);
  }
});

it("handles missing catalog fields and unassigned position without displaying a score block", () => {
  const html = renderToStaticMarkup(
    <ProductCard product={{ ...first, price_cents: null, inventory: null, tags: [], position: null,
      feature_scores: {}, total_score: null }} onSelect={noop} />,
  );
  expect(html).toContain("库存未提供");
  expect(html).toContain("价格未提供");
  expect(html).toContain("disabled");
  expect(html).not.toContain("规格标签");
  expect(html).not.toContain("推荐评分");
});

it("renders current conditions, notice and buttons for the indexed card", () => {
  const html = renderToStaticMarkup(<ShoppingPanel view={view} busy={false} onNavigate={noop} />);
  for (const value of ["当前条件", "300.00 元以内", "黑色", "42", "不限", "已到首位", "NAV-2",
    'aria-label="上一个"', 'aria-label="换一个"', 'aria-label="对比当前与下一个商品"', "选这个"]) {
    expect(html).toContain(value);
  }
  expect(html).not.toContain("NAV-1");
  expect(html).not.toContain("disabled");
  const unrestricted = { max_price_cents: null, color: null, size: null, evidence_tags: [] };
  const empty = renderToStaticMarkup(<ShoppingPanel view={{ ...view, conditions: unrestricted }} busy={false} onNavigate={noop} />);
  expect(empty.match(/不限/g)).toHaveLength(4);
});

it("compares the selected pair in the provided order and disables all actions while busy", () => {
  const html = renderToStaticMarkup(<ShoppingPanel view={{ ...view, view: "compare", compare_skus: ["NAV-2", "NAV-1"] }}
    busy={true} onNavigate={noop} />);
  expect(html).toContain("shopping-compare");
  expect(html.indexOf("NAV-2")).toBeLessThan(html.indexOf("NAV-1"));
  expect(html).toContain("数据来自已核验详情，不代表独立检测");
  expect(html.match(/disabled=""/g)).toHaveLength(5);
});

it("highlights the selected product instead of the indexed card", () => {
  const html = renderToStaticMarkup(<ShoppingPanel view={{ ...view, view: "select", selected_sku: "NAV-1" }}
    busy={false} onNavigate={noop} />);
  expect(html).toContain("product-card-selected");
  expect(html).toContain("已记录选中");
  expect(html).toContain("NAV-1");
  expect(html).not.toContain("NAV-2");
});

it("renders shopping only below completed answers and disables history controls", () => {
  const html = renderToStaticMarkup(<MessageView
    conversation={{ id: "chat", title: "选购", updatedAt: 0, messages: [
      { id: "old", role: "assistant", content: "历史答复", state: "complete", shoppingView: view },
      { id: "new", role: "assistant", content: "生成中", state: "streaming", shoppingView: view },
    ] }}
    busy={false} onNavigate={noop} onConfirm={noop} onCancel={noop}
  />);
  expect(html.match(/aria-label="导购结果"/g)).toHaveLength(1);
  expect(html.indexOf("历史答复")).toBeLessThan(html.indexOf("当前条件"));
  expect(html.match(/disabled=""/g)).toHaveLength(4);
});
