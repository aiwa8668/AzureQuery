#!/usr/bin/env bash
set -uo pipefail

# Lists Azure OpenAI / Foundry quota tier information for every enabled
# subscription visible to the current Azure CLI login.
#
# Intended runtime: Azure portal Cloud Shell (Bash).

API_VERSIONS=()
if [[ -n "${API_VERSION:-}" ]]; then
  API_VERSIONS+=("$API_VERSION")
fi
API_VERSIONS+=("2026-01-15-preview" "2025-10-01-preview")

usage() {
  cat <<'EOF'
Usage:
  bash check-quota-tiers.sh [--format table|json|csv] [--output FILE]

Options:
  --format  Output format. Default: table.
  --output  Write output to a file instead of printing to the screen.
  --json    Same as --format json. Kept for compatibility.

Environment:
  API_VERSION=<version>  Try this Azure Management API version first.

Examples:
  bash check-quota-tiers.sh
  bash check-quota-tiers.sh --format csv --output tiers.csv
  bash check-quota-tiers.sh --format json --output tiers.json
  API_VERSION=2025-10-01-preview bash check-quota-tiers.sh --json
EOF
}

OUTPUT_FORMAT="table"
OUTPUT_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --format)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --format" >&2
        usage >&2
        exit 2
      fi
      OUTPUT_FORMAT="$2"
      shift 2
      ;;
    --output)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --output" >&2
        usage >&2
        exit 2
      fi
      OUTPUT_FILE="$2"
      shift 2
      ;;
    --json)
      OUTPUT_FORMAT="json"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$OUTPUT_FORMAT" in
  table|json|csv)
    ;;
  *)
    echo "Unsupported format: $OUTPUT_FORMAT" >&2
    usage >&2
    exit 2
    ;;
esac

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
}

dedupe_versions() {
  local version
  local seen=","
  for version in "${API_VERSIONS[@]}"; do
    [[ -z "$version" ]] && continue
    if [[ "$seen" != *",$version,"* ]]; then
      printf '%s\n' "$version"
      seen+="$version,"
    fi
  done
}

quota_request() {
  local subscription_id="$1"
  local api_version="$2"
  local path_suffix="$3"
  local err_file="$4"

  az rest \
    --method get \
    --url "https://management.azure.com/subscriptions/${subscription_id}/providers/Microsoft.CognitiveServices/quotaTiers${path_suffix}?api-version=${api_version}" \
    --only-show-errors \
    -o json 2>"$err_file"
}

extract_quota_object() {
  jq -c '
    if type == "object" and has("value") then
      (.value[]? | select(.name == "default")) // (.value[0] // empty)
    else
      .
    end
  '
}

to_china_time() {
  local utc_time="$1"
  local normalized_time

  if [[ -z "$utc_time" || "$utc_time" == "null" ]]; then
    printf ''
    return 0
  fi

  normalized_time="$(printf '%s' "$utc_time" | sed -E 's/\.[0-9]+Z$/Z/')"
  if jq -nr --arg t "$normalized_time" '$t | fromdateiso8601 + 28800 | strftime("%Y-%m-%d %H:%M:%S UTC+08")' >/dev/null 2>&1; then
    jq -nr --arg t "$normalized_time" '$t | fromdateiso8601 + 28800 | strftime("%Y-%m-%d %H:%M:%S UTC+08")'
  else
    printf '%s' "$utc_time"
  fi
}

get_subscription_quota_tier() {
  local subscription_id="$1"
  local api_version
  local suffix
  local err_file
  local response
  local quota

  err_file="$(mktemp)"
  for api_version in $(dedupe_versions); do
    for suffix in "" "/default"; do
      : >"$err_file"
      if response="$(quota_request "$subscription_id" "$api_version" "$suffix" "$err_file")"; then
        quota="$(printf '%s' "$response" | extract_quota_object)"
        if [[ -n "$quota" && "$quota" != "null" ]]; then
          rm -f "$err_file"
          printf '%s' "$quota" | jq -c --arg apiVersion "$api_version" '. + {apiVersionUsed: $apiVersion, queryStatus: "OK"}'
          return 0
        fi
      fi
    done
  done

  rm -f "$err_file"

  jq -nc '{
    properties: {},
    apiVersionUsed: "",
    queryStatus: "未使用"
  }'
}

