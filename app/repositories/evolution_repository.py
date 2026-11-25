"""State machine and deterministic gate for self-evolution proposals.

Lifecycle: collect -> attribute -> regress -> gate -> review -> promoted.
A failed hard gate moves to the terminal 'blocked' state; a human rejection
moves to 'rejected'. Human review is mandatory and cannot be bypassed.
"""

import hashlib
import json

from sqlalchemy import select

from app.config.settings import Settings, get_settings
from app.core.data_scope import require_data_scope
from app.core.exceptions import AppError
from app.core.ids import new_id
from app.core.time import utcnow
from app.repositories.models import (
    DatasetVersionRow,
    EvaluationRunRow,
    ImprovementProposalRow,
)
from app.repositories.scope_policy import require_operator
from app.schemas.evolution import (
    CandidateChange,
    FeedbackCaseBundle,
    GateResult,
    IntentRegressionBundle,
)
from app.services.evolution import (
    AttributionEngine,
    FeedbackCollector,
    build_regression_case_from_feedback,
    evaluate_intent_regression,
)


def proposal_view(row: ImprovementProposalRow) -> dict:
    return {
        "proposal_id": row.proposal_id,
        "title": row.title,
        "status": row.status,
        "candidate_label": row.candidate_label,
        "source_dataset_id": row.source_dataset_id,
        "baseline_run_id": row.baseline_run_id,
        "candidate_run_id": row.candidate_run_id,
        "attribution": json.loads(row.attribution_json) if row.attribution_json else None,
        "gate": json.loads(row.gate_json) if row.gate_json else None,
        "review_decision": row.review_decision,
        "reviewer": row.reviewer,
        "review_note": row.review_note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def regression_dataset_view(row: DatasetVersionRow) -> dict:
    return {
        "dataset_id": row.dataset_id,
        "name": row.name,
        "version": row.version,
        "task": row.task,
        "status": row.status,
        "reviewer": row.reviewer,
        "review_note": row.review_note,
        "payload": json.loads(row.payload_json),
    }


class EvolutionRepository:
    def __init__(self, factory, settings: Settings | None = None) -> None:
        self.factory = factory
        self.settings = settings or get_settings()
        self.scope = require_data_scope()
        require_operator(self.scope)

    # ---- frozen intent regression dataset -------------------------------

    async def create_intent_regression_dataset(self, bundle: IntentRegressionBundle) -> dict:
        require_operator(self.scope)
        canonical = json.dumps(
            bundle.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self.factory() as db:
            row = DatasetVersionRow(
                dataset_id=new_id("ds_"),
                name=bundle.name,
                version=bundle.version,
                task="intent_classification",
                split="regression",
                status="draft",
                content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                payload_json=bundle.model_dump_json(),
            )
            db.add(row)
            await db.commit()
            return regression_dataset_view(row)

    async def review_intent_regression_dataset(
        self, dataset_id: str, *, decision: str, note: str
    ) -> dict:
        require_operator(self.scope)
        async with self.factory() as db:
            row = await db.get(DatasetVersionRow, dataset_id)
            if row is None or row.task != "intent_classification":
                raise AppError("intent regression dataset not found", http_status=404)
            if row.status != "draft":
                raise AppError("dataset already reviewed", http_status=409)
            row.status = decision
            row.reviewer = self.scope.actor_user_id
            row.review_note = note
            await db.commit()
            return regression_dataset_view(row)

    # ---- proposal lifecycle ---------------------------------------------

    async def list_proposals(self, limit: int = 50) -> dict:
        require_operator(self.scope)
        async with self.factory() as db:
            rows = await db.scalars(
                select(ImprovementProposalRow).order_by(
                    ImprovementProposalRow.created_at.desc()
                ).limit(limit)
            )
            return {"proposals": [proposal_view(row) for row in rows]}

    async def get_proposal(self, proposal_id: str) -> dict:
        require_operator(self.scope)
        async with self.factory() as db:
            return proposal_view(await self._get(db, proposal_id))

    async def create_proposal(self, *, name: str, version: str) -> dict:
        """collect: gather low-score feedback and persist the case bundle."""
        require_operator(self.scope)
        bundle = await FeedbackCollector(self.factory, self.settings).collect(
            name=name, version=version
        )
        canonical = json.dumps(
            bundle.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self.factory() as db:
            dataset = DatasetVersionRow(
                dataset_id=new_id("ds_"),
                name=bundle.name,
                version=bundle.version,
                task="reception_feedback",
                split="regression",
                status="draft",
                content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                payload_json=bundle.model_dump_json(),
            )
            db.add(dataset)
            await db.flush()
            proposal = ImprovementProposalRow(
                proposal_id=new_id("prop_"),
                title=f"{bundle.name}:{bundle.version}",
                status="collect",
                candidate_label="",
                source_dataset_id=dataset.dataset_id,
            )
            db.add(proposal)
            await db.commit()
            return proposal_view(proposal)

    async def attribute_proposal(self, proposal_id: str) -> dict:
        """attribute: deterministic lexical failure attribution."""
        require_operator(self.scope)
        async with self.factory() as db:
            proposal = await self._get(db, proposal_id)
            self._require_status(proposal, "collect")
            source = await db.get(DatasetVersionRow, proposal.source_dataset_id)
            bundle = FeedbackCaseBundle.model_validate_json(source.payload_json)
            report = AttributionEngine().attribute(bundle)
            proposal.attribution_json = report.model_dump_json()
            proposal.status = "attribute"
            proposal.updated_at = utcnow()
            await db.commit()
            return proposal_view(proposal)

    async def run_regression(
        self,
        proposal_id: str,
        *,
        regression_dataset_id: str,
        candidate: CandidateChange,
    ) -> dict:
        """regress: run baseline and candidate rules over the frozen set."""
        require_operator(self.scope)
        async with self.factory() as db:
            proposal = await self._get(db, proposal_id)
            self._require_status(proposal, "attribute")
            reg_dataset = await db.get(DatasetVersionRow, regression_dataset_id)
            if reg_dataset is None or reg_dataset.task != "intent_classification":
                raise AppError("intent regression dataset not found", http_status=404)
            if reg_dataset.status != "approved":
                raise AppError("regression dataset must be approved", http_status=409)
            bundle = IntentRegressionBundle.model_validate_json(reg_dataset.payload_json)
            baseline_report = evaluate_intent_regression(bundle)
            candidate_report = evaluate_intent_regression(bundle, candidate=candidate)
            baseline_run = EvaluationRunRow(
                run_id=new_id("eval_"),
                dataset_id=reg_dataset.dataset_id,
                candidate_label="baseline",
                status="completed",
                manifest_json=json.dumps(self._manifest(reg_dataset, "baseline")),
                report_json=json.dumps(baseline_report, ensure_ascii=False),
            )
            candidate_run = EvaluationRunRow(
                run_id=new_id("eval_"),
                dataset_id=reg_dataset.dataset_id,
                candidate_label=candidate.keyword,
                status="completed",
                manifest_json=json.dumps(
                    self._manifest(
                        reg_dataset,
                        candidate.keyword,
                        candidate_intent=candidate.intent.value,
                    )
                ),
                report_json=json.dumps(candidate_report, ensure_ascii=False),
            )
            db.add_all([baseline_run, candidate_run])
            await db.flush()
            proposal.baseline_run_id = baseline_run.run_id
            proposal.candidate_run_id = candidate_run.run_id
            proposal.candidate_label = candidate.keyword
            proposal.status = "regress"
            proposal.updated_at = utcnow()
            await db.commit()
            return proposal_view(proposal)

    async def run_gate(self, proposal_id: str) -> dict:
        """gate: deterministic fail-closed comparison."""
        require_operator(self.scope)
        async with self.factory() as db:
            proposal = await self._get(db, proposal_id)
            self._require_status(proposal, "regress")
            baseline = await db.get(EvaluationRunRow, proposal.baseline_run_id)
            candidate = await db.get(EvaluationRunRow, proposal.candidate_run_id)
            base_report = json.loads(baseline.report_json)
            cand_report = json.loads(candidate.report_json)
            base_cases = {case["case_id"]: case for case in base_report["cases"]}
            improved: list[str] = []
            regressed: list[str] = []
            for case in cand_report["cases"]:
                old = base_cases[case["case_id"]]
                if case["passed"] and not old["passed"]:
                    improved.append(case["case_id"])
                if not case["passed"] and old["passed"]:
                    regressed.append(case["case_id"])
            base_rate = base_report["summary"]["pass_rate"]
            cand_rate = cand_report["summary"]["pass_rate"]
            reasons: list[str] = []
            if baseline.status != "completed" or candidate.status != "completed":
                reasons.append("both runs must be completed")
            if cand_rate < base_rate:
                reasons.append("candidate pass rate must not decrease")
            if regressed:
                reasons.append("candidate regressed existing passing cases")
            result = GateResult(
                passed=not reasons,
                baseline_pass_rate=base_rate,
                candidate_pass_rate=cand_rate,
                pass_rate_delta=cand_rate - base_rate,
                improved=improved,
                regressed=regressed,
                reasons=reasons,
            )
            proposal.gate_json = result.model_dump_json()
            proposal.status = "gate" if result.passed else "blocked"
            proposal.updated_at = utcnow()
            await db.commit()
            return proposal_view(proposal)

    async def review_proposal(
        self, proposal_id: str, *, decision: str, note: str
    ) -> dict:
        """review: mandatory human decision."""
        require_operator(self.scope)
        async with self.factory() as db:
            proposal = await self._get(db, proposal_id)
            self._require_status(proposal, "gate")
            if decision not in ("approved", "rejected"):
                raise AppError("decision must be approved or rejected", http_status=422)
            proposal.review_decision = decision
            proposal.reviewer = self.scope.actor_user_id
            proposal.review_note = note
            proposal.status = "review" if decision == "approved" else "rejected"
            proposal.updated_at = utcnow()
            await db.commit()
            return proposal_view(proposal)

    async def promote_proposal(self, proposal_id: str) -> dict:
        """promote: only an explicitly approved proposal can be promoted."""
        require_operator(self.scope)
        async with self.factory() as db:
            proposal = await self._get(db, proposal_id)
            self._require_status(proposal, "review")
            if proposal.review_decision != "approved":
                raise AppError("only an approved proposal can be promoted", http_status=409)
            proposal.status = "promoted"
            proposal.updated_at = utcnow()
            await db.commit()
            return proposal_view(proposal)

    async def consolidate_to_regression(
        self, proposal_id: str, *, new_version: str
    ) -> dict:
        """Build a new regression version that absorbs confirmed feedback.

        The machine deterministically extracts one regression case per
        feedback case and emits a *draft* dataset version; a human must
        approve that version before it becomes the new frozen set.
        """
        require_operator(self.scope)
        async with self.factory() as db:
            proposal = await self._get(db, proposal_id)
            if proposal.status != "promoted":
                raise AppError(
                    "only a promoted proposal can be consolidated", http_status=409
                )
            source = await db.get(DatasetVersionRow, proposal.source_dataset_id)
            feedback_bundle = FeedbackCaseBundle.model_validate_json(source.payload_json)
            candidate_run = await db.get(EvaluationRunRow, proposal.candidate_run_id)
            manifest = json.loads(candidate_run.manifest_json)
            if not manifest.get("candidate_intent"):
                raise AppError(
                    "candidate run is missing candidate intent metadata",
                    http_status=409,
                )
            candidate = CandidateChange(
                type="intent_keyword",
                intent=manifest["candidate_intent"],
                keyword=manifest["candidate_label"],
            )
            reg_dataset = await db.get(DatasetVersionRow, manifest["dataset_id"])
            regression_bundle = IntentRegressionBundle.model_validate_json(
                reg_dataset.payload_json
            )
            new_cases = list(regression_bundle.cases)
            existing_queries = {case.query for case in new_cases}
            added_case_ids: list[str] = []
            for feedback_case in feedback_bundle.cases:
                case_id = "sed_" + new_id()[:12]
                regression_case = build_regression_case_from_feedback(
                    feedback_case, candidate=candidate, case_id=case_id
                )
                if regression_case.query in existing_queries:
                    continue
                new_cases.append(regression_case)
                existing_queries.add(regression_case.query)
                added_case_ids.append(case_id)
            if not added_case_ids:
                raise AppError(
                    "no new regression case to consolidate", http_status=409
                )
            new_bundle = IntentRegressionBundle(
                name=regression_bundle.name,
                version=new_version,
                cases=new_cases,
            )
            canonical = json.dumps(
                new_bundle.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            new_row = DatasetVersionRow(
                dataset_id=new_id("ds_"),
                name=new_bundle.name,
                version=new_bundle.version,
                task="intent_classification",
                split="regression",
                status="draft",
                content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                payload_json=new_bundle.model_dump_json(),
            )
            db.add(new_row)
            await db.commit()
            return regression_dataset_view(new_row)

    # ---- helpers ---------------------------------------------------------

    async def _get(self, db, proposal_id: str) -> ImprovementProposalRow:
        row = await db.get(ImprovementProposalRow, proposal_id)
        if row is None:
            raise AppError("improvement proposal not found", http_status=404)
        return row

    @staticmethod
    def _require_status(row: ImprovementProposalRow, expected: str) -> None:
        if row.status != expected:
            raise AppError(
                f"proposal must be in '{expected}' status, current '{row.status}'",
                http_status=409,
            )

    @staticmethod
    def _manifest(
        dataset: DatasetVersionRow,
        label: str,
        *,
        candidate_intent: str | None = None,
    ) -> dict:
        return {
            "adapter": "intent_classification",
            "adapter_version": "intent-rules-v1",
            "dataset_id": dataset.dataset_id,
            "candidate_label": label,
            "candidate_intent": candidate_intent,
            "external_api_calls": 0,
            "mode": "deterministic-rules",
        }
