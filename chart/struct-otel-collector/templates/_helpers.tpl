{{- define "struct-otel-collector.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "struct-otel-collector.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "struct-otel-collector.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "struct-otel-collector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "struct-otel-collector.selectorLabels" -}}
app.kubernetes.io/name: {{ include "struct-otel-collector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "struct-otel-collector.serviceAccountBase" -}}
{{- default (include "struct-otel-collector.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{- define "struct-otel-collector.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/* The Secret holding the ingest key: the one named in values, or the one this chart creates. */}}
{{- define "struct-otel-collector.secretName" -}}
{{- if .Values.ingest.existingSecret -}}
{{- .Values.ingest.existingSecret -}}
{{- else -}}
{{- include "struct-otel-collector.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "struct-otel-collector.secretKey" -}}
{{- if .Values.ingest.existingSecret -}}
{{- .Values.ingest.existingSecretKey -}}
{{- else -}}
ingest-key
{{- end -}}
{{- end -}}

{{/*
OTEL_RESOURCE_ATTRIBUTES: the environment under both of its keys and the cluster
name, then whatever the workload adds (.extra). Takes a dict: root, extra.
*/}}
{{- define "struct-otel-collector.resourceAttributes" -}}
{{- $attributes := list -}}
{{- if .root.Values.environment -}}
{{- $attributes = append $attributes (printf "deployment.environment.name=%s" .root.Values.environment) -}}
{{- $attributes = append $attributes (printf "deployment.environment=%s" .root.Values.environment) -}}
{{- end -}}
{{- if .root.Values.clusterName -}}
{{- $attributes = append $attributes (printf "k8s.cluster.name=%s" .root.Values.clusterName) -}}
{{- end -}}
{{- range .extra -}}
{{- $attributes = append $attributes . -}}
{{- end -}}
{{- join "," $attributes -}}
{{- end -}}

{{/*
Environment every collector gets. Each workload adds its own identity after it
(OTEL_RESOURCE_ATTRIBUTES and STRUCT_COLLECTOR_RESOURCE_DETECTORS), where it can
refer to the K8S_ variables defined here.
*/}}
{{- define "struct-otel-collector.commonEnv" -}}
- name: STRUCT_INGEST_URL
  value: {{ .Values.ingest.url | quote }}
- name: STRUCT_INGEST_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "struct-otel-collector.secretName" . }}
      key: {{ include "struct-otel-collector.secretKey" . }}
- name: K8S_POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: K8S_POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: K8S_NODE_NAME
  valueFrom:
    fieldRef:
      fieldPath: spec.nodeName
- name: K8S_NODE_IP
  valueFrom:
    fieldRef:
      fieldPath: status.hostIP
{{- end -}}

{{/*
--config arguments: the base config, the chosen presets, then any extra config.
The hosts preset is refused: it describes the machine the collector runs on, and
in a pod that is the pod (its network, its name), not the node.
*/}}
{{- define "struct-otel-collector.args" -}}
{{- if has "hosts" .presets -}}
{{- fail "the hosts preset is for the Linux install, where the collector runs on the machine itself. In a pod it would report the pod's network and name as the node's. Node CPU, memory, filesystem and network come from the kubernetes-node preset." -}}
{{- end -}}
- --config=/etc/struct-otel-collector/config.yaml
{{- range .presets }}
- --config=/etc/struct-otel-collector/presets/{{ . }}.yaml
{{- end }}
{{- if .extraConfig }}
- --config=/etc/struct-otel-collector-extra/extra.yaml
{{- end }}
{{- end -}}
