"""Pinned Bitext transfer evaluation. Run python -m eval.bitext --help."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import random
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from eval.metrics import accuracy

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval/bitext"
REV = "72ea2203180d14a416b579c657033ecd21e59a11"
SOURCE = "https://github.com/bitext/customer-support-llm-chatbot-training-dataset"
CSV = "data/Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
SEED = 20261111
LIMIT = 1900
MAPPING = {
    "cancel_order": "cancel_order",
    "track_order": "order_inquiry",
    "get_refund": "refund",
    "track_refund": "refund",
    "check_refund_policy": "faq_policy",
    "check_cancellation_fee": "faq_policy",
    "delivery_options": "faq_policy",
    "delivery_period": "faq_policy",
    "check_invoice": "faq_policy",
    "get_invoice": "faq_policy",
    "check_payment_methods": "faq_policy",
    "complaint": "complaint",
    "contact_human_agent": "human_handoff",
}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / name
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(10):
        try:
            temporary.replace(target)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def read(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def normalize(text):
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\{\{.*?\}\}|\d+", " ENTITY ", text)
    return " ".join(re.findall(r"[a-z]+|[\u4e00-\u9fff]", text))


def groups(rows):
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    normalized = [(normalize(r["en"]), normalize(r.get("zh", ""))) for r in rows]
    stop = set("i my me the a an to can could would you please want need help with".split())
    tokens = [set(en.split()) - stop for en, _ in normalized]
    for i in range(len(rows)):
        for j in range(i):
            en1, zh1 = normalized[i]
            en2, zh2 = normalized[j]
            union = tokens[i] | tokens[j]
            overlap = len(tokens[i] & tokens[j]) / len(union) if union else 0
            similar = en1 == en2 or bool(zh1 and zh1 == zh2)
            if rows[i]["source_label"] == rows[j]["source_label"]:
                similar |= overlap >= 0.70
                similar |= SequenceMatcher(None, en1, en2).ratio() >= 0.85
                similar |= bool(zh1 and SequenceMatcher(None, zh1, zh2).ratio() >= 0.85)
            if similar:
                parent[find(i)] = find(j)
    result = defaultdict(list)
    for i, row in enumerate(rows):
        result[find(i)].append(row)
    return list(result.values())


def split(rows):
    grouped = groups(rows)
    rng = random.Random(SEED)
    rng.shuffle(grouped)
    reachable = {0: []}
    for index, group in enumerate(grouped):
        for count, chosen in list(reachable.items())[::-1]:
            new = count + len(group)
            if new <= 200 and new not in reachable:
                reachable[new] = chosen + [index]
    if 200 not in reachable:
        raise RuntimeError("Cannot split groups into exactly 200/300")
    dev = set(reachable[200])
    for index, group in enumerate(grouped):
        group_id = digest("|".join(sorted(r["id"] for r in group)).encode())[:16]
        for row in group:
            row["group_id"] = group_id
            row["split"] = "dev" if index in dev else "test"
    assert sum(r["split"] == "dev" for r in rows) == 200
    return rows


def summarize(rows):
    from app.core.enums import IntentLabel

    labels = [x.value for x in IntentLabel] + ["ERROR"]
    pairs = [(r["gold"], r["prediction"]) for r in rows]
    matrix = {gold: {pred: 0 for pred in labels} for gold in labels}
    for gold, pred in pairs:
        matrix[gold][pred] += 1
    metrics = {}
    for label in labels[:-1]:
        tp = matrix[label][label]
        support = sum(matrix[label].values())
        predicted = sum(matrix[g][label] for g in labels)
        p = tp / predicted if predicted else 0
        r = tp / support if support else 0
        metrics[label] = {
            "support": support,
            "precision": p,
            "recall": r,
            "f1": 2 * p * r / (p + r) if p + r else 0,
        }
    covered = [m for m in metrics.values() if m["support"]]
    return {
        "n": len(rows),
        "accuracy": accuracy(pairs),
        "failure_count": sum(r["prediction"] == "ERROR" for r in rows),
        "per_class": metrics,
        "macro_f1_supported_classes": sum(m["f1"] for m in covered) / len(covered),
        "confusion_matrix": matrix,
        "mismatches": [r for r in rows if r["gold"] != r["prediction"]],
        "human_review_excluded": summarize_clean(rows),
    }


def summarize_clean(rows):
    clean = [r for r in rows if not r["review_required"]]
    return {"n": len(clean), "accuracy": accuracy([(r["gold"], r["prediction"]) for r in clean])}


class Budget:
    def __init__(self):
        from app.config.settings import get_settings
        from app.providers.deepseek import DeepSeekProvider

        settings = get_settings().model_copy(
            update={"llm_max_retries": 1, "llm_timeout_seconds": 60}
        )
        if settings.deepseek_base_url.rstrip("/") not in {
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
        }:
            raise RuntimeError("Expected official DeepSeek endpoint")
        self.model = settings.deepseek_model
        self.provider = DeepSeekProvider(settings)
        self.ledger = (
            read("usage.json")
            if (OUT / "usage.json").exists()
            else {
                "attempts": 0,
                "tokens": 0,
                "failures": 0,
            }
        )
        self.lock = asyncio.Semaphore(4)
        self.last_failed = False

    async def generate(self, messages, **kwargs):
        async with self.lock:
            if self.ledger["attempts"] >= LIMIT:
                raise RuntimeError("API budget exhausted")
            self.ledger["attempts"] += 1
            save("usage.json", self.ledger)
            try:
                answer = await self.provider.generate(messages, **kwargs)
            except Exception:
                self.ledger["failures"] += 1
                save("usage.json", self.ledger)
                self.last_failed = True
                raise
            self.ledger["tokens"] += answer.usage.total_tokens
            save("usage.json", self.ledger)
            return answer

    async def structured(self, system, payload, max_tokens=3500):
        answer = await self.generate(
            [
                {"role": "system", "content": "Return JSON. " + system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return json.loads(answer.content)


async def prepare():
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        for path, filename in [
            (CSV, "source.csv"),
            ("LICENSE.txt", "LICENSE.txt"),
            ("README.md", "SOURCE_README.md"),
        ]:
            if not (OUT / filename).exists():
                response = await client.get(
                    f"https://raw.githubusercontent.com/bitext/customer-support-llm-chatbot-training-dataset/{REV}/{path}"
                )
                response.raise_for_status()
                (OUT / filename).write_bytes(response.content)
    source_bytes = (OUT / "source.csv").read_bytes()
    data = list(csv.DictReader(io.StringIO(source_bytes.decode("utf-8-sig"))))
    counts = Counter(row["intent"] for row in data)
    save(
        "manifest.json",
        {
            "source": SOURCE,
            "revision": REV,
            "source_file": CSV,
            "license": "CDLA-Sharing-1.0",
            "downloaded_at": datetime.now(UTC).isoformat(),
            "seed": SEED,
            "source_rows": len(data),
            "source_sha256": digest(source_bytes),
            "mapping": MAPPING,
            "unmapped": {k: v for k, v in counts.items() if k not in MAPPING},
            "sampling": "Uniform stratified by external intent; exact normalized English dedup before sampling",
            "grouping": "Connected components: exact EN/ZH; same source intent token Jaccard>=0.70 or EN/ZH SequenceMatcher>=0.85",
            "modifications": "Sampled, translated into Chinese, relabeled, similarity-group split; Bitext attribution retained",
            "limitations": "Synthetic English training source translated by the evaluation model; machine audit pending human review",
        },
    )
    if (OUT / "sample.json").exists():
        return
    rng = random.Random(SEED)
    strata = defaultdict(list)
    seen = set()
    for index, row in enumerate(data):
        if row["intent"] not in MAPPING:
            continue
        text = normalize(row["instruction"])
        if text in seen:
            continue
        seen.add(text)
        strata[row["intent"]].append(
            {
                "id": f"bitext-{index:05d}",
                "en": row["instruction"],
                "source_label": row["intent"],
                "label": MAPPING[row["intent"]],
            }
        )
    selected = []
    for i, label in enumerate(sorted(strata)):
        count = 500 // len(strata) + (i < 500 % len(strata))
        selected.extend(rng.sample(strata[label], count))
    save("sample.json", selected)


async def translate(budget):
    rows = read("sample.json")
    translated = read("translated.json") if (OUT / "translated.json").exists() else {}
    for start in range(0, 500, 20):
        batch = [r for r in rows[start : start + 20] if r["id"] not in translated]
        if not batch:
            continue
        result = await budget.structured(
            '将每条英文客服原话忠实翻译成自然中文。保留占位符、疑问、否定、拼写含糊的语义，不增添订单、退款等信息。输出 {"rows":[{"id":"原id","zh":"译文","note":"存在歧义时说明，否则空字符串"}]}。',
            [{"id": r["id"], "en": r["en"]} for r in batch],
        )
        assert {r["id"] for r in result["rows"]} == {r["id"] for r in batch}
        for row in result["rows"]:
            assert re.search(r"[\u4e00-\u9fff]", row["zh"])
            translated[row["id"]] = row
        save("translated.json", translated)
        completed = sum(r["id"] in translated for r in rows)
        print(f"translated {completed}/500", flush=True)
    audited = read("audit.json") if (OUT / "audit.json").exists() else {}
    for start in range(0, 500, 20):
        batch = [
            dict(r, **{k: v for k, v in translated[r["id"]].items() if k != "id"})
            for r in rows[start : start + 20]
            if r["id"] not in audited
        ]
        if not batch:
            continue
        result = await budget.structured(
            '独立检查英中翻译是否漏掉/添加语义，外部标签到本地label是否含糊。特别检查 track_order 的订单状态vs物流，check_invoice的政策vs具体查询，complaint的投诉vs售后，退款政策vs退款申请。输出 {"rows":[{"id":"原id","translation_ok":true,"label_ambiguous":false,"reason":"简短原因或空"}]}。只审核，不改译文。',
            batch,
        )
        assert {r["id"] for r in result["rows"]} == {r["id"] for r in batch}
        audited.update({r["id"]: r for r in result["rows"]})
        save("audit.json", audited)
    for row in rows:
        row["zh"] = translated[row["id"]]["zh"]
        audit = audited[row["id"]]
        reasons = [translated[row["id"]].get("note", ""), audit.get("reason", "")]
        row["review_required"] = bool(
            translated[row["id"]].get("note")
            or not audit["translation_ok"]
            or audit["label_ambiguous"]
        )
        row["review_reasons"] = reasons
    if len({normalize(r["zh"]) for r in rows}) != 500:
        raise RuntimeError("Duplicate Chinese translations. Run repair, then translate again.")
    if not (OUT / "frozen.json").exists():
        save("frozen.json", split(rows))
    save("human_review.json", [r for r in read("frozen.json") if r["review_required"]])
    frozen = read("frozen.json")
    dev = [r for r in frozen if r["split"] == "dev"]
    test = [r for r in frozen if r["split"] == "test"]
    assert len(dev) == 200 and len(test) == 300
    assert not {r["group_id"] for r in dev} & {r["group_id"] for r in test}
    save(
        "split_stats.json",
        {
            "frozen_sha256": digest((OUT / "frozen.json").read_bytes()),
            "dev": dict(Counter(r["source_label"] for r in dev)),
            "test": dict(Counter(r["source_label"] for r in test)),
            "groups": len({r["group_id"] for r in frozen}),
            "pending_human_review": sum(r["review_required"] for r in frozen),
        },
    )


def repair():
    """Archive invalid runs and replace Chinese duplicates within source strata."""
    rows = read("sample.json")
    translated = read("translated.json")
    seen = set()
    duplicate_indices = []
    for i, row in enumerate(rows):
        key = normalize(translated[row["id"]]["zh"])
        if key in seen:
            duplicate_indices.append(i)
        seen.add(key)
    if not duplicate_indices:
        return
    source = list(csv.DictReader(io.StringIO((OUT / "source.csv").read_text(encoding="utf-8-sig"))))
    used = {normalize(r["en"]) for r in rows}
    rng = random.Random(SEED + 1)
    for i in duplicate_indices:
        options = [
            (index, row)
            for index, row in enumerate(source)
            if row["intent"] == rows[i]["source_label"]
            and normalize(row["instruction"]) not in used
            and f"bitext-{index:05d}" not in translated
        ]
        index, row = rng.choice(options)
        used.add(normalize(row["instruction"]))
        rows[i] = {
            "id": f"bitext-{index:05d}",
            "en": row["instruction"],
            "source_label": row["intent"],
            "label": MAPPING[row["intent"]],
        }
    archive = OUT / ("discarded-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S"))
    archive.mkdir()
    for name in [
        "sample.json",
        "frozen.json",
        "split_stats.json",
        "human_review.json",
        "candidate_prompt.json",
        "predictions-bitext_baseline_v3.json",
        "predictions-bitext_dev_v1.json",
    ]:
        path = OUT / name
        if path.exists():
            path.replace(archive / name)
    save("sample.json", rows)
    manifest = read("manifest.json")
    manifest.setdefault("dedup_repairs", []).append(
        {
            "replacement_count": len(duplicate_indices),
            "replacement_seed": SEED + 1,
            "discarded_run": archive.name,
            "reason": "Exact normalized Chinese duplicates",
        }
    )
    save("manifest.json", manifest)


async def classify(budget, rows, version, prompt):
    from app.agents.nodes.intent import IntentClassifier
    from app.agents.nodes.intent_overlay import apply_intent_overlay

    filename = f"predictions-{version}.json"
    predictions = read(filename) if (OUT / filename).exists() else {}

    class ObservedProvider:
        def __init__(self):
            self.failed = False

        async def generate(self, *args, **kwargs):
            try:
                return await budget.generate(*args, **kwargs)
            except Exception:
                self.failed = True
                raise

    async def one(row):
        if row["id"] in predictions:
            return
        observed = ObservedProvider()
        classifier = IntentClassifier(
            observed, system_prompt=prompt, prompt_version=version, apply_overlay=False
        )
        parse_failed = False
        try:
            result = await classifier.classify(row["zh"])
            parse_failed = result.prompt_version == "intent_rules_v1"
            raw = "ERROR" if observed.failed or parse_failed else result.label.value
            overlay = apply_intent_overlay(row["zh"], result.label)[0].value
        except Exception:
            raw, overlay = "ERROR", "ERROR"
        predictions[row["id"]] = {
            "id": row["id"],
            "query": row["zh"],
            "gold": row["label"],
            "raw": raw,
            "overlay": overlay if raw != "ERROR" else "ERROR",
            "review_required": row["review_required"],
            "split": row["split"],
        }
        save(filename, predictions)
        if len(predictions) % 25 == 0:
            print(f"{version} classified {len(predictions)}", flush=True)

    await asyncio.gather(*(one(row) for row in rows))
    return predictions


async def evaluate(budget):
    from app.agents.nodes.intent import rule_based_intent
    from app.prompts.intent import INTENT_SYSTEM

    frozen = read("frozen.json")
    dev = [r for r in frozen if r["split"] == "dev"]
    test = [r for r in frozen if r["split"] == "test"]
    baseline = await classify(budget, dev, "bitext_baseline_v3", INTENT_SYSTEM)
    if not (OUT / "candidate_prompt.json").exists():
        dev_errors = [p for p in baseline.values() if p["gold"] != p["raw"]]
        draft = await budget.structured(
            '你只收到200条调试集中的错例。针对这些错误改进中文电商意图分类system prompt，保留原有14个标签及JSON格式。不要记忆具体句子，用通用判定规则。输出 {"system":"完整新提示词"}。',
            {"baseline": INTENT_SYSTEM, "mapping_contract": MAPPING, "dev_errors": dev_errors},
            max_tokens=4500,
        )
        from app.core.enums import IntentLabel

        assert all(label.value in draft["system"] for label in IntentLabel)
        save(
            "candidate_prompt.json",
            {
                "version": "bitext_dev_v1",
                "system": draft["system"],
                "sha256": digest(draft["system"].encode()),
                "dev_error_count": len(dev_errors),
                "dev_only": True,
                "frozen_before_test": True,
            },
        )
    candidate = read("candidate_prompt.json")
    await classify(budget, dev, "bitext_dev_v1", candidate["system"])
    await classify(budget, test, "bitext_baseline_v3", INTENT_SYSTEM)
    await classify(budget, test, "bitext_dev_v1", candidate["system"])
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": budget.model,
        "dataset": "Bitext Chinese translation",
        "n": 500,
        "dev_n": 200,
        "test_n": 300,
        "seed": SEED,
        "manifest": read("manifest.json"),
        "split": read("split_stats.json"),
        "baseline_prompt_sha256": digest(INTENT_SYSTEM.encode()),
        "candidate_prompt_sha256": candidate["sha256"],
        "results": {},
    }
    for version in ["bitext_baseline_v3", "bitext_dev_v1"]:
        predictions = read(f"predictions-{version}.json")
        for split_name in ["dev", "test"]:
            for overlay in [False, True]:
                selected = [
                    dict(p, prediction=p["overlay" if overlay else "raw"])
                    for p in predictions.values()
                    if p["split"] == split_name
                ]
                report["results"][f"{version}/{split_name}/overlay={overlay}"] = summarize(selected)
    for split_name in ["dev", "test"]:
        rules = [
            dict(
                gold=r["label"],
                prediction=rule_based_intent(r["zh"]).label.value,
                review_required=r["review_required"],
                id=r["id"],
                query=r["zh"],
            )
            for r in frozen
            if r["split"] == split_name
        ]
        report["results"][f"rules/{split_name}"] = summarize(rules)
    report["usage"] = budget.ledger
    save("report.json", report)
    print("Bitext 500 evaluation complete", flush=True)


def export():
    from importlib.metadata import version

    from app.prompts.intent import INTENT_SYSTEM

    rows = read("frozen.json")
    save(
        "experiment.json",
        {
            "baseline_system": INTENT_SYSTEM,
            "baseline_system_sha256": digest(INTENT_SYSTEM.encode()),
            "frozen_sha256": digest((OUT / "frozen.json").read_bytes()),
            "overlay_sha256": digest((ROOT / "app/agents/nodes/intent_overlay.py").read_bytes()),
            "parameters": {
                "temperature": 0.0,
                "classification_max_tokens": 256,
                "concurrency": 4,
                "request_limit": LIMIT,
                "timeout_seconds": 60,
            },
            "dependencies": {name: version(name) for name in ["httpx", "pydantic", "langgraph"]},
            "model_version_note": "Configured deepseek-chat alias; server snapshot not supplied by provider",
        },
    )
    fields = [
        "id",
        "en",
        "zh",
        "source_label",
        "label",
        "group_id",
        "split",
        "review_required",
        "review_reasons",
    ]
    for name, selected in [
        ("samples.csv", rows),
        ("human_review.csv", [r for r in rows if r["review_required"]]),
    ]:
        with (OUT / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(selected)


async def main(stage):
    if stage == "export":
        export()
        return
    if stage == "repair":
        repair()
        return
    if stage == "prepare":
        await prepare()
        return
    if stage == "validate":
        rows = read("frozen.json")
        assert len(rows) == len({r["id"] for r in rows}) == 500
        assert len({normalize(r["en"]) for r in rows}) == 500
        assert len({normalize(r["zh"]) for r in rows}) == 500
        assert not {r["group_id"] for r in rows if r["split"] == "dev"} & {
            r["group_id"] for r in rows if r["split"] == "test"
        }
        print(json.dumps(read("split_stats.json"), ensure_ascii=False))
        return
    budget = Budget()
    try:
        if stage == "translate":
            await translate(budget)
        elif stage == "evaluate":
            await evaluate(budget)
    finally:
        await budget.provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=["prepare", "translate", "repair", "evaluate", "validate", "export"]
    )
    asyncio.run(main(parser.parse_args().stage))
