"""Optional single-strategy advisory for the existing audited planner."""

from network.adapter import audit_result, run_scenario
from network.scheduler import REPOSITION

from .dataset import DEFAULT_LIMITS, DEFAULT_POLICY
from .model import DEFAULT_ARTIFACT, forecast


def plan(raw, *, strategy=REPOSITION, tariffs=None, limits=DEFAULT_LIMITS, policy=DEFAULT_POLICY,
         use_ml=False, allow_uncertain=False, artifact_path=DEFAULT_ARTIFACT):
    """Run one strategy under the supplied budget. ML advice is opt-in."""
    advice = forecast(raw, enabled=use_ml, artifact_path=artifact_path, tariffs=tariffs,
                      limits=limits, policy=policy)
    order = advice["strategy_order"]
    allowed = ("AVAILABLE", "HIGH_UNCERTAINTY") if allow_uncertain else ("AVAILABLE",)
    selected = order[0] if advice["status"] in allowed and order else strategy
    result = run_scenario(raw, selected, tariffs=tariffs, limits=limits, search_policy=policy)
    if audit_result(result)["schedule_audit"]["status"] != "PASSED":
        raise ValueError("Independent schedule audit failed")
    return result, advice
