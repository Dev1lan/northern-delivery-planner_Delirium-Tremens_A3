"""Scenario-defined navigation approvals. Class labels have no implicit ordering."""

from dataclasses import dataclass

from scenario_validation import ident

RULES_VERSION = 'explicit-marine-admission-v1'


@dataclass(frozen=True)
class VesselAccess:
    vessel_class: str | None
    data_status: str
    permits: tuple[tuple[str, str, str], ...]


def provenance(row, scenario_status):
    status = row.get('data_status', 'SYNTHETIC')
    if status not in ('SYNTHETIC', 'REAL', 'MIXED'):
        raise ValueError(f'Unknown data status: {status!r}')
    if scenario_status != 'MIXED' and status != scenario_status:
        raise ValueError(f'{status} record requires a matching or MIXED scenario')
    if status != 'SYNTHETIC':
        ident(row.get('source'), 'source')
    return status


def vessel_access(row, scenario_status):
    status = provenance(row, scenario_status)
    vessel_class = row.get('vessel_class')
    if vessel_class is not None:
        ident(vessel_class, 'vessel_class')
    permits = []
    for permit in row.get('navigation_permits', []):
        area = ident(permit.get('area'), 'permit area')
        source = ident(permit.get('source'), 'permit source')
        evidence = permit.get('status')
        if evidence not in ('CONFIRMED', 'SCENARIO_ASSUMPTION', 'UNVERIFIED'):
            raise ValueError(f'Invalid permit status: {evidence!r}')
        permits.append((area, evidence, source))
    return VesselAccess(vessel_class, status, tuple(permits))


def admission_error(scenario, edge, tug, barge):
    pair = (tug.id, barge.id)
    if edge.allowed_pairs and pair not in edge.allowed_pairs:
        return 'PAIR_NOT_ADMITTED'
    for vessel in (tug, barge):
        access = vessel.access
        if edge.allowed_classes and access.vessel_class not in edge.allowed_classes:
            return 'CLASS_NOT_ADMITTED'
        if edge.marine:
            if not access.vessel_class or not edge.area:
                return 'MISSING_MARINE_CLASS_OR_AREA'
            allowed_status = 'SCENARIO_ASSUMPTION' if scenario.data_status == 'SYNTHETIC' else 'CONFIRMED'
            if allowed_status == 'CONFIRMED' and access.data_status == 'SYNTHETIC':
                return 'MISSING_MARINE_PERMIT'
            if not any(area == edge.area and evidence == allowed_status
                       for area, evidence, _ in access.permits):
                return 'MISSING_MARINE_PERMIT'
    if edge.marine and not edge.allowed_pairs:
        return 'MISSING_MARINE_TOW_APPROVAL'
    return None
