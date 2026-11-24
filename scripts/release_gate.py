"""Run the CaseDesk release checks before deployment.

From the project root with the virtual environment active:
    python scripts/release_gate.py
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "evidence" / "29-evaluation"
TIMEOUT_SECONDS = 300
REQUIRED_MARKERS = {"redteam", "fault"}


def load_config(filename: str) -> dict[str, Any]:
    path = EVIDENCE / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{filename} 读取失败：{exc}") from exc
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError(f"{filename} 需要 schema_version=1 的 JSON 对象")
    return data


def run_pytest(*args: str) -> str:
    env = os.environ.copy()
    # Keep local shell options from changing selection or turning execution into collection.
    env.pop("PYTEST_ADDOPTS", None)
    env.update(PYTHONIOENCODING="utf-8", PY_COLORS="0", MCP_REMOTE_URL="")
    command = [sys.executable, "-m", "pytest", *args]
    try:
        result = subprocess.run(
            command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"pytest 超过 {TIMEOUT_SECONDS} 秒（{' '.join(args)}）") from exc
    except OSError as exc:
        raise ValueError(f"无法启动 pytest：{exc}") from exc
    if result.returncode != 0:
        print(result.stdout.rstrip())
        raise ValueError(f"pytest 退出码 {result.returncode}（{' '.join(args)}）")
    return result.stdout


def normalize_node_id(node: str) -> str:
    path, separator, test = node.partition("::")
    return path.replace("\\", "/") + separator + test


def node_is_collected(target: str, nodes: set[str]) -> bool:
    """精确匹配，或匹配以 target 为基名的参数化用例（target[param]）。"""
    if target in nodes:
        return True
    return any(node.startswith(target + "[") for node in nodes)


def missing_regressions(registry: dict[str, Any], nodes: set[str]) -> list[str]:
    defects = registry.get("defects")
    if not isinstance(defects, list):
        raise ValueError("defect_registry.json 的 defects 必须是数组")
    problems = []
    for index, defect in enumerate(defects):
        if not isinstance(defect, dict):
            raise ValueError(f"defects[{index}] 必须是对象")
        if defect.get("severity") not in ("P0", "P1") or defect.get("status") != "fixed":
            continue
        bug_id = defect.get("bug_id") or f"defects[{index}]"
        test = defect.get("regression_test")
        if not isinstance(test, str) or not test.strip():
            problems.append(f"{bug_id} 缺少 regression_test")
        elif not node_is_collected(normalize_node_id(test.strip()), nodes):
            problems.append(f"{bug_id} 未收集到 {test}")
    return problems


def report(label: str, passed: bool, detail: str) -> bool:
    print(f"{label} ... {'PASS' if passed else 'FAIL'} | {detail}", flush=True)
    return passed


def main() -> int:
    print(f"项目根路径：{ROOT}", flush=True)
    print(f"检查时间：{datetime.now().astimezone().isoformat(timespec='seconds')}", flush=True)
    results = []

    label = "[检查一] 缺陷回归覆盖完整"
    collection_error = None
    actual = None
    try:
        output = run_pytest("--collect-only", "-q")
        node_lines = [normalize_node_id(line.strip()) for line in output.splitlines() if "::" in line]
        summary = re.findall(r"(?m)^\s*(\d+)\s+tests? collected\b", output)
        actual = int(summary[-1]) if summary else len(node_lines)
    except ValueError as exc:
        collection_error = str(exc)
    try:
        registry = load_config("defect_registry.json")
        if collection_error:
            raise ValueError(collection_error)
        problems = missing_regressions(registry, set(node_lines))
        results.append(report(label, not problems, "；".join(problems) or "已修复 P0/P1 的回归 node ID 均已收集"))
    except ValueError as exc:
        results.append(report(label, False, str(exc)))

    label = "[检查二] 测试总数无未解释下降"
    baseline: dict[str, Any] = {}
    baseline_error = None
    try:
        baseline = load_config("release_baseline.json")
    except ValueError as exc:
        baseline_error = str(exc)
    expected = baseline.get("expected_total_tests")
    if baseline_error:
        results.append(report(label, False, baseline_error))
    elif type(expected) is not int or expected <= 0:
        results.append(report(label, False, "expected_total_tests 必须是正整数"))
    elif actual is None:
        results.append(report(label, False, f"实际数未知，基线 {expected} 项；{collection_error}"))
    else:
        results.append(report(label, actual >= expected, f"实际 {actual} 项，基线 {expected} 项"))

    label = "[检查三] 发布关键标记测试全部通过"
    markers = baseline.get("release_markers")
    ran_markers: set[str] = set()
    try:
        if baseline_error:
            raise ValueError(baseline_error)
        if not isinstance(markers, list) or not markers or any(
            not isinstance(marker, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", marker)
            or marker in {"and", "or", "not"} for marker in markers
        ):
            raise ValueError("release_markers 必须是非空的 pytest 标记名数组")
        expression = " or ".join(markers)
        output = run_pytest("-q", "-m", expression)
        print(output.rstrip())
        ran_markers = set(markers)
        results.append(report(label, True, expression))
    except ValueError as exc:
        results.append(report(label, False, str(exc)))

    label = "[检查四] 未授权副作用为零"
    configured = {marker for marker in markers if isinstance(marker, str)} if isinstance(markers, list) else set()
    missing = REQUIRED_MARKERS - configured
    if missing:
        results.append(report(label, False, f"门禁配置缺少标记：{', '.join(sorted(missing))}"))
    elif not REQUIRED_MARKERS <= ran_markers:
        results.append(report(label, False, "检查三未通过，无副作用断言的通过证据"))
    else:
        results.append(report(label, True, "未授权副作用=0 由 redteam/fault 快照断言保证"))

    print(f"汇总：{sum(results)}/{len(results)} 项通过")
    passed = all(results)
    print(f"RELEASE GATE: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
