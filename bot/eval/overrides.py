"""``--set key=value`` — the counterfactual mechanism (W7-12, D2).

The point of this file is one sentence: *a configuration question becomes an
experiment instead of an argument.* "Would a wider correlation window have
over-merged?" is answerable in nine seconds against forty real incidents, and
the answer is a number rather than an opinion.

Overrides apply **to the replay only**. Nothing is written back, no environment
variable is set, and the committed configuration is untouched -- which is what
lets the E-1 model decision be settled by evidence without anyone having to
merge the candidate first.

Two namespaces:

    roles.<role>.<field>        model, provider, rates, timeouts
    correlation.<field>         window_s, merge_threshold, storm_threshold

Anything else is refused by name. A silently-ignored ``--set`` is the worst
possible outcome here: you run the counterfactual, read the unchanged number,
and conclude the parameter does not matter.
"""

from __future__ import annotations

from dataclasses import replace

from incidentpilot.config.models_config import ModelsConfig, ProviderSpec, RoleSpec

CORRELATION_FIELDS = {"window_s", "correlation_window_s", "merge_threshold", "storm_threshold"}
ROLE_FIELDS = {
    "provider",
    "model",
    "max_output_tokens",
    "temperature",
    "timeout_s",
    "input_usd_per_mtok",
    "output_usd_per_mtok",
}


class UnknownOverride(ValueError):
    """The key names nothing this harness can change."""


def parse_overrides(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise UnknownOverride(f"--set expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def apply_to_models(config: ModelsConfig, overrides: dict[str, str]) -> ModelsConfig:
    """Return a config with the ``roles.*`` overrides applied.

    Rates travel with the model on purpose. Overriding
    ``roles.synthesize.model`` alone changes what the report *says* was used
    without changing what it cost, and a cost comparison between two models
    priced identically is not a comparison. The E-1 experiment therefore reads:

        --set roles.synthesize.model=<candidate> \\
        --set roles.synthesize.input_usd_per_mtok=<rate> \\
        --set roles.synthesize.output_usd_per_mtok=<rate>

    -- or, better, add the candidate to ``models.yaml`` with its real prices and
    override only the name.
    """
    roles = dict(config.roles)
    touched = False

    for key, raw in overrides.items():
        parts = key.split(".")
        if parts[0] != "roles":
            continue
        if len(parts) != 3:
            raise UnknownOverride(f"expected roles.<role>.<field>, got {key!r}")
        _, role_name, field = parts
        if field not in ROLE_FIELDS:
            raise UnknownOverride(
                f"role field {field!r} is not overridable (have {sorted(ROLE_FIELDS)})"
            )
        spec = roles.get(role_name)
        if spec is None:
            raise UnknownOverride(f"no such role: {role_name!r} (have {sorted(roles)})")
        roles[role_name] = _with_field(spec, field, raw)
        touched = True

    if not touched:
        return config
    return ModelsConfig(
        version=config.version,
        roles=roles,
        providers=config.providers,
        budgets=config.budgets,
    )


def _with_field(spec: RoleSpec, field: str, raw: str) -> RoleSpec:
    primary = _apply_provider_field(spec.primary, field, raw)
    return replace(spec, primary=primary)


def _apply_provider_field(spec: ProviderSpec, field: str, raw: str) -> ProviderSpec:
    """Spelled out per field rather than splatted.

    `replace(spec, **{field: value})` type-checks as "some string into some
    field", which is exactly the mistake worth catching here -- `--set
    roles.synthesize.temperature=warm` should fail at parse time with the field
    named, not produce a config whose temperature is the string "warm".
    """
    match field:
        case "provider":
            return replace(spec, provider=raw)
        case "model":
            return replace(spec, model=raw)
        case "max_output_tokens":
            return replace(spec, max_output_tokens=int(raw))
        case "temperature":
            return replace(spec, temperature=float(raw))
        case "timeout_s":
            return replace(spec, timeout_s=float(raw))
        case "input_usd_per_mtok":
            return replace(spec, input_usd_per_mtok=float(raw))
        case "output_usd_per_mtok":
            return replace(spec, output_usd_per_mtok=float(raw))
    raise UnknownOverride(f"role field {field!r} is not overridable")


class OverriddenCorrelation:
    """Correlation config with ``--set correlation.*`` applied.

    Correlation takes its config as a parameter (INV-01) rather than importing
    ``settings``, which is exactly why this class is nine lines instead of a
    monkeypatch.
    """

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        values = overrides or {}
        self._window = 300
        self._threshold = 0.62
        self._storm = 5
        for key, raw in values.items():
            parts = key.split(".")
            if parts[0] != "correlation":
                continue
            if len(parts) != 2 or parts[1] not in CORRELATION_FIELDS:
                raise UnknownOverride(
                    f"expected correlation.<{'|'.join(sorted(CORRELATION_FIELDS))}>, got {key!r}"
                )
            field = parts[1]
            if field in {"window_s", "correlation_window_s"}:
                self._window = int(raw)
            elif field == "merge_threshold":
                self._threshold = float(raw)
            else:
                self._storm = int(raw)

    @property
    def correlation_window_s(self) -> int:
        return self._window

    @property
    def merge_threshold(self) -> float:
        return self._threshold

    @property
    def storm_threshold(self) -> int:
        return self._storm


def validate(overrides: dict[str, str]) -> None:
    """Refuse anything neither namespace understands, by name."""
    for key in overrides:
        head = key.split(".")[0]
        if head not in {"roles", "correlation"}:
            raise UnknownOverride(
                f"--set {key}: only roles.* and correlation.* are overridable. "
                "A --set that is silently ignored is worse than one that errors: "
                "you read the unchanged number and conclude the parameter does not matter."
            )
