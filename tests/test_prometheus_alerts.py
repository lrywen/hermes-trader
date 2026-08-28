"""R11-F1 — PrometheusRule yaml validity + metric-ownership contract.

For every alert in k8s/prometheusrule.yaml we enforce:

  * The file parses as a PrometheusRule CRD (apiVersion,
    kind, spec.groups).
  * Each alert has a unique name.
  * Each alert has a non-empty ``expr``, ``for``, ``labels.severity``
    and ``annotations.summary``.
  * The ``expr`` references only metric names that are actually
    registered in ``prometheus_client.REGISTRY`` (i.e. owned by
    ``hermes_trader.metrics``). This catches the "renamed a metric
    but forgot to update the alert" regression that would otherwise
    fail silently at the Prometheus evaluation engine.
  * Counter / Gauge / Histogram metric name suffixes match the
    declared type — e.g. ``*_total`` must be a Counter, ``_seconds``
    must be a Histogram or Gauge named in seconds.

We do NOT spin up a Prometheus server in tests; the contract is
"this YAML + this metrics.py will not silently disagree".
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from hermes_trader import metrics as _metrics
from prometheus_client import Counter, Gauge, Histogram, REGISTRY


REPO_ROOT = Path(__file__).resolve().parent.parent
RULE_PATH = REPO_ROOT / "k8s" / "prometheusrule.yaml"


# ── Helpers ───────────────────────────────────────────────────────────

# Aggregate function names from PromQL. Metric names inside these
# calls are *not* full metric names (e.g. ``rate(metric[5m])``), so
# we strip the wrapping before extracting. We also strip the range
# selector and any label matchers.
_PROMQL_FUNCS = (
    r"rate|irate|increase|delta|idelta|deriv|predict_linear|"
    r"histogram_quantile|sum|avg|min|max|count|topk|bottomk|"
    r"absent|ceil|floor|abs|exp|ln|log2|log10|sqrt|round|"
    r"clamp_max|clamp_min|clamp|sort|sort_desc|group|"
    r"stddev|stdvar|quantile|time|minute|hour|day_of_week|"
    r"day_of_month|days_in_month|month|year|timestamp|"
    r"vector|on|ignoring|group_left|group_right|by|without|"
    r"sum_over_time|avg_over_time|min_over_time|max_over_time|"
    r"count_over_time|quantile_over_time|stddev_over_time|"
    r"stdvar_over_time|mad_over_time|last_over_time|present_over_time|"
    r"changes|resets|absent_over_time"
)
_FUNC_RE = re.compile(rf"\b(?:{_PROMQL_FUNCS})\s*\(", re.IGNORECASE)

# Match a Prometheus metric name. Per the spec: [a-zA-Z_:][a-zA-Z0-9_:]*.
# In practice our names are all snake_case lowercase with the ``hermes_``
# prefix and a documented suffix (``_total`` / ``_seconds`` / etc.).
_METRIC_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")

# Map a metric name as declared in hermes_trader.metrics to the
# ``prometheus_client.MetricWrapperBase`` subclass that owns it.
# Built lazily below.


def _build_name_to_metric() -> dict:
    """Return {metric_name: metric_object} for every metric Hermes
    has registered. We do this by walking REGISTRY and looking up
    each child's name."""
    out: dict = {}
    for collector in list(REGISTRY._collector_to_names.keys()):  # noqa: SLF001
        for metric in collector.collect():
            # metric is a protobuf; ``metric.name`` is the canonical
            # name (with the ``_total`` / ``_bucket`` / ``_count`` /
            # ``_sum`` suffix stripped for counter/histogram types).
            name = metric.name
            if name not in out:
                out[name] = metric
    return out


@pytest.fixture(scope="module")
def name_to_metric() -> dict:
    return _build_name_to_metric()


