"""Pure contract-driven M18 quality gate validators."""

from __future__ import annotations

from dataclasses import dataclass

from scidatafusion.contracts.fusion import GoldRecordCandidate
from scidatafusion.contracts.quality import EvidenceRef, QualityIssueCode
from scidatafusion.contracts.scientific import QualityGate, QualityGateKind


@dataclass(frozen=True, slots=True)
class RecordGateFinding:
    gold_record_id: str
    passed: bool
    affected_field_names: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class GateAudit:
    findings: tuple[RecordGateFinding, ...]

    @property
    def passed_record_count(self) -> int:
        return sum(item.passed for item in self.findings)

    @property
    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        return tuple(dict.fromkeys(ref for item in self.findings for ref in item.evidence_refs))


def issue_code_for_gate(kind: QualityGateKind) -> QualityIssueCode:
    """Map one registered gate kind to its structured issue code."""

    return {
        QualityGateKind.REQUIRED_FIELDS: QualityIssueCode.REQUIRED_FIELD_MISSING,
        QualityGateKind.ANY_OF_FIELDS: QualityIssueCode.ANY_OF_FIELDS_MISSING,
        QualityGateKind.FIELD_PROVENANCE: QualityIssueCode.FIELD_PROVENANCE_MISSING,
    }[kind]


def audit_gate(gate: QualityGate, records: tuple[GoldRecordCandidate, ...]) -> GateAudit:
    """Evaluate one contract quality gate against every Gold candidate record."""

    findings: list[RecordGateFinding] = []
    for record in records:
        fields = {item.field_name: item for item in record.fields}
        if gate.kind is QualityGateKind.ANY_OF_FIELDS:
            passed = any(name in fields for name in gate.fields)
            affected = () if passed else gate.fields
        elif gate.kind is QualityGateKind.FIELD_PROVENANCE:
            affected = tuple(
                name for name in gate.fields if name not in fields or not fields[name].evidence_ids
            )
            passed = not affected
        else:
            affected = tuple(name for name in gate.fields if name not in fields)
            passed = not affected
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    f"gate:{gate.gate_id}",
                    record.gold_record_id,
                    record.fused_record_id,
                    *(
                        evidence_id
                        for name in gate.fields
                        if name in fields
                        for evidence_id in fields[name].evidence_ids
                    ),
                )
            )
        )
        findings.append(
            RecordGateFinding(
                gold_record_id=record.gold_record_id,
                passed=passed,
                affected_field_names=affected,
                evidence_refs=evidence_refs,
            )
        )
    return GateAudit(findings=tuple(findings))
