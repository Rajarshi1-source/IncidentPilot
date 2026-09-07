"""The replay harness and the eval gate (W7, D2).

Deliberately *outside* ``src/incidentpilot``. The harness imports the
application; the application must never import the harness. Keeping it in a
sibling package makes that a structural fact rather than a convention -- there
is no path by which a production process can end up running a fake adapter or a
judge prompt, because the package is not installed into the wheel
(``tool.hatch.build.targets.wheel`` ships ``src/incidentpilot`` only).

Run it with ``python -m eval.run`` from ``bot/``.
"""
