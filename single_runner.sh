#!/bin/bash

CSV_FILE="test_config.csv"
ISSUE_ID_FILTER="$1"



if [[ -z "$ISSUE_ID_FILTER" ]]; then
    echo "Usage: $0 <issue_id>"
    exit 1
fi

if [[ ! -s "$CSV_FILE" ]]; then
    echo "Error: CSV file is missing or empty!"
    exit 1
fi

mapfile -t csv_lines < <(tail -n +2 "$CSV_FILE")  # Skip the header
echo "CSV Lines Read: ${#csv_lines[@]}"

for line in "${csv_lines[@]}"; do
    IFS=, read -r test_type issue_id zip module preceding_test flaky_test iterations config javav nondexSeed url <<< "$line"
    if [[ "$issue_id" != "$ISSUE_ID_FILTER" ]]; then
        continue
    fi

    if [[ -z "${FLAKYGUARD_CODE_VERSION:-}" ]]; then
        safe_type=$(printf '%s' "$test_type" | tr -cs '[:alnum:]' '_')
        [[ -n "$safe_type" ]] || safe_type="run"
        safe_issue_id=$(printf '%s' "$issue_id" | tr -cs '[:alnum:]_.-' '_')
        exec > >(tee "${safe_type}_${safe_issue_id}.log") 2>&1
    fi

    CSV_ITERATIONS="$iterations"
    CSV_CONFIG="$config"
    if [[ -n "${FLAKYGUARD_REPRO_ITERATIONS:-}" ]]; then
        iterations="$FLAKYGUARD_REPRO_ITERATIONS"
    fi
    if [[ -n "${FLAKYGUARD_CODE_VERSION:-}" ]]; then
        config="$FLAKYGUARD_CODE_VERSION"
    fi
    echo "Effective ReproFlake run for $issue_id: iterations=$iterations config=$config (csv iterations=$CSV_ITERATIONS config=$CSV_CONFIG)"

    ZIP_DATA_CONTAINER="data/${zip}"
    ZIP_PATH="${ZIP_DATA_CONTAINER}.zip"

     if [ ! -f "$ZIP_PATH" ]; then
        if [ -z "$url" ]; then
            echo "ERROR: $ZIP_PATH not found and no download URL provided (arg #8)."
            exit 1
        fi

        echo "Zip not found: $ZIP_PATH"
        echo "Downloading from: $url"
        mkdir -p "$(dirname "$ZIP_PATH")"

         if ! command -v curl >/dev/null 2>&1; then
            echo "curl not found. Trying to install curl..."

            if command -v apt-get >/dev/null 2>&1; then
                if [ "$(id -u)" -eq 0 ]; then
                    apt-get update -y && apt-get install -y curl || true
                elif command -v sudo >/dev/null 2>&1; then
                    sudo apt-get update -y && sudo apt-get install -y curl || true
                else
                    echo "WARNING: sudo not available; cannot install curl automatically."
                fi
            else
                echo "WARNING: apt-get not found; cannot install curl automatically."
            fi
        fi


        if command -v curl >/dev/null 2>&1; then
            curl -L --fail --retry 3 --retry-delay 2 -o "$ZIP_PATH.part" "$url" || {
            echo "ERROR: Download failed (curl)."
            rm -f "$ZIP_PATH.part"
            exit 1
            }
        elif command -v wget >/dev/null 2>&1; then
            wget -O "$ZIP_PATH.part" "$url" || {
            echo "ERROR: Download failed (wget)."
            rm -f "$ZIP_PATH.part"
            exit 1
            }
        else
            echo "ERROR: Neither curl nor wget is installed."
            exit 1
        fi

        mv "$ZIP_PATH.part" "$ZIP_PATH"
        echo "Downloaded: $ZIP_PATH"
    fi
    
    if [[ "$test_type" == "britle" ]]; then
        script_name="flaky_analysis_tool_od_brittle.sh"
        chmod +x "$script_name"
        bash "$script_name" "$issue_id"  "$zip" "$module" "$preceding_test" "$flaky_test" "$iterations" "$config"

    elif [[ "$test_type" == "od" ]]; then
        if [[ "$module" =~ ^hadoop ]]; then
            script_name="flaky_analysis_tool_od_proto.sh"
         else
             script_name="flaky_analysis_tool_od.sh"
        fi

        chmod +x "$script_name"
        bash "$script_name" "$issue_id"  "$zip" "$module" "$preceding_test" "$flaky_test" "$iterations" "$config"

    elif [[ "$test_type" == "td" ]]; then
        if [[ "$module" =~ ^hadoop ]]; then
            script_name="flaky_analysis_tool_td_proto.sh"
         else
           script_name="flaky_analysis_tool_td.sh"
         fi
        chmod +x "$script_name"
        bash "$script_name" "$issue_id" "$zip" "$module" "$flaky_test" "$iterations" "$config"
        
     elif [[ "$test_type" == "id" ]]; then

        if [[ "$javav" == "8" ]]; then
            script_name="flaky_analysis_tool_id_8.sh"
        elif [[ "$javav" == "11" ]]; then
            script_name="flaky_analysis_tool_id_11.sh"
        elif [[ "$javav" == "17" ]]; then
            script_name="flaky_analysis_tool_id_17.sh"
        else
            script_name="flaky_analysis_tool_id_11.sh"
        fi    
        chmod +x "$script_name"
        bash "$script_name" "$issue_id" "$zip" "$module" "$flaky_test" "$iterations" "$config" "$nondexSeed"

    elif [[ "$test_type" == "raft" ]]; then
        script_name="flaky_analysis_tool_raft.sh"
        chmod +x "$script_name"
        bash "$script_name" "$issue_id" "$zip" "$module" "$flaky_test" "$iterations" "$config"

    elif [[ "$test_type" == "nio" ]]; then
        script_name="flaky_analysis_tool_nio.sh"
        chmod +x "$script_name"
        bash "$script_name" "$issue_id" "$zip" "$module" "$flaky_test" "$iterations" "$config"    

    else
        if [[ "$module" =~ ^hadoop ]]; then
            script_name="flaky_analysis_tool_proto.sh"
        else
            script_name="flaky_analysis_tool.sh"
        fi
        chmod +x "$script_name"
        bash "$script_name" "$issue_id" "$zip" "$module" "$flaky_test" "$iterations" "$config"
    fi

    break
done

