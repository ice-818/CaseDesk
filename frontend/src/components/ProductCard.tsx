import type { ShoppingViewProduct } from "../types";

const featureLabels: Record<string, string> = {
  relevance: "相关性",
  price_fit: "价格符合度",
  inventory: "库存评分",
  tag_match: "标签匹配",
};

interface ProductCardProps {
  product: ShoppingViewProduct;
  selected?: boolean;
  busy?: boolean;
  onSelect: () => void;
}

export function ProductCard({ product, selected, busy, onSelect }: ProductCardProps) {
  const { position, name, sku, price_cents, inventory, tags, feature_scores, total_score } = product;
  const features = Object.entries(feature_scores);
  const label = position == null ? name : `第 ${position} 个商品：${name}`;

  return <article className={`product-card${selected ? " product-card-selected" : ""}`} aria-label={label}>
    <div className="product-card-heading">
      <h3>{position == null ? name : `第 ${position} 个 · ${name}`}</h3>
      {selected && <span className="product-card-selected-label">已记录选中</span>}
    </div>
    <div className="product-card-sku">SKU：{sku}</div>
    <div className="product-card-facts">
      <strong>{price_cents == null ? "价格未提供" : `${(price_cents / 100).toFixed(2)} 元`}</strong>
      <span>{inventory == null ? "库存未提供" : `库存 ${inventory} 件`}</span>
    </div>
    {!!tags?.length && <ul className="product-card-tags" aria-label="规格标签">
      {tags.map((tag, index) => <li key={`${tag}-${index}`}>{tag}</li>)}
    </ul>}
    {(features.length > 0 || total_score != null) && <dl className="product-scores" aria-label="推荐评分">
      {features.map(([key, value]) => <div key={key}>
        <dt>{featureLabels[key] ?? key}</dt><dd>{value.toFixed(3)}</dd>
      </div>)}
      {total_score != null && <div><dt>总分</dt><dd>{total_score.toFixed(3)}</dd></div>}
    </dl>}
    <button type="button" className="shopping-button product-card-action" disabled={busy || position == null}
      aria-label={`选择${label}`} onClick={onSelect}>选这个</button>
  </article>;
}
