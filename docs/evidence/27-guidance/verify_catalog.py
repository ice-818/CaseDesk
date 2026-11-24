"""Assertion-style verification of the synthetic catalog in app_merchant.db.

Run from anywhere:
    .\\.venv\\Scripts\\python.exe docs\\evidence\\27-guidance\\verify_catalog.py
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DB_PATH = ROOT / "data" / "local" / "app_merchant.db"


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    return condition


def main() -> int:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    results: list[bool] = []

    total = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    results.append(check("total products is 24", total == 24, str(total)))

    by_merchant = dict(
        connection.execute(
            "SELECT merchant_id, COUNT(*) FROM products GROUP BY merchant_id"
        ).fetchall()
    )
    results.append(check("each merchant has 12 products", by_merchant == {
        "m_demo": 12,
        "m_second": 12,
    }, str(by_merchant)))

    zero = dict(
        connection.execute(
            "SELECT merchant_id, COUNT(*) FROM products WHERE inventory = 0 GROUP BY merchant_id"
        ).fetchall()
    )
    results.append(check("each merchant has 2 zero-inventory rows", zero == {
        "m_demo": 2,
        "m_second": 2,
    }, str(zero)))

    sellable = dict(
        connection.execute(
            "SELECT merchant_id, COUNT(*) FROM products WHERE inventory > 0 GROUP BY merchant_id"
        ).fetchall()
    )
    results.append(check("each merchant has 10 sellable rows", sellable == {
        "m_demo": 10,
        "m_second": 10,
    }, str(sellable)))

    by_category = connection.execute(
        "SELECT category, COUNT(*) FROM products GROUP BY category ORDER BY category"
    ).fetchall()
    results.append(check("three categories populated", len(by_category) == 3,
                         str(dict(by_category))))

    audio_price = connection.execute(
        "SELECT price_cents FROM products WHERE sku = 'SKU-AUDIO-01'"
    ).fetchone()[0]
    results.append(check("yuan converted to cents (569 -> 56900)", audio_price == 56900,
                         str(audio_price)))

    purple_44 = connection.execute(
        "SELECT COUNT(*) FROM products "
        "WHERE tags LIKE '%color:紫%' AND tags LIKE '%size:44%'"
    ).fetchone()[0]
    results.append(check("no purple/size44 combination (no-stock case)", purple_44 == 0,
                         str(purple_44)))

    purple_any = connection.execute(
        "SELECT COUNT(*) FROM products WHERE tags LIKE '%color:紫%'"
    ).fetchone()[0]
    results.append(check("no purple product at all", purple_any == 0, str(purple_any)))

    size_44 = connection.execute(
        "SELECT COUNT(*) FROM products WHERE tags LIKE '%size:44%'"
    ).fetchone()[0]
    results.append(check("size44 exists so the no-stock case is genuinely about color",
                         size_44 >= 1, str(size_44)))

    apparel_rows = connection.execute(
        "SELECT sku, tags FROM products WHERE category = '服饰'"
    ).fetchall()
    missing_tags = [
        row["sku"]
        for row in apparel_rows
        if not ("color:" in row["tags"] and "size:" in row["tags"])
    ]
    results.append(check(
        "new apparel rows carry color:/size:; only legacy SKU-TEE is exempt",
        missing_tags == ["SKU-TEE"], f"missing={missing_tags} of {len(apparel_rows)} apparel"
    ))

    synonym_rows = connection.execute(
        "SELECT COUNT(*) FROM products WHERE tags LIKE '%耳机,耳麦%'"
    ).fetchone()[0]
    results.append(check("synonym tags present (耳机,耳麦)", synonym_rows >= 1,
                         str(synonym_rows)))

    invalid = connection.execute(
        "SELECT COUNT(*) FROM products WHERE price_cents < 0 OR inventory < 0"
    ).fetchone()[0]
    results.append(check("no negative price or inventory", invalid == 0, str(invalid)))

    distinct_skus = connection.execute("SELECT COUNT(DISTINCT sku) FROM products").fetchone()[0]
    results.append(check("all SKUs globally unique", distinct_skus == total,
                         f"{distinct_skus}/{total}"))

    connection.close()

    print("\n" + ("ALL CHECKS PASSED" if all(results) else "SOME CHECKS FAILED"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
