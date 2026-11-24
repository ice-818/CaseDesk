"""Operator-only persistence for platform datasets and evaluations."""

import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.data_scope import require_data_scope
from app.core.exceptions import AppError
from app.core.ids import new_id
from app.repositories.models import DatasetVersionRow, EvaluationRunRow
from app.repositories.scope_policy import require_operator
from app.schemas.platform import DatasetCreate
from app.services.offline_evaluation import evaluate, execution_manifest


def dataset_view(row, *, detail=False):
    payload = json.loads(row.payload_json)
    result = {
        "dataset_id": row.dataset_id,
        "name": row.name,
        "version": row.version,
        "task": row.task,
        "split": row.split,
        "status": row.status,
        "content_hash": row.content_hash,
        "sample_count": len(payload["samples"]),
        "source": payload["source"],
        "source_type": payload["source_type"],
        "license": payload["license"],
        "reviewer": row.reviewer,
        "review_note": row.review_note,
    }
    if detail:
        result["payload"] = payload
    return result


def run_view(row):
    return {
        "run_id": row.run_id,
        "dataset_id": row.dataset_id,
        "candidate_label": row.candidate_label,
        "status": row.status,
        "manifest": json.loads(row.manifest_json),
        "report": json.loads(row.report_json),
    }


class PlatformRepository:
    def __init__(self, factory):
        self.factory = factory
        self.scope = require_data_scope()
        require_operator(self.scope)

    async def datasets(self, limit=50):
        require_operator(self.scope)
        async with self.factory() as db:
            rows = await db.scalars(
                select(DatasetVersionRow).order_by(DatasetVersionRow.created_at.desc()).limit(limit)
            )
            return {"datasets": [dataset_view(row) for row in rows]}

    async def create_dataset(self, payload):
        require_operator(self.scope)
        canonical = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self.factory() as db:
            previous = await db.scalars(
                select(DatasetVersionRow).where(DatasetVersionRow.name == payload.name)
            )
            groups = {sample.group_id for sample in payload.samples}
            for row in previous:
                if row.task != payload.task:
                    raise AppError("dataset family cannot change task type", http_status=409)
                if row.split != payload.split:
                    old_groups = {
                        sample["group_id"] for sample in json.loads(row.payload_json)["samples"]
                    }
                    if groups & old_groups:
                        raise AppError("sample groups overlap across splits", http_status=409)
            row = DatasetVersionRow(
                dataset_id=new_id("ds_"),
                name=payload.name,
                version=payload.version,
                task=payload.task,
                split=payload.split,
                content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                payload_json=canonical,
            )
            db.add(row)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise AppError("dataset name/version already exists", http_status=409) from exc
            return dataset_view(row, detail=True)

    async def dataset_detail(self, dataset_id):
        require_operator(self.scope)
        async with self.factory() as db:
            row = await db.get(DatasetVersionRow, dataset_id)
            if row is None:
                raise AppError("dataset not found", http_status=404)
            return dataset_view(row, detail=True)

    async def review_dataset(self, dataset_id, payload):
        require_operator(self.scope)
        async with self.factory() as db:
            result = await db.execute(
                update(DatasetVersionRow)
                .where(
                    DatasetVersionRow.dataset_id == dataset_id, DatasetVersionRow.status == "draft"
                )
                .values(
                    status=payload.decision,
                    reviewer=self.scope.actor_user_id,
                    review_note=payload.note,
                )
            )
            if result.rowcount != 1:
                raise AppError(
                    "dataset missing or already reviewed; publish a new version to revise",
                    http_status=409,
                )
            await db.commit()
            return dataset_view(await db.get(DatasetVersionRow, dataset_id), detail=True)

    async def runs(self, limit=50):
        require_operator(self.scope)
        async with self.factory() as db:
            rows = await db.scalars(
                select(EvaluationRunRow).order_by(EvaluationRunRow.created_at.desc()).limit(limit)
            )
            return {"runs": [run_view(row) for row in rows]}

    async def create_run(self, payload):
        require_operator(self.scope)
        async with self.factory() as db:
            dataset_row = await db.get(DatasetVersionRow, payload.dataset_id)
            if dataset_row is None:
                raise AppError("dataset not found", http_status=404)
            if dataset_row.status != "approved":
                raise AppError("dataset must be approved before evaluation", http_status=409)
            dataset = DatasetCreate.model_validate_json(dataset_row.payload_json)
            manifest = execution_manifest(dataset, dataset_row.content_hash)
            row = EvaluationRunRow(
                run_id=new_id("eval_"),
                dataset_id=payload.dataset_id,
                candidate_label=payload.candidate_label,
                manifest_json=json.dumps(manifest),
            )
            db.add(row)
            await db.commit()
            try:
                report = await evaluate(dataset)
                row.status = "completed"
            except Exception as exc:
                report = {"error": type(exc).__name__}
                row.status = "failed"
            row.report_json = json.dumps(report, ensure_ascii=False)
            await db.commit()
            return run_view(row)

    async def run_detail(self, run_id):
        require_operator(self.scope)
        async with self.factory() as db:
            row = await db.get(EvaluationRunRow, run_id)
            if row is None:
                raise AppError("evaluation run not found", http_status=404)
            return run_view(row)

    async def compare(self, baseline, candidate):
        require_operator(self.scope)
        async with self.factory() as db:
            left = await db.get(EvaluationRunRow, baseline)
            right = await db.get(EvaluationRunRow, candidate)
            if left is None or right is None:
                raise AppError("evaluation run not found", http_status=404)
            if (
                left.dataset_id != right.dataset_id
                or left.status != "completed"
                or right.status != "completed"
            ):
                raise AppError(
                    "comparison requires completed runs on the same dataset version",
                    http_status=409,
                )
            if (
                json.loads(left.manifest_json)["adapter_version"]
                != json.loads(right.manifest_json)["adapter_version"]
            ):
                raise AppError("comparison requires the same evaluation protocol", http_status=409)
            old = {row["case_id"]: row for row in json.loads(left.report_json)["cases"]}
            new = json.loads(right.report_json)["cases"]
            improved = [
                row["case_id"] for row in new if row["passed"] and not old[row["case_id"]]["passed"]
            ]
            regressed = [
                row["case_id"] for row in new if not row["passed"] and old[row["case_id"]]["passed"]
            ]
            return {
                "baseline": baseline,
                "candidate": candidate,
                "improved": improved,
                "regressed": regressed,
                "unchanged": len(new) - len(improved) - len(regressed),
                "pass_rate_delta": json.loads(right.report_json)["summary"]["pass_rate"]
                - json.loads(left.report_json)["summary"]["pass_rate"],
            }
