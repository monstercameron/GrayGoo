"""External hidden-test evaluator (plan.md sections 36-37, Domain D).

This package is the single mandatory promotion gate: promotion may not
proceed without a ``pass`` verdict from :func:`service.evaluate`, served
over the stdio/JSON subprocess protocol in :mod:`protocol`. See README.md
in this directory for the gate contract. Stdlib only.
"""

from . import protocol, service, transforms  # noqa: F401
