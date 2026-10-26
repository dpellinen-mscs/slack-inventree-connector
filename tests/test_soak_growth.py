"""The soak's growth judge (`tests/soak/soak.py`): a leak keeps growing, a pool levels off."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent / "soak" / "soak.py"
spec = importlib.util.spec_from_file_location("soak", SCRIPT)
assert spec is not None
assert spec.loader is not None
soak = importlib.util.module_from_spec(spec)
sys.modules["soak"] = soak
spec.loader.exec_module(soak)


def samples(threads: list[int], files: list[int] | None = None, rss: list[int] | None = None) -> list[Any]:
    files = files or [20] * len(threads)
    rss = rss or [100_000] * len(threads)
    return [soak.Sample(i * 60, t, f, r) for i, (t, f, r) in enumerate(zip(threads, files, rss, strict=True))]


def test_a_count_that_levels_off_after_the_warm_up_passes() -> None:
    assert soak.growth(samples([8, 8, 15, 14, 15, 16, 15, 15, 16, 15]), warm_up=2) == []


def test_a_count_that_keeps_growing_fails() -> None:
    found = soak.growth(samples([8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]), warm_up=2)
    assert found == ["threads kept growing: peak 14 in the first half, 19 in the second"]


def test_open_files_and_memory_are_judged_too() -> None:
    files = [20, 20, 20, 21, 20, 21, 40, 41]
    rss = [100_000, 100_000, 100_000, 101_000, 120_000, 130_000, 140_000, 150_000]
    found = soak.growth(samples([8] * 8, files, rss), warm_up=0)
    assert found == [
        "open_files kept growing: peak 21 in the first half, 41 in the second",
        "memory kept growing: peak 101000 kB in the first half, 150000 kB in the second",
    ]


def test_growth_during_the_warm_up_is_not_counted() -> None:
    assert soak.growth(samples([2, 30, 40, 40, 40, 40, 40, 40]), warm_up=2) == []


def test_too_short_a_run_is_reported_rather_than_passed() -> None:
    assert soak.growth(samples([8, 8, 8, 8]), warm_up=2) == [
        "too few samples after the warm-up to judge growth: run longer"
    ]
