"""12-factor configuration. Import ``settings``; never read ``os.environ``.

Settings are injected into constructors rather than reached for globally, which
is why the tests need no monkeypatching -- and no-monkeypatching is itself a
signal about the design.

Every secret is ``SecretStr | None`` (conflict C-01). Rev 2 declares the Slack
signing secret required, but the repository's most valuable property is that a
stranger can clone it and run the full demo with no credentials. A secret that
is required at *import* time makes ``docker compose up`` fail on a clean clone.
The requirement is real, so it moves to *startup* time, where it can tell the
difference between dev and production -- see ``assert_invariants``.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IP_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "dev"

    # --- infrastructure -------------------------------------------------
    database_url: SecretStr = SecretStr("postgresql+psycopg://ip:ip@localhost:5432/incidentpilot")
    valkey_url: SecretStr = SecretStr("redis://localhost:6379/0")

    # --- providers, named by ROLE, never by vendor ----------------------
    chat_provider: str = "fake"
    paging_provider: str = "static"
    metrics_provider: str = "fake"
    llm_config_path: str = "config/models.yaml"

    # --- trust boundary -------------------------------------------------
    # Optional here, enforced at startup in non-dev environments (C-01).
    slack_signing_secret: SecretStr | None = None
    slack_bot_token: SecretStr | None = None
    alertmanager_bearer: SecretStr | None = None
    paging_webhook_secret: SecretStr | None = None
    deploy_bearer: SecretStr | None = None
    # PagerDuty splits its surfaces: the Events API v2 routing key creates
    # the alert, the REST token answers "who is on call". Different tokens,
    # different hosts, different headers -- one field for each so a
    # deployment cannot half-configure the provider and find out at 3 a.m.
    paging_routing_key: SecretStr | None = None
    paging_api_token: SecretStr | None = None
    # Socket Mode only. An app-level token, not the bot token: it opens the
    # WebSocket in development so no public URL is needed, and it is absent
    # in production where the HTTP receiver is the one that runs.
    slack_app_token: SecretStr | None = None
    slack_socket_mode: bool = False
    signature_max_age_s: int = 300

    # --- governance -----------------------------------------------------
    llm_budget_usd_per_incident: float = 0.50
    llm_budget_usd_per_day: float = 5.0
    llm_budget_usd_per_month: float = 50.0
    require_citations: bool = True  # CI asserts this is True in prod configs
    redact_pii: bool = True
    # W8-09. See `effective_chat_provider` below for how this is enforced.
    demo_mode: bool = False

    # --- correlation (D3) -----------------------------------------------
    correlation_window_s: int = 300
    merge_threshold: float = 0.62
    storm_threshold: int = 5

    # --- fatigue and routing (D5) ---------------------------------------
    fatigue_lookback_h: int = 8
    fatigue_max_pages: int = 2
    # The on-call answer is cached for a minute, not an hour: a rotation
    # handover mid-incident must not page the person who just went to bed.
    oncall_cache_ttl_s: int = 60
    oncall_schedule_default: str = "default"

    # --- PIR and impact (W6) ---------------------------------------------
    # Repositories permitted to write deploy rows. Empty means "any", which
    # is fine in dev and checked by assert_invariants in production: a
    # fabricated deploy row makes a *valid* citation, so this allowlist is
    # doing more work than it looks.
    deploy_repo_allowlist: tuple[str, ...] = ()
    prometheus_url: str = "http://localhost:9090"
    pir_support_threshold: float = 0.35

    # --- runbooks (D4) ---------------------------------------------------
    runbooks_dir: str = "runbooks"

    # --- scheduler (W5-17) -----------------------------------------------
    sla_nudge_interval_s: int = 300
    abandonment_after_h: int = 24

    # --- transcript and reconciliation (W4) ------------------------------
    # Slack allows a non-Marketplace app one conversations.history call per
    # minute, workspace-wide. The period is configurable so a Marketplace-
    # approved deployment can lower it -- never so a demo can cheat past it.
    history_budget_period_s: int = 60
    reconcile_interval_s: int = 900
    channel_cache_ttl_s: int = 6 * 3600
    intent_layer2_enabled: bool = True

    # --- degradation (W7-17, INV-12) -------------------------------------
    # Where the level-change banner is posted. Unset means the banner has
    # nowhere to go, and `DegradationManager` reports that as an *unannounced*
    # degradation rather than silently succeeding -- a level nobody was told
    # about is the failure INV-12 names.
    ops_channel_id: str | None = None

    # --- streams --------------------------------------------------------
    stream_maxlen: int = 100_000
    stream_alerts_raw: str = "alerts.raw"
    stream_alerts_resolved: str = "alerts.resolved"
    stream_alerts_wal: str = "alerts.wal"

    # --- server ---------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    otel_enabled: bool = False
    otel_endpoint: str | None = None
    service_name: str = "incidentpilot-api"
    shutdown_grace_s: int = Field(default=30, ge=1, le=120)

    # --- demo mode (W8-09, §16) ------------------------------------------
    # The public URL on a résumé is a URL strangers open. DEMO_MODE serves the
    # whole dashboard from seeded fixtures and makes every outbound path
    # unreachable: no Slack channel can be created, nobody can be paged, no
    # model can be called.
    #
    # Enforced structurally rather than by a guard at each call site. The
    # `effective_*` properties below force the adapter factories onto the fakes,
    # so a write is not *refused* -- there is nothing to refuse it with. A flag
    # checked in ten places is a flag someone forgets in the eleventh, and the
    # eleventh is the one that creates a channel in a stranger's workspace.
    #
    # The field itself is declared with the other governance settings above.

    @property
    def is_dev(self) -> bool:
        return self.environment.lower() in {"dev", "local", "test"}

    @property
    def is_demo(self) -> bool:
        return self.demo_mode

    @property
    def effective_chat_provider(self) -> str:
        """The chat provider after demo mode has had its say.

        Demo mode wins over configuration on purpose: someone deploying the
        demo will copy a production env file, and the one setting they must not
        be able to get wrong is the one that reaches a real workspace.
        """
        return "fake" if self.demo_mode else self.chat_provider

    @property
    def effective_paging_provider(self) -> str:
        return "fake" if self.demo_mode else self.paging_provider


settings = Settings()
