"""Optional budgets for detailed assignment generation and timed planning."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class SearchPolicy:
    mode: str | None = None
    time_seconds: float | None = None
    top_k_pairs_per_cargo: int | None = None
    max_candidates_per_state: int | None = None
    max_detailed_checks_per_iteration: int | None = None
    recover_undelivered: bool = False
    sea_fleet_allocation: bool = False

    def __post_init__(self):
        if type(self.sea_fleet_allocation) is not bool:
            raise ValueError("sea_fleet_allocation must be boolean")
        if type(self.recover_undelivered) is not bool:
            raise ValueError("recover_undelivered must be boolean")
        if self.mode not in (None, 'FAST', 'DEMO', 'STANDARD', 'RESEARCH'):
            raise ValueError('Unknown planning mode')
        if self.time_seconds is not None and (type(self.time_seconds) not in (int, float)
                or not math.isfinite(self.time_seconds) or self.time_seconds <= 0):
            raise ValueError('time_seconds must be finite and positive')
        for name in ('top_k_pairs_per_cargo', 'max_candidates_per_state', 'max_detailed_checks_per_iteration'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f'{name} must be a positive integer')

    @property
    def budget_seconds(self):
        if self.time_seconds is not None:
            return self.time_seconds
        return {'FAST': 5, 'DEMO': 30, 'STANDARD': 120}.get(self.mode)


class TimeLimitReached(Exception):
    """Internal cooperative stop signal for the route and schedule searches."""