require_command az
require_command jq
if [[ "$OUTPUT_FORMAT" == "table" ]]; then
  require_command column
fi

if ! az account show --only-show-errors >/dev/null 2>&1; then
  echo "Azure CLI is not logged in. In Cloud Shell, open a new Cloud Shell session or run: az login" >&2
  exit 1
fi

subscriptions_json="$(az account list --all --refresh --only-show-errors -o json)"
enabled_count="$(printf '%s' "$subscriptions_json" | jq '[.[] | select(.state == "Enabled")] | length')"

if [[ "$enabled_count" == "0" ]]; then
  echo "No enabled subscriptions were found for the current Azure login." >&2
  exit 1
fi

results_file="$(mktemp)"
trap 'rm -f "$results_file"' EXIT

printf '%s' "$subscriptions_json" | jq -c '.[] | select(.state == "Enabled") | {id, name}' |
while IFS= read -r subscription; do
  subscription_id="$(printf '%s' "$subscription" | jq -r '.id')"
  subscription_name="$(printf '%s' "$subscription" | jq -r '.name')"

  quota="$(get_subscription_quota_tier "$subscription_id")"
  assignment_date="$(printf '%s' "$quota" | jq -r '.properties.assignmentDate // ""')"
  next_tier_date="$(printf '%s' "$quota" | jq -r '.properties.tierUpgradeEligibilityInfo.upgradeApplicableDate // ""')"

  jq -nc \
    --arg subscriptionId "$subscription_id" \
    --arg subscriptionName "$subscription_name" \
    --arg assignmentDateChina "$(to_china_time "$assignment_date")" \
    --arg nextTierDateChina "$(to_china_time "$next_tier_date")" \
    --argjson quota "$quota" \
    '{
      subscriptionName: $subscriptionName,
      subscriptionId: $subscriptionId,
      currentTierName: ($quota.properties.currentTierName // ""),
      assignmentDate: $assignmentDateChina,
      tierUpgradePolicy: ($quota.properties.tierUpgradePolicy // ""),
      nextTierName: ($quota.properties.tierUpgradeEligibilityInfo.nextTierName // ""),
      nextTierStatus: ($quota.properties.tierUpgradeEligibilityInfo.upgradeAvailabilityStatus // ""),
      nextTierDate: $nextTierDateChina,
      queryStatus: ($quota.queryStatus // "OK")
    }' >>"$results_file"
done

output_results() {
  case "$OUTPUT_FORMAT" in
    json)
      jq -s '.' "$results_file"
      ;;
    csv)
      {
        printf '\xEF\xBB\xBF'
        jq -sr '
          ([
            "订阅名称/SubscriptionName",
            "订阅ID/SubscriptionId",
            "当前层级/CurrentTier",
            "分配日期/AssignmentDate",
            "升级策略/UpgradePolicy",
            "下一层级/NextTier",
            "下一层级状态/NextTierStatus",
            "下一层级日期/NextTierDate",
            "查询状态/Status"
          ] | @csv),
          (.[] | [
            .subscriptionName,
            .subscriptionId,
            (.currentTierName // ""),
            (.assignmentDate // ""),
            (.tierUpgradePolicy // ""),
            (.nextTierName // ""),
            (.nextTierStatus // ""),
            (.nextTierDate // ""),
            (.queryStatus // "")
          ] | @csv)
        ' "$results_file"
      }
      ;;
    table)
      {
    printf '订阅名称/SubscriptionName\t订阅ID/SubscriptionId\t当前层级/CurrentTier\t分配日期/AssignmentDate\t升级策略/UpgradePolicy\t下一层级/NextTier\t下一层级状态/NextTierStatus\t下一层级日期/NextTierDate\t查询状态/Status\n'
    jq -r '[
      .subscriptionName,
      .subscriptionId,
      (.currentTierName // ""),
      (.assignmentDate // ""),
      (.tierUpgradePolicy // ""),
      (.nextTierName // ""),
      (.nextTierStatus // ""),
      (.nextTierDate // ""),
      (.queryStatus // "")
    ] | @tsv' "$results_file"
  } | column -t -s $'\t'
      ;;
  esac
}

if [[ -n "$OUTPUT_FILE" ]]; then
  output_results >"$OUTPUT_FILE"
  echo "Saved $OUTPUT_FORMAT output to: $OUTPUT_FILE"
else
  output_results
fi
