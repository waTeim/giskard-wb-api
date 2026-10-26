{{/*
Expand the name of the chart.
*/}}
{{- define "giskard-wb-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "giskard-wb-api.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "giskard-wb-api.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "giskard-wb-api.labels" -}}
helm.sh/chart: {{ include "giskard-wb-api.chart" . }}
{{ include "giskard-wb-api.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "giskard-wb-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "giskard-wb-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "giskard-wb-api.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "giskard-wb-api.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret holding credentials.
*/}}
{{- define "giskard-wb-api.secretName" -}}
{{- default (include "giskard-wb-api.fullname" .) .Values.secrets.existingSecret }}
{{- end }}

{{/*
Name of the PVC for the SQLite database.
*/}}
{{- define "giskard-wb-api.pvcName" -}}
{{- default (printf "%s-data" (include "giskard-wb-api.fullname" .)) .Values.persistence.existingClaim }}
{{- end }}

{{/*
Server configuration: user values plus the settings the chart controls.
Fails rendering when a required setting is missing.
*/}}
{{- define "giskard-wb-api.config" -}}
{{- $c := deepCopy .Values.config }}
{{- range $path := list "oidc.issuer" "oidc.audience" "minio.endpoint" "storage.results_location" "storage.allowed_trainer_locations" "storage.allowed_dataset_locations" "runs.gpus" }}
{{- $parts := splitList "." $path }}
{{- if empty (get (get $c (first $parts) | default dict) (last $parts)) }}
{{- fail (printf "config.%s is required" $path) }}
{{- end }}
{{- end }}
{{- range $path := list "minio.access_key" "minio.secret_key" "minio.job_access_key" "minio.job_secret_key" "skypilot.service_account_token" }}
{{- $parts := splitList "." $path }}
{{- if hasKey (get $c (first $parts) | default dict) (last $parts) }}
{{- fail (printf "config.%s is a credential; set it via secrets instead" $path) }}
{{- end }}
{{- end }}
{{- if hasKey $c "secrets_dir" }}
{{- fail "config.secrets_dir is set by the chart" }}
{{- end }}
{{- $server := get $c "server" | default dict }}
{{- $_ := set $server "host" "0.0.0.0" }}
{{- $_ := set $server "port" (int .Values.containerPort) }}
{{- $_ := set $server "database_path" "/data/giskard-wb-api.db" }}
{{- $_ := set $c "server" $server }}
{{- $sky := get $c "skypilot" | default dict }}
{{- $_ := set $sky "api_server_endpoint" (include "giskard-wb-api.skypilotEndpoint" .) }}
{{- $_ := set $c "skypilot" $sky }}
{{- $_ := set $c "secrets_dir" "/etc/giskard-wb-api/secrets" }}
{{- toYaml $c }}
{{- end }}

{{/*
Name of the bundled SkyPilot release resources (mirrors skypilot.fullname in the subchart).
*/}}
{{- define "giskard-wb-api.skypilotFullname" -}}
{{- default .Release.Name .Values.skypilot.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
SkyPilot API server endpoint: explicit config, else the bundled server.
*/}}
{{- define "giskard-wb-api.skypilotEndpoint" -}}
{{- $endpoint := (.Values.config.skypilot | default dict).api_server_endpoint }}
{{- if $endpoint }}
{{- $endpoint }}
{{- else if .Values.skypilot.enabled }}
{{- printf "http://%s-api-service.%s.svc.cluster.local" (include "giskard-wb-api.skypilotFullname" .) .Release.Namespace }}
{{- else }}
{{- fail "config.skypilot.api_server_endpoint is required when skypilot.enabled is false" }}
{{- end }}
{{- end }}
