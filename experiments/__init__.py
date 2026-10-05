"""Transfer-experiment pilots (memory.md section 34 key experiment).

Modules:
    key_experiment    Conditions A/B/C/D over a pilot transfer subset.
    semantic_compare  Code-only vs semantic+code retrieval, same subset.

Stdlib + repo imports only; live calls go through benchmarks/runner.py.
"""

from . import key_experiment, semantic_compare

__all__ = ["key_experiment", "semantic_compare"]
