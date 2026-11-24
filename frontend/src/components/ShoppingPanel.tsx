import { navigationText, selectText } from "../lib/shoppingCommands";
import type { ShoppingView, ShoppingViewProduct } from "../types";
import { ProductCard } from "./ProductCard";

interface ShoppingPanelProps {
  view: ShoppingView;
  busy: boolean;
  onNavigate: (text: string) => void;
}

export function ShoppingPanel({ view, busy, onNavigate }: ShoppingPanelProps) {
  const conditions = view.conditions;
  const current = view.products[view.display_index];
  const bySku = new Map(view.products.map((product) => [product.sku, product]));
  const selected = view.selected_sku ? bySku.get(view.selected_sku) : undefined;
  const visible = view.view === "compare"
    ? view.compare_skus.map((sku) => bySku.get(sku))
    : [view.view === "select" ? selected : current];
  const products = visible.filter((product): product is ShoppingViewProduct => product !== undefined);

  return <section className="shopping-panel" aria-label="导购结果">
    <div className="shopping-conditions">
      <h3>当前条件</h3>
      <dl>
        <div><dt>预算</dt><dd>{conditions.max_price_cents == null ? "不限" : `${(conditions.max_price_cents / 100).toFixed(2)} 元以内`}</dd></div>
        <div><dt>颜色</dt><dd>{conditions.color ?? "不限"}</dd></div>
        <div><dt>尺码</dt><dd>{conditions.size ?? "不限"}</dd></div>
        <div><dt>属性</dt><dd>{conditions.evidence_tags.join("、") || "不限"}</dd></div>
      </dl>
    </div>
    {view.notice && <p className="shopping-notice" role="status">{view.notice}</p>}
    <div className={view.view === "compare" ? "shopping-cards shopping-compare" : "shopping-cards"}>
      {products.map((product) => <ProductCard key={product.sku} product={product} busy={busy}
        selected={view.view === "select" && product.sku === view.selected_sku}
        onSelect={() => {
          if (product.position != null) onNavigate(selectText(product.position));
        }} />)}
    </div>
    {view.view === "compare" && <p className="shopping-note">数据来自已核验详情，不代表独立检测。</p>}
    <div className="shopping-actions" role="group" aria-label="导购导航">
      {(["previous", "next", "compare"] as const).map((kind) => {
        const text = navigationText(kind);
        return <button key={kind} type="button" className="shopping-button" disabled={busy}
          aria-label={kind === "compare" ? "对比当前与下一个商品" : text}
          onClick={() => onNavigate(text)}>{text}</button>;
      })}
    </div>
  </section>;
}
