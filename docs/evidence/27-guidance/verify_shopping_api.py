"""End-to-end HTTP acceptance for the read-only shopping navigation.

Runs against the live API on 8002 (app_merchant.db, offline deterministic LLM).
It verifies the structured shopping_view contract across a real multi-turn flow:
initial -> next -> compare -> select, including stable positions and conditions.
"""

import sys

import httpx

BASE = "http://127.0.0.1:8002/api/v1"


def show(view, tag):
    print(f"===== {tag} =====")
    if not view:
        print("no shopping_view")
        return
    print(
        "view={view} display_index={i} stop={stop} selected={sel} compare={cmp}".format(
            view=view["view"],
            i=view["display_index"],
            stop=view["stop_reason"],
            sel=view["selected_sku"],
            cmp=view["compare_skus"],
        )
    )
    c = view["conditions"]
    print(
        "conditions: budget={b} color={color} size={size} tags={tags}".format(
            b=c["max_price_cents"],
            color=c["color"],
            size=c["size"],
            tags=c["evidence_tags"],
        )
    )
    for p in view["products"]:
        print(
            "  pos={pos} sku={sku} price={price} stock={stock} total={total}".format(
                pos=p["position"],
                sku=p["sku"],
                price=p["price_cents"],
                stock=p["inventory"],
                total=p["total_score"],
            )
        )


def main():
    failures = []
    with httpx.Client(timeout=30) as client:
        login = client.post(
            f"{BASE}/auth/login",
            json={"user_id": "u_demo", "password": "Buyer-Demo-2026"},
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        def chat(message, session_id=None):
            payload = {"message": message}
            if session_id:
                payload["session_id"] = session_id
            resp = client.post(f"{BASE}/chat", headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

        initial = chat("推荐300元以内黑色42码鞋")
        sid = initial["session_id"]
        init_view = initial["shopping_view"]
        show(init_view, "initial")

        nxt = chat("换一个", sid)
        next_view = nxt["shopping_view"]
        show(next_view, "after next")

        back = chat("上一个", sid)
        back_view = back["shopping_view"]
        show(back_view, "back to first")

        cmp = chat("对比", sid)
        cmp_view = cmp["shopping_view"]
        show(cmp_view, "compare")

        sel = chat("选第1个", sid)
        sel_view = sel["shopping_view"]
        show(sel_view, "select")

        # Assertions
        init_skus = sorted(p["sku"] for p in init_view["products"])
        next_skus = sorted(p["sku"] for p in next_view["products"])
        if len(init_skus) < 2:
            failures.append("expected at least 2 verified products for navigation")
        if init_skus != next_skus:
            failures.append("verified product set changed across turns")
        if next_view["display_index"] != 1:
            failures.append("next did not advance display_index to 1")
        if back_view["display_index"] != 0:
            failures.append("previous did not return display_index to 0")
        if cmp_view["view"] != "compare" or len(cmp_view["compare_skus"]) != 2:
            failures.append("compare did not produce compare view with two skus")
        if sel_view["selected_sku"] is None:
            failures.append("select did not record selected_sku")
        if init_view["conditions"]["max_price_cents"] != 30000:
            failures.append("conditions budget not preserved")

    print("===== assertions =====")
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("ALL PASS: multi-turn navigation, stable positions, conditions preserved")


if __name__ == "__main__":
    main()
