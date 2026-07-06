#!/usr/bin/env bash
set -euo pipefail

module=${1:? "module is required"}
dir_to_python_script=${2:-.}            # e.g., /app/source
complete_test_name=${3:? "full_test_name is required"}
iterations=${4:-1}
full_test_name="${complete_test_name//#/.}"
ROOT_DIR="$(pwd)"

TESTRUNNER_DIR="${TESTRUNNER_DIR:-$ROOT_DIR/testrunner}"
IDFLAKIES_DIR="${IDFLAKIES_DIR:-$ROOT_DIR/iDFlakies}"

mkdir -p "flaky-result/buildlog" "flaky-result/testlog" "flaky-result/dtfixingtools"

build_if_exists() {
  local dir="$1" name="$2"
  if [[ -d "$dir" ]]; then
    local ts log_path
    ts="$(date +'%Y%m%dT%H%M%S')"
    log_path="flaky-result/buildlog/${name}-install-${ts}.log"
    pushd "$dir" >/dev/null
    ( set -o pipefail; mvn -DskipTests install 2>&1 | tee "$ROOT_DIR/$log_path" )
    popd >/dev/null
  fi
}

build_if_exists "$TESTRUNNER_DIR" "testrunner"
build_if_exists "$IDFLAKIES_DIR" "iDFlakies"

bash ./coverage_generator.sh "$module" . "$full_test_name" 1 || echo "Coverage failed; continuing"

MVNOPTIONS="-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false \
-Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip \
-Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip \
-Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip \
-Dxml.skip -Dcobertura.skip=true -Dfindbugs.skip=true"

CHECKSTYLE_OPTS="-P!checkstyle -Dcheckstyle.skipExec=true -Dmaven.checkstyle.skip=true -DskipCheckstyle=true \
-Dcheckstyle.config.location=google_checks.xml -Dcheckstyle.failsOnError=false -Dspotless.skip=true -Dskip.format=true"

DETECTOR_OPTS="-Ddt.detector.original_order.all_must_pass=false -Ddt.randomize.rounds=0 \
-Ddt.detector.original_order.retry_count=1 -Dtestplugin.runner.idempotent.num.runs=2 \
-Dtestplugin.runner.consec.idempotent=true -Ddt.detector.forceJUnit4=true -Ddetector.detector_type=original"

mvn clean install -DskipTests -pl "$module" -am $MVNOPTIONS $CHECKSTYLE_OPTS

mkdir -p "$ROOT_DIR/$module/.dtfixingtools"

for ((i=0; i<iterations; i++)); do
  mkdir -p "$ROOT_DIR/$module/.dtfixingtools"
  echo "$full_test_name" > "$ROOT_DIR/$module/.dtfixingtools/original-order"

  set -x
  mvn testrunner:testplugin \
    -pl "$module" \
    $MVNOPTIONS \
    $CHECKSTYLE_OPTS \
    $DETECTOR_OPTS \
    |& tee "mvn-test-$i.log"
  set +x

  mv -f "mvn-test-$i.log" "flaky-result/testlog/"

  if [[ -d "$ROOT_DIR/$module/.dtfixingtools" ]]; then
    dest="flaky-result/dtfixingtools/nioresult$i"
    mkdir -p "$dest"
    shopt -s nullglob dotglob
    contents=( "$ROOT_DIR/$module/.dtfixingtools"/* )
    if ((${#contents[@]})); then
      mv "${contents[@]}" "$dest/" 2>/dev/null || true
    fi
    shopt -u nullglob dotglob
    rm -rf "$ROOT_DIR/$module/.dtfixingtools"/* 2>/dev/null || true
  fi
done

CSV_OUT="flaky-result/rounds-test-results.csv"
mkdir -p "flaky-result"

iter_list=( flaky-result/dtfixingtools/nioresult* )
if [[ "${iter_list[0]}" == "flaky-result/dtfixingtools/nioresult*" ]]; then
  printf "%s,%s,%s,%s\n" "$full_test_name" "error" "" "" >> "$CSV_OUT"
else
  IFS=$'\n' read -r -d '' -a iter_sorted < <(
    printf "%s\n" "${iter_list[@]}" \
      | awk -F'nioresult' '{printf "%010d %s\n", $2, $0}' \
      | sort -n \
      | awk '{print $2}' && printf '\0'
  )

  for iter_path in "${iter_sorted[@]}"; do
    results_dir=""
    if [[ -d "$iter_path/test-runs/results" ]]; then
      results_dir="$iter_path/test-runs/results"
    elif [[ -d "$iter_path/results" ]]; then
      results_dir="$iter_path/results"
    fi

    status="error"
    time_value=""
    json_quoted=""

    if [[ -n "$results_dir" ]]; then
      json_file="$(find "$results_dir" -maxdepth 1 -type f -print -quit 2>/dev/null || true)"
      if [[ -n "${json_file:-}" && -f "$json_file" ]]; then
        pf="$(python3 "$dir_to_python_script/python-scripts/nio_results_to_csv.py" "$json_file" || echo "FAIL")"
        pf="${pf//$'\r'/}"; pf="${pf//$'\n'/}"
        pass_occurs=$(grep -Eo '"result":"PASS"|"result":"SUCCESS"' "$json_file" | wc -l || echo 0)
        if [[ "$pf" == "PASS" ]]; then
          status="pass"
        else
          if [[ "$pass_occurs" -ge 1 ]]; then
            status="failure"   
          else
            status="error"    
          fi
        fi
        time_value="$(python3 - "$json_file" <<'PY'
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    d = json.load(f)
res = d.get('results') or {}
time_out = None
for v in res.values():
    r = (v or {}).get('result','').upper()
    if r not in ('PASS','SUCCESS'):
        time_out = (v or {}).get('time', 0) or 0
        break
if time_out is None:
    # all PASS/SUCCESS -> fall back to time of first entry (or 0)
    if res:
        first = next(iter(res.values())) or {}
        time_out = first.get('time', 0) or 0
    else:
        time_out = 0
print(time_out)
PY
)"
        json_raw="$(tr -d '\n' < "$json_file")"
        json_quoted="\"${json_raw//\"/\"\"}\""
      fi
    fi

    printf "%s,%s,%s,%s\n" "$full_test_name" "$status" "$time_value" "${json_quoted:-""}" >> "$CSV_OUT"
  done
fi

summary_file="flaky-result/summary.txt"
mkdir -p "$(dirname "$summary_file")"

pass_count=0
fail_count=0
error_count=0

while IFS=',' read -r _ col2 _rest; do
  [[ -z "${col2:-}" ]] && continue
  col2=${col2%$'\r'}
  col2=${col2%%[[:space:]]}
  col2=${col2,,}
  
  if   [[ "$col2" == "pass" ]];    then ((pass_count+=1))
  elif [[ "$col2" == "failure" ]]; then ((fail_count+=1))
  elif [[ "$col2" == "error" ]];   then ((error_count+=1))
  fi
done < "$CSV_OUT"

{
  echo "Summary:"
  echo "Passes: $pass_count"
  echo "Failures: $fail_count"
  echo "Errors: $error_count"
} > "$summary_file"