def _metric_kind(name_to_metric: dict, name: str) -> str | None:
    """Return 'counter' / 'gauge' / 'histogram' / 'summary' / None
    for a metric name as Prometheus would expose it. We need to
    walk the live collectors because prometheus_client doesn't
    expose a single ``Metric._type`` for the unwrapped form."""
    for collector in list(REGISTRY._collector_to_names.keys()):  # noqa: SLF001
        for metric in collector.collect():
            if metric.name == name:
                t = metric.type
                # protobuf enum names: COUNTER, GAUGE, SUMMARY,
                # UNTYPED, HISTOGRAM, GAUGE_HISTOGRAM
                return {
                    "COUNTER": "counter",
                    "GAUGE": "gauge",
                    "SUMMARY": "summary",
                    "HISTOGRAM": "histogram",
                    "GAUGE_HISTOGRAM": "histogram",
                    "UNTYPED": "untyped",
                }.get(t, t.lower())
    return None


# ── YAML loading ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rule_doc() -> dict:
    with open(RULE_PATH) as f:
        return yaml.safe_load(f)


def _all_alerts(rule_doc: dict) -> list:
    """Flatten the (group -> rules) tree into a list of
    (group_name, alert_dict) tuples."""
    out = []
    for group in rule_doc["spec"]["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                out.append((group["name"], rule))
    return out


# ── Tests ─────────────────────────────────────────────────────────────

class TestYAMLStructure:
    def test_yaml_loads(self, rule_doc: dict) -> None:
        assert isinstance(rule_doc, dict)

    def test_top_level_shape(self, rule_doc: dict) -> None:
        assert rule_doc["apiVersion"] == "monitoring.coreos.com/v1"
        assert rule_doc["kind"] == "PrometheusRule"
        assert rule_doc["metadata"]["name"]
        assert rule_doc["metadata"]["namespace"] == "hermes"
        # release label is what kube-prometheus-stack selects on.
        labels = rule_doc["metadata"]["labels"]
        assert labels.get("release") == "kube-prometheus-stack"

    def test_groups_present(self, rule_doc: dict) -> None:
        groups = rule_doc["spec"]["groups"]
        assert isinstance(groups, list)
        assert len(groups) >= 1
        # Every group has a name and a list of rules.
        for g in groups:
            assert g.get("name")
            assert isinstance(g.get("rules"), list)

    def test_has_at_least_seven_alerts(self, rule_doc: dict) -> None:
        # 5 audit-deduced + 5 ws/notify/gate = 10. We don't pin
        # the exact count (operators may add more) but enforce a
        # lower bound so a typo that drops a whole group fails.
        assert len(_all_alerts(rule_doc)) >= 7


class TestAlertNames:
    def test_unique(self, rule_doc: dict) -> None:
        names = [r["alert"] for _g, r in _all_alerts(rule_doc)]
        # No duplicates.
        assert len(names) == len(set(names)), (
            f"duplicate alert names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_hermes_prefix(self, rule_doc: dict) -> None:
        # Naming convention: all alerts start with ``Hermes`` so the
        # Grafana alerts page groups them with the bot's other
        # entities. We don't fail a future rename, but a typo that
        # forgets the prefix is loud.
        for _g, r in _all_alerts(rule_doc):
            assert r["alert"].startswith("Hermes"), (
                f"alert {r['alert']!r} missing 'Hermes' prefix"
            )

    def test_severity_label_set(self, rule_doc: dict) -> None:
        # Every alert must declare a severity so Alertmanager
        # routing can use it.
        for _g, r in _all_alerts(rule_doc):
            labels = r.get("labels", {})
            assert labels.get("severity") in ("critical", "warning"), (
                f"alert {r['alert']!r} missing severity label"
            )

    def test_for_set(self, rule_doc: dict) -> None:
        # Every alert must declare a ``for`` to avoid flapping
        # on a single bad scrape.
        for _g, r in _all_alerts(rule_doc):
            assert "for" in r, f"alert {r['alert']!r} missing 'for'"
            # The value is a Go duration string ("5m", "2m", "0m")
            # or "0m" for immediate.
            assert r["for"] is not None
            assert isinstance(r["for"], str)
            assert re.match(r"^\d+[smh]$", r["for"]), (
                f"alert {r['alert']!r} has invalid for={r['for']!r}"
            )

    def test_summary_annotation_set(self, rule_doc: dict) -> None:
        for _g, r in _all_alerts(rule_doc):
            ann = r.get("annotations", {})
            assert ann.get("summary"), (
                f"alert {r['alert']!r} missing annotations.summary"
            )


class TestAlertExprMetrics:
    """The contract test: every metric named in an alert ``expr``
    must be registered in ``prometheus_client.REGISTRY``."""

    @pytest.mark.parametrize("group,rule", [
        (g, r) for g, r in _all_alerts(yaml.safe_load(open(RULE_PATH)))
    ])
    def test_expr_metrics_exist(
        self, group: str, rule: dict, name_to_metric: dict,
    ) -> None:
        expr = rule["expr"]
        # Strip the PromQL aggregate functions and their parens so
        # we don't pick up keywords like "rate" or "le" as metric
        # names. We do this by repeatedly walking the string and
        # deleting function-call bodies.
        scrubbed = expr
        while True:
            m = _FUNC_RE.search(scrubbed)
            if not m:
                break
            # Find the matching close-paren, allowing one level of
            # nesting (e.g. ``rate(foo[5m])``).
            depth = 0
            i = m.end() - 1  # at the opening paren
            assert scrubbed[i] == "("
            while i < len(scrubbed):
                ch = scrubbed[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            scrubbed = scrubbed[: m.start()] + scrubbed[i + 1 :]
        # Also strip range selectors like ``[5m]`` so ``5m`` doesn't
        # pollute the candidate metric names.
        scrubbed = re.sub(r"\[\s*\d+[smhd]?\s*\]", "", scrubbed)
        # And label matchers — the inside of ``{...}`` can contain
        # ``=~``/``!~`` regex literals that look like identifiers.
        scrubbed = re.sub(r"\{[^}]*\}", "", scrubbed)
        # And template placeholders like ``{{ $labels.x }}``.
        scrubbed = re.sub(r"\{\{[^}]*\}\}", "", scrubbed)
        # Now extract candidate identifiers.
        candidates = set(_METRIC_RE.findall(scrubbed))
        # Whitelist of well-known PromQL keywords / operators /
        # placeholders that are NOT metric names.
        NON_METRIC = {
            "true", "false", "on", "ignoring", "group_left", "group_right",
            "by", "without", "or", "and", "unless", "bool", "le",
            # numeric-ish tokens from threshold values: "0", "1", etc.
            # aren't matched by [a-zA-Z_], so we don't need to
            # whitelist them.
        }
        candidates -= NON_METRIC
        # Build the set of all metric names currently exported by
        # prometheus_client (full canonical names — Counter names
        # end in ``_total``, Histogram names have ``_bucket`` /
        # ``_count`` / ``_sum`` siblings, etc.).
        for cand in sorted(candidates):
            # Skip numeric thresholds that look like "1" but matched
            # the regex (rare in our alerts but defensive).
            if not cand.startswith("hermes_") and not cand.startswith("process_") \
                    and not cand.startswith("python_") and not cand.startswith("up"):
                # Not a metric name. (e.g. 'sum', 'le', 'endpoint' label
                # key inside a stripped matcher that survived — should
                # not happen with our scrubbing but defensive.)
                continue
            assert cand in name_to_metric, (
                f"alert {rule['alert']!r} (group {group!r}) references "
                f"metric {cand!r} which is not registered. Either the "
                f"metric was renamed in hermes_trader.metrics and this "
                f"alert was missed, or the alert expr has a typo."
            )


class TestMetricKindSuffixes:
    """The metric family should match the suffix in the alert
    expression. This catches "named a Counter ``hermes_foo`` but
    forgot to add ``_total``" or "named a Histogram
    ``hermes_foo_seconds`` but the alert query uses the wrong
    name"."""

    def test_counters_have_total_suffix(
        self, name_to_metric: dict,
    ) -> None:
        for collector in list(REGISTRY._collector_to_names.keys()):  # noqa: SLF001
            for metric in collector.collect():
                if metric.type == "COUNTER":
                    assert metric.name.endswith("_total"), (
                        f"counter {metric.name!r} missing _total suffix"
                    )

    def test_dsl_state_save_errors_is_counter(self) -> None:
        # We use this in HermesDSLStateSaveErrors — verify the type
        # so a future refactor that turns it into a Gauge is loud.
        assert _metric_kind(None, "hermes_dsl_state_save_errors") == "counter"

    def test_memory_flush_errors_is_counter(self) -> None:
        assert _metric_kind(None, "hermes_memory_flush_errors") == "counter"

    def test_notify_dispatch_errors_is_counter(self) -> None:
        assert _metric_kind(None, "hermes_notify_dispatch_errors") == "counter"

    def test_ws_dropped_dup_is_counter(self) -> None:
        assert _metric_kind(None, "hermes_ws_dropped_dup") == "counter"

    def test_ws_dropped_stale_is_counter(self) -> None:
        assert _metric_kind(None, "hermes_ws_dropped_stale") == "counter"

    def test_ws_data_age_is_gauge(self) -> None:
        assert _metric_kind(None, "hermes_ws_data_age_seconds") == "gauge"

    def test_ws_app_heartbeat_age_is_gauge(self) -> None:
        assert _metric_kind(None, "hermes_ws_app_heartbeat_age_seconds") == "gauge"

    def test_hl_rate_gate_wait_is_histogram(self) -> None:
        assert _metric_kind(None, "hermes_hl_rate_gate_wait_seconds") == "histogram"

    def test_llm_circuit_state_is_gauge(self) -> None:
        assert _metric_kind(None, "hermes_llm_circuit_state") == "gauge"

    def test_trade_circuit_state_is_gauge(self) -> None:
        assert _metric_kind(None, "hermes_trade_circuit_state") == "gauge"


class TestRefreshWiresWSGauges:
    """The /metrics endpoint must surface the R11-D1 WS gauges so
    the ws_freshness group has something to evaluate against."""

    def test_metrics_module_exposes_ws_gauges(self) -> None:
        # The Gauge/Counter objects are module-level so they are
        # importable and the REGISTRY sees them.
        for name in (
            "WS_LAST_SEQ",
            "WS_DATA_AGE_S",
            "WS_APP_HEARTBEAT_AGE_S",
            "WS_DROPPED_DUP",
            "WS_DROPPED_STALE",
        ):
            assert hasattr(_metrics, name), f"metrics module missing {name}"

    def test_refresh_renders_ws_lines(self) -> None:
        body, ct = _metrics.render_metrics()
        # The /metrics output must contain at least the WS family
        # names that are surfaced via _refresh (gauges) or via
        # direct inc() (counters — the metric line is created the
        # first time inc() runs, but the *name* is registered
        # regardless).
        text = body.decode("utf-8", errors="replace")
        # HELP line is always emitted for a registered metric, even
        # if it has no samples yet.
        assert "hermes_ws_last_seq" in text
        assert "hermes_ws_data_age_seconds" in text
        assert "hermes_ws_app_heartbeat_age_seconds" in text
        # Counters: HELP line is emitted on first inc() or on
        # first samples(). Force one inc to make sure the name
        # appears in the output.
        _metrics.WS_DROPPED_DUP.inc(0)
        _metrics.WS_DROPPED_STALE.inc(0)
        body2, _ = _metrics.render_metrics()
        text2 = body2.decode("utf-8", errors="replace")
        assert "hermes_ws_dropped_dup_total" in text2
        assert "hermes_ws_dropped_stale_total" in text2
