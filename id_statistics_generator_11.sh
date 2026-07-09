#!/bin/bash
set -x
set -e
set -u
MODULE="$1"   
TEST="$2"    
rounds="$3"
NONDEXSEED="$4"
CSV_FILE="rounds-test-results.csv"
ROOT_DIR="$(pwd)"


MVNOPTIONS="-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false \
-Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip \
-Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip \
-Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip \
-Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true \
-Dspotless.skip=true -Dspotless.check.skip=true -Dossindex.skip=true \
-Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false"

mvn clean install -DskipTests -pl "$MODULE" -am $MVNOPTIONS 

if [ -n "$NONDEXSEED" ]; then
   SEED_PARAM="-DnondexSeed=$NONDEXSEED"
else
   SEED_PARAM=""
fi

if [ "${SKIP_COVERAGE:-0}" = "1" ]; then
    echo "SKIP_COVERAGE=1 -> skipping coverage generation"
else
    bash ./coverage_generator.sh "$MODULE" . "$TEST" 1 id "$NONDEXSEED" 2.1.1 || echo "Coverage failed; continuing"
fi

OUTPUT=$(mvn -pl "$MODULE" edu.illinois:nondex-maven-plugin:2.1.1:nondex \
 $SEED_PARAM -DnondexRuns=$rounds -Dtest="$TEST" $MVNOPTIONS 2>&1 )
EXEC_IDS=$(echo "$OUTPUT" | grep "nondexExecid=" | sed -E 's/.*nondexExecid=([^\ ]+).*/\1/')
SEEDS=$(echo "$OUTPUT" | grep "nondexSeed=" | sed -E 's/.*nondexSeed=([^\ ]+).*/\1/')

echo "Iteration,Execution ID,Seed,XML File,Result,Total Success,Total Failures,Total Errors,Total Skipped,Total Time" > "$CSV_FILE"

total_success_count=0
total_failure_count=0

if [ -n "$EXEC_IDS" ] && [ -n "$SEEDS" ]; then
    echo "Extracted NonDex Execution IDs and Seeds:"
    paste -d',' <(echo "$EXEC_IDS") <(echo "$SEEDS") > output.txt
    awk '!seen[$0]++ && $0 !~ /^clean_/' output.txt > filtered_output.txt
else
    exit 1
fi

iteration=1
CLASS_NAME="${TEST%%#*}"
while IFS=',' read -r line seed; do
    if [[ -n "$line" ]]; then
        NDEX_DIR="${MODULE}/.nondex/${line}"
        TXT_FILE="${NDEX_DIR}/${CLASS_NAME}.txt"
        xml_file="${NDEX_DIR}/TEST-${CLASS_NAME}.xml"

        if [[ -f "$TXT_FILE" && -f "$xml_file" ]]; then
            mkdir -p "flaky-result/testlog/$iteration"
            cp "$TXT_FILE" "flaky-result/testlog/$iteration/mvn-test-$iteration.log"
            total_tests=$(xmllint --xpath 'string(//testsuite/@tests)' "$xml_file")
            total_failures=$(xmllint --xpath 'string(//testsuite/@failures)' "$xml_file")
            total_errors=$(xmllint --xpath 'string(//testsuite/@errors)' "$xml_file")
            total_skipped=$(xmllint --xpath 'string(//testsuite/@skipped)' "$xml_file")
            total_time=$(xmllint --xpath 'string(//testsuite/@time)' "$xml_file")
            total_success=$((total_tests - total_failures - total_errors - total_skipped))
            total_failure=$((total_failures + total_errors))
            total_success_count=$((total_success_count + total_success))
            total_failure_count=$((total_failure_count + total_failure))

            if [ "$total_success" -gt 0 ]; then
               result="pass"
            elif [ "$total_failures" -gt 0 ]; then
               result="failure"
            elif [ "$total_errors" -gt 0 ]; then
               result="error"
            else
               result="unknown"
            fi
            echo "$iteration,$line,$seed,$xml_file,$result,$total_time" >> "$CSV_FILE"
        else
            echo "Error: File $TXT_FILE or $xml_file not found!"
        fi
        ((iteration++))
    fi
done < filtered_output.txt

ls -a

pass_count=0
fail_count=0
error_count=0

while IFS=',' read -r col1 col2 col3 col4 col5 col6; do
    cleaned_result=$(echo "$col5" | xargs)  # trims spaces/newlines

    if [[ $cleaned_result == "pass" ]]; then
	((pass_count+=1))
    elif [[ $cleaned_result == "failure" ]]; then
        ((fail_count+=1))
    elif [[ $cleaned_result == "error" ]]; then
        ((error_count+=1))
    fi
done < <(tail -n +2 "$CSV_FILE")


if [[ -d "$ROOT_DIR/$MODULE/.nondex" ]]; then
  cp -r "$ROOT_DIR/$MODULE/.nondex" "flaky-result/"
fi

# if [[ ! -d "$SRC_DIR/flaky-result" ]]; then
#     echo "ERROR: missing result dir: $SRC_DIR/flaky-result"
#     exit 1
# fi

total_rounds=$((pass_count + fail_count + error_count))
summary_file="flaky-result/summary.txt"
{
    echo "Summary:"
    echo "Passes: $pass_count"
    echo "Failures: $fail_count"
    echo "Errors: $error_count"
} > "$summary_file"


mv $CSV_FILE "flaky-result"


#bash ./coverage_generator.sh "$MODULE" . "$TEST" 1
set+x
