"""Plan 3 smoke: _run_hitl exists with the expected signature.

The helper collapses two near-duplicate HITL paths in _worker_loop_body
(generic + login_required). This test pins the public-ish API so future
edits don't accidentally break callers.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_run_hitl_helper_exists():
    from applypilot.apply.launcher import _run_hitl
    assert callable(_run_hitl), "_run_hitl must be a callable"


def test_run_hitl_signature_has_required_params():
    from applypilot.apply.launcher import _run_hitl
    sig = inspect.signature(_run_hitl)
    params = sig.parameters
    # Required positional/kw args. If any of these are renamed, callers in
    # _worker_loop_body must be updated atomically.
    for name in (
        "worker_id", "port", "job", "reason", "instructions", "navigate_url",
        "duration_ms",
    ):
        assert name in params, f"_run_hitl missing required parameter: {name}"


def test_run_hitl_returns_tuple_of_result_and_duration_and_qs():
    """The helper must return the same shape that callers slot into:
        result, duration_ms, screening_qs = _run_hitl(...)
    We can't actually invoke it (would need real Chrome), but we can assert
    the documented return type via __annotations__.
    """
    from applypilot.apply.launcher import _run_hitl
    ann = _run_hitl.__annotations__.get("return")
    assert ann is not None, \
        "_run_hitl must declare a return-type annotation so callers know the shape"
    # Stringify defensively — annotation may be a typing.Tuple or a string forward-ref.
    s = str(ann)
    assert "tuple" in s.lower() or "Tuple" in s, \
        f"_run_hitl return annotation should be a tuple, got: {s}"
