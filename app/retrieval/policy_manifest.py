"""Explicit legacy registry plus controlled policy directories. No guessed tenant ownership."""

import hashlib
import json
from pathlib import Path

from app.retrieval.policy_scope import FORMAT_VERSION

LEGACY_DOCUMENTS: dict[str, dict[str, str | None]] = {
    "account_security.md": {"policy_layer": "platform", "applicable_category": None},
    "complaint_sla.md": {"policy_layer": "platform", "applicable_category": None},
    "coupons.md": {"policy_layer": "platform", "applicable_category": None},
    "cross_border.md": {"policy_layer": "platform", "applicable_category": None},
    "exchange_size.md": {"policy_layer": "platform", "applicable_category": None},
    "faq.md": {"policy_layer": "platform", "applicable_category": None},
    "gift_card.md": {"policy_layer": "platform", "applicable_category": None},
    "handoff_hours.md": {"policy_layer": "platform", "applicable_category": None},
    "install_appliance.md": {"policy_layer": "platform", "applicable_category": None},
    "installment_huabei.md": {"policy_layer": "platform", "applicable_category": None},
    "invoice_vat.md": {"policy_layer": "platform", "applicable_category": None},
    "live_presale.md": {"policy_layer": "platform", "applicable_category": None},
    "logistics_exception.md": {"policy_layer": "platform", "applicable_category": None},
    "membership_plus.md": {"policy_layer": "platform", "applicable_category": None},
    "missing_wrong_item.md": {"policy_layer": "platform", "applicable_category": None},
    "packaging_gift.md": {"policy_layer": "platform", "applicable_category": None},
    "payment_methods.md": {"policy_layer": "platform", "applicable_category": None},
    "pickup_return.md": {"policy_layer": "platform", "applicable_category": None},
    "points_expiry.md": {"policy_layer": "platform", "applicable_category": None},
    "price_protect.md": {"policy_layer": "platform", "applicable_category": None},
    "privacy_cs.md": {"policy_layer": "platform", "applicable_category": None},
    "promo_stacking.md": {"policy_layer": "platform", "applicable_category": None},
    "refund_timeline.md": {"policy_layer": "platform", "applicable_category": None},
    "repair_onsite.md": {"policy_layer": "platform", "applicable_category": None},
    "restock_notify.md": {"policy_layer": "platform", "applicable_category": None},
    "return_apparel.md": {"policy_layer": "category", "applicable_category": "apparel"},
    "return_custom.md": {"policy_layer": "platform", "applicable_category": None},
    "return_electronics.md": {"policy_layer": "category", "applicable_category": "electronics"},
    "return_food.md": {"policy_layer": "category", "applicable_category": "food"},
    "return_policy.md": {"policy_layer": "platform", "applicable_category": None},
    "shipping.md": {"policy_layer": "platform", "applicable_category": None},
    "shipping_address.md": {"policy_layer": "platform", "applicable_category": None},
    "shipping_cod.md": {"policy_layer": "platform", "applicable_category": None},
    "shipping_remote.md": {"policy_layer": "platform", "applicable_category": None},
    "shipping_split.md": {"policy_layer": "platform", "applicable_category": None},
    "size_guide.md": {"policy_layer": "platform", "applicable_category": None},
    "warranty.md": {"policy_layer": "platform", "applicable_category": None},
    "warranty_battery.md": {"policy_layer": "category", "applicable_category": "battery"},
    "warranty_jewelry.md": {"policy_layer": "category", "applicable_category": "jewelry"},
}


def document_scope(path: Path, root: Path, merchants: frozenset[str]):
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_relative_to(root.resolve()):
        raise ValueError("policy file escapes knowledge directory")
    relative = path.relative_to(root)
    if any(part.startswith(".") for part in relative.parts):
        raise ValueError("hidden policy path")
    parts = relative.parts
    if len(parts) == 1:
        if parts[0] not in LEGACY_DOCUMENTS:
            raise ValueError("unregistered legacy policy document: " + parts[0])
        result = dict(LEGACY_DOCUMENTS[parts[0]])
        result["merchant_id"] = None
    elif len(parts) == 2 and parts[0] == "platform":
        result = {"policy_layer": "platform", "merchant_id": None, "applicable_category": None}
    elif len(parts) == 3 and parts[0] == "categories" and parts[1]:
        result = {"policy_layer": "category", "merchant_id": None, "applicable_category": parts[1]}
    elif len(parts) == 3 and parts[0] == "merchants" and parts[1] in merchants:
        result = {"policy_layer": "merchant", "merchant_id": parts[1], "applicable_category": None}
    else:
        raise ValueError("invalid or unknown policy ownership: " + relative.as_posix())
    return {**result, "policy_scope_version": FORMAT_VERSION, "policy_version": "unresolved"}


def corpus_fingerprint(documents):
    values = sorted(
        (relative, hashlib.sha256(content).hexdigest(), metadata)
        for relative, content, metadata in documents
    )
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


async def known_merchants(factory):
    from sqlalchemy import select

    from app.repositories.models import MerchantRow

    async with factory() as db:
        return frozenset((await db.scalars(select(MerchantRow.merchant_id))).all())
