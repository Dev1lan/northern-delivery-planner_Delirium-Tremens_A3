"""Run independent network plans in separate processes."""

from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from itertools import islice
from time import perf_counter

from network.adapter import run_scenario
from network.models import Limits
from network.scheduler import REPOSITION
from network.search_policy import SearchPolicy


@dataclass(frozen=True)
class PlanningJob:
    scenario: dict
    strategy: str = REPOSITION
    limits: Limits | None = None
    tariffs: dict | None = None
    search_policy: SearchPolicy | None = None


@dataclass(frozen=True)
class PlanningRun:
    result: dict
    compute_seconds: float


def _run(job):
    started = perf_counter()
    result = run_scenario(job.scenario, job.strategy, limits=job.limits, tariffs=job.tariffs,
                          search_policy=job.search_policy)
    return PlanningRun(result, perf_counter() - started)


def run_jobs(jobs, *, workers=1):
    """Yield results in input order with a bounded queue of independent jobs.

    Consume the iterator or close it to release its executor. Each job has an
    independent search budget. The caller must leave inputs unchanged during
    iteration and use the standard multiprocessing main guard in scripts.
    """
    if type(workers) is not int or workers < 1:
        raise ValueError('workers must be a positive integer')
    if workers == 1:
        yield from map(_run, jobs)
        return

    jobs = iter(jobs)
    first = list(islice(jobs, 2 * workers))
    if len(first) <= 1:
        yield from map(_run, first)
        return
    with ProcessPoolExecutor(max_workers=min(workers, len(first))) as pool:
        pending = deque(pool.submit(_run, job) for job in first)
        try:
            for job in jobs:
                completed = pending.popleft().result()
                pending.append(pool.submit(_run, job))
                yield completed
            while pending:
                yield pending.popleft().result()
        finally:
            for future in pending:
                future.cancel()
