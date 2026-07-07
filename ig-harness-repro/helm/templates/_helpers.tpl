{{/*
Release-name-based prefix used for all object names.
Replace with your real chart's helper if you already have one.
*/}}
{{- define "helm.prefix" -}}
{{- .Release.Name -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "helm.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
