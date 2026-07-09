#!/usr/bin/env bash
set -u

CSV_FILE="test_config.csv"
REPRO_SCRIPT="single_runner.sh"
SUMMARY_FILE="flakyguard_batch_summary.csv"
SUCCESS_PATCH_DIR="successful_patches"

mkdir -p "$SUCCESS_PATCH_DIR"

if [[ ! -s "$CSV_FILE" ]]; then
    echo "ERROR: CSV file is missing or empty: $CSV_FILE"
    exit 1
fi

printf 'issue_id,status,exit_code,log_file,working_patch,saved_working_patch\n' > "$SUMMARY_FILE"

while IFS=, read -r test_type issue_id; do
    test_type="${test_type//$'\r'/}"
    issue_id="${issue_id//$'\r'/}"
    [[ -n "$issue_id" ]] || continue

    safe_type=$(printf '%s' "$test_type" | tr -cs '[:alnum:]' '_')
    [[ -n "$safe_type" ]] || safe_type="run"
    safe_issue_id=$(printf '%s' "$issue_id" | tr -cs '[:alnum:]_.-' '_')
    log_file="${safe_type}_${safe_issue_id}.log"

    echo "============================================================"
    echo "Running issue: $issue_id"
    echo "Log: $log_file"
    echo "============================================================"

    python3 flakyguard.py \
        --language java \
        --repro-script "$REPRO_SCRIPT" \
        --repro-config-csv "$CSV_FILE" \
        --repro-issue-id "$issue_id" \
        --use-jacoco-coverage \
        --context-attempts 3 \
        --thoughts-per-context 2 \
        --fixes-per-thought 3 \
        < /dev/null > "$log_file" 2>&1

    exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        status="completed"
    else
        status="failed"
    fi

    working_patch=$(grep -m 1 "Fix validated! Patch saved to:" "$log_file" \
        | sed -E 's/.*Patch saved to: //' || true)

    saved_working_patch=""

    if [[ -n "$working_patch" && -f "$working_patch" ]]; then
        saved_working_patch="$SUCCESS_PATCH_DIR/${safe_issue_id}_$(basename "$working_patch")"
        cp "$working_patch" "$saved_working_patch"
        echo "Saved working patch: $saved_working_patch"
    else
        working_patch=""
        echo "No working patch found for $issue_id"
    fi

    printf '"%s","%s",%d,"%s","%s","%s"\n' \
        "$issue_id" "$status" "$exit_code" "$log_file" "$working_patch" "$saved_working_patch" \
        >> "$SUMMARY_FILE"

done < <(tail -n +2 "$CSV_FILE" | cut -d, -f1,2)

echo
echo "Finished processing all issues."
echo "Summary: $SUMMARY_FILE"
echo "Successful patches: $SUCCESS_PATCH_DIR"

