#!/usr/bin/env bash
# timing.sh — Record step-level timing for codereview pipeline observability
#
# Subcommands:
#   reset                    — Clear previous timing data
#   start <step_name>        — Record start of a named step
#   stop  <step_name>        — Record end of a named step
#   mark  <name> [value]     — Record a point-in-time event
#   summary                  — Output aggregated timing JSON to stdout
#
# Data stored as JSONL in ${CODEREVIEW_TIMING_FILE:-/tmp/codereview-timing.jsonl}
#
# Exit 0 always — timing must never break the review.
# Bash 3 compatible (macOS).

TIMING_FILE="${CODEREVIEW_TIMING_FILE:-/tmp/codereview-timing.jsonl}"

# Detect whether GNU date is available (supports %N for nanoseconds)
_HAS_GNU_DATE=false
_test_date="$(date -u +%3N 2>/dev/null || echo "N")"
case "$_test_date" in
  *[!0-9]*) _HAS_GNU_DATE=false ;;
  *)        _HAS_GNU_DATE=true ;;
esac

# get_ts_iso: ISO 8601 UTC timestamp with milliseconds
get_ts_iso() {
  if $_HAS_GNU_DATE; then
    date -u +"%Y-%m-%dT%H:%M:%S.%3NZ"
  else
    # macOS date doesn't support %N; use python3 for millisecond precision
    python3 -c "
import datetime
dt = datetime.datetime.now(datetime.timezone.utc)
print(dt.strftime('%Y-%m-%dT%H:%M:%S.') + '%03d' % (dt.microsecond // 1000) + 'Z')
" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%S.000Z"
  fi
}

cmd="${1:-}"
shift 2>/dev/null || true

case "$cmd" in
  reset)
    rm -f "$TIMING_FILE" 2>/dev/null || true
    exit 0
    ;;

  start)
    name="${1:-}"
    if [ -z "$name" ]; then
      exit 0
    fi
    ts="$(get_ts_iso)"
    printf '{"type":"start","name":"%s","ts":"%s"}\n' "$name" "$ts" >> "$TIMING_FILE" 2>/dev/null || true
    exit 0
    ;;

  stop)
    name="${1:-}"
    if [ -z "$name" ]; then
      exit 0
    fi
    ts="$(get_ts_iso)"
    printf '{"type":"stop","name":"%s","ts":"%s"}\n' "$name" "$ts" >> "$TIMING_FILE" 2>/dev/null || true
    exit 0
    ;;

  mark)
    name="${1:-}"
    value="${2:-}"
    if [ -z "$name" ]; then
      exit 0
    fi
    ts="$(get_ts_iso)"
    if [ -n "$value" ]; then
      printf '{"type":"mark","name":"%s","value":"%s","ts":"%s"}\n' "$name" "$value" "$ts" >> "$TIMING_FILE" 2>/dev/null || true
    else
      printf '{"type":"mark","name":"%s","ts":"%s"}\n' "$name" "$ts" >> "$TIMING_FILE" 2>/dev/null || true
    fi
    exit 0
    ;;

  summary)
    # Output aggregated timing summary as JSON
    if [ ! -f "$TIMING_FILE" ] || [ ! -s "$TIMING_FILE" ]; then
      echo '{"total_ms":0,"steps":[],"marks":[]}'
      exit 0
    fi

    # Use jq to process the JSONL file
    jq -s '
      # Helper: parse ISO timestamp to epoch milliseconds
      def to_epoch_ms:
        sub("\\.[0-9]+Z$"; "Z") as $sec_str |
        capture("\\.(?<ms>[0-9]+)Z$") as $ms_cap |
        (($sec_str | fromdateiso8601) * 1000) + ($ms_cap.ms | tonumber);

      # Parse all events
      . as $events |

      # Build steps: match start/stop pairs by name
      [
        [ $events[] | select(.type == "start") ] as $starts |
        [ $events[] | select(.type == "stop") ] as $stops |
        $starts[] |
        . as $start_evt |
        ($stops | map(select(.name == $start_evt.name)) | last) as $stop_evt |
        if $stop_evt then
          {
            name: $start_evt.name,
            start: $start_evt.ts,
            stop: $stop_evt.ts,
            duration_ms: (($stop_evt.ts | to_epoch_ms) - ($start_evt.ts | to_epoch_ms))
          }
        else
          empty
        end
      ] | unique_by(.name) as $steps |

      # Build marks
      [
        $events[] | select(.type == "mark") |
        if .value then
          { name: .name, value: .value, ts: .ts }
        else
          { name: .name, ts: .ts }
        end
      ] as $marks |

      # Compute total_ms
      (
        ($steps | map(select(.name == "review_total")) | first) as $rt |
        if $rt then
          $rt.duration_ms
        else
          if ($steps | length) > 0 then
            (
              [ $events[] | select(.type == "start") | .ts | to_epoch_ms ] | min
            ) as $earliest |
            (
              [ $events[] | select(.type == "stop") | .ts | to_epoch_ms ] | max
            ) as $latest |
            ($latest - $earliest)
          else
            0
          end
        end
      ) as $total |

      {
        total_ms: $total,
        steps: $steps,
        marks: $marks
      }
    ' "$TIMING_FILE" 2>/dev/null || echo '{"total_ms":0,"steps":[],"marks":[]}'
    exit 0
    ;;

  *)
    # Unknown command — silently succeed
    exit 0
    ;;
esac

exit 0
