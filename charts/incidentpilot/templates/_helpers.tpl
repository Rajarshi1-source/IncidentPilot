{{/*
Shared naming and the environment block.

The env block is a helper rather than repeated in four Deployments for one
reason: the day someone adds a setting and updates three of the four is the day
the relay starts behaving differently from the worker for reasons nobody can
find. One definition, four consumers.
*/}}

{{- define "incidentpilot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "incidentpilot.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "incidentpilot.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "incidentpilot.labels" -}}
app.kubernetes.io/name: {{ include "incidentpilot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Every secret is a reference to something created out of band. Nothing here
produces a Secret object, and `helm template` with default values emits none —
which is asserted by a test rather than promised by a comment.
*/}}
{{- define "incidentpilot.env" -}}
- name: IP_ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
- name: IP_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: IP_CHAT_PROVIDER
  value: {{ .Values.config.chatProvider | quote }}
- name: IP_METRICS_PROVIDER
  value: {{ .Values.config.metricsProvider | quote }}
- name: IP_DATABASE_URL
  valueFrom:
    secretKeyRef: {name: {{ .Values.existingSecrets.database }}, key: url}
- name: IP_VALKEY_URL
  valueFrom:
    secretKeyRef: {name: {{ .Values.existingSecrets.valkey }}, key: url}
- name: IP_SLACK_SIGNING_SECRET
  valueFrom:
    secretKeyRef: {name: {{ .Values.existingSecrets.slack }}, key: signingSecret}
- name: IP_SLACK_BOT_TOKEN
  valueFrom:
    secretKeyRef: {name: {{ .Values.existingSecrets.slack }}, key: botToken}
- name: IP_ALERTMANAGER_BEARER
  valueFrom:
    secretKeyRef: {name: {{ .Values.existingSecrets.alertmanager }}, key: bearer}
- name: IP_PAGING_WEBHOOK_SECRET
  valueFrom:
    secretKeyRef: {name: {{ .Values.existingSecrets.paging }}, key: webhookSecret}
{{- end -}}

{{/*
The security context every pod gets. Applied here rather than per-Deployment so
a new workload cannot be added without it.
*/}}
{{- define "incidentpilot.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "incidentpilot.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}
