"""Load ``models.yaml`` and resolve a role to a chain (W6-11, INV-07).

Lives in ``config/`` because that is the only directory the INV-07 CI grep
exempts, and because loading the file is the one operation that legitimately
handles model-name strings.

``${VAR:-default}`` expansion is done here rather than by the YAML loader so the
file stays readable as YAML and so a missing variable produces the *stated*
default rather than an empty string -- a provider silently resolving to `""` is
a much worse failure than one that resolves to the fake.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).with_name("models.yaml")

# ${NAME} or ${NAME:-default}
INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ModelConfigError(RuntimeError):
    """The configuration cannot produce a usable role."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One rung of a role's chain: which provider, which model, which limits."""

    provider: str
    model: str
    max_output_tokens: int = 1024
    temperature: float = 0.0
    timeout_s: float = 30.0
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0


@dataclass(frozen=True, slots=True)
class RoleSpec:
    name: str
    primary: ProviderSpec
    secondary: ProviderSpec | None = None
    budget_usd_per_incident: float = 0.0
    offline_only: bool = False

    def chain(self) -> tuple[ProviderSpec, ...]:
        """Primary, then secondary. Layer 3 is the skeleton and needs no vendor."""
        return (self.primary,) if self.secondary is None else (self.primary, self.secondary)


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    version: str
    roles: dict[str, RoleSpec]
    providers: dict[str, dict[str, Any]]
    budgets: dict[str, float]

    def role(self, name: str, *, offline: bool = False) -> RoleSpec:
        """Resolve a role, refusing an offline-only role on the hot path.

        ``judge`` carries ``offline_only: true``. Resolving it in production
        would put a second model between an incident and its postmortem -- and
        the validator is deterministic precisely so that nothing has to. The
        refusal is here rather than in a comment because the eval harness and
        the generator both call this function.
        """
        spec = self.roles.get(name)
        if spec is None:
            raise ModelConfigError(f"no such role: {name!r} (have {sorted(self.roles)})")
        if spec.offline_only and not offline:
            raise ModelConfigError(
                f"role {name!r} is offline_only and must not be resolved on the production path"
            )
        return spec

    def model_names(self) -> set[str]:
        """Every model string this config can produce. Used by the INV-07 test."""
        out: set[str] = set()
        for spec in self.roles.values():
            out.add(spec.primary.model)
            if spec.secondary is not None:
                out.add(spec.secondary.model)
        return out


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1)) or (match.group(2) or "")

        return INTERPOLATION.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _provider_spec(role: dict[str, Any], *, provider: str, model: str) -> ProviderSpec:
    return ProviderSpec(
        provider=provider,
        model=model,
        max_output_tokens=int(role.get("max_output_tokens", 1024)),
        temperature=float(role.get("temperature", 0.0)),
        timeout_s=float(role.get("timeout_s", 30.0)),
        input_usd_per_mtok=float(role.get("input_usd_per_mtok", 0.0)),
        output_usd_per_mtok=float(role.get("output_usd_per_mtok", 0.0)),
    )


def load_models_config(path: Path | str | None = None) -> ModelsConfig:
    raw = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    data = _interpolate(raw)

    roles: dict[str, RoleSpec] = {}
    for name, body in (data.get("roles") or {}).items():
        provider = str(body.get("provider") or "").strip()
        model = str(body.get("model") or "").strip()
        if not provider or not model:
            raise ModelConfigError(f"role {name!r} resolves to an empty provider or model")

        secondary: ProviderSpec | None = None
        fb_provider = str(body.get("fallback_provider") or "").strip()
        fb_model = str(body.get("fallback_model") or "").strip()
        if fb_provider and fb_model:
            secondary = _provider_spec(body, provider=fb_provider, model=fb_model)

        roles[str(name)] = RoleSpec(
            name=str(name),
            primary=_provider_spec(body, provider=provider, model=model),
            secondary=secondary,
            budget_usd_per_incident=float(body.get("budget_usd_per_incident", 0.0)),
            offline_only=bool(body.get("offline_only", False)),
        )

    if not roles:
        raise ModelConfigError("models.yaml defines no roles")

    return ModelsConfig(
        version=str(data.get("version", "0.0.0")),
        roles=roles,
        providers={str(k): dict(v or {}) for k, v in (data.get("providers") or {}).items()},
        budgets={str(k): float(v) for k, v in (data.get("budgets") or {}).items()},
    )
