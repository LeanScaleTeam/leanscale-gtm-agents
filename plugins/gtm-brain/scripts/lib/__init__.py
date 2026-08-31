"""
LeanScale GTM Agents — shared core library.

Vendored into every plugin at scripts/lib/. Do not edit the vendored copy;
edit core/lib/ and re-run tools/vendor.py.

Python 3.9+, standard library only. These modules never touch the network:
Claude fetches data through MCP and writes it to raw/*.json; this library
only transforms local files.
"""

__version__ = "1.0.0"

from .config import (  # noqa: F401
    GTM_HOME,
    ConfigError,
    fiscal_period,
    load_profile,
    load_plugin_config,
    plugin_config_path,
    profile_path,
    profile_summary,
    save_profile,
    save_plugin_config,
)
from .manifest import RunManifest, SourceEmptyError  # noqa: F401
from .findings import (  # noqa: F401
    SEVERITIES,
    Finding,
    FindingsDoc,
    Score,
    severity_rank,
)
from .baseline import (  # noqa: F401
    BASELINE_RUN_NOTE,
    apply_deltas,
    diff_scores,
    load_previous_baseline,
    save_baseline,
)
from .render import (  # noqa: F401
    load_manifest,
    render_html,
    render_markdown,
    write_reports,
)
from .crmutil import (  # noqa: F401
    fill_rate,
    percentile,
    parse_dt,
    days_between,
    median,
    normalize_records,
    pct,
    redact_name,
)
