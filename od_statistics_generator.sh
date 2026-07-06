#!/bin/bash

module=$1
precedingtest=$2
flakytest=$3
iterations=${4:-100}

MVNOPTIONS="-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false -Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true -Dfindbugs.skip=true"


mvn clean install -DskipTests -pl "$module" -am $MVNOPTIONS || { echo "Maven install failed"; exit 1; }

pass_count=0
fail_count=0
error_count=0

bash ./coverage_generator.sh "$module" . "$precedingtest,$flakytest" 1 || echo "Coverage failed; continuing"

for ((i=0; i<iterations; i++)); do
    find . -name "TEST-*.xml" -delete
    set -x
    mvn -pl "$module" test -Dsurefire.runOrder=testorder -Dtest="$precedingtest,$flakytest" $MVNOPTIONS |& tee mvn-test-$i.log
    set +x

    formatted_flakytest="${flakytest//#/.}"
    test_class=$(echo "$formatted_flakytest" | rev | cut -d'.' -f2- | rev)
    test_method=$(echo "$formatted_flakytest" | awk -F. '{print $NF}')

    f=""
    while IFS= read -r file; do
        if grep -Pq "<testcase[^>]*\bclassname=\"$test_class\"[^>]*\bname=\"$test_method\"" "$file" || \
           grep -Pq "<testcase[^>]*\bname=\"$test_method\"[^>]*\bclassname=\"$test_class\"" "$file"; then
            f="$file"
            break
        fi
    done < <(find . -name "TEST-*.xml" -not -path "*target/surefire-reports/junitreports/*")

    if [[ -n "$f" && -f "$f" ]]; then
        python python-scripts/parse_surefire_report.py "$f" "$i" "$flakytest" >> rounds-test-results1.csv
    fi

    mkdir -p "flaky-result/testlog"
    mkdir -p "flaky-result/surefire-reports"

    mv mvn-test-$i.log "flaky-result/testlog"

    for xmlf in $(find . -name "TEST-*.xml" -not -path "*target/surefire-reports/*"); do 
        mv "$xmlf" "flaky-result/testlog"
    done

    cp -r "$module/target/surefire-reports" "flaky-result/surefire-reports/reports-$i"
done

formatted_flakytest="${flakytest//#/.}"
awk -v dependent="$formatted_flakytest" -F, '$1 ~ dependent { print $0 }' rounds-test-results1.csv > rounds-test-results.tmp

if [[ -s rounds-test-results.tmp ]]; then
    mv rounds-test-results.tmp rounds-test-results.csv
else
    rm -f rounds-test-results.tmp
fi

pass_count=0
fail_count=0
error_count=0

while IFS=',' read -r col1 col2 col3 col4 col5; do
    if [[ $cleaned_result == "pass" ]]; then
	((pass_count+=1))
    elif [[ $cleaned_result == "failure" ]]; then
        ((fail_count+=1))
    elif [[ $cleaned_result == "error" ]]; then
        ((error_count+=1))
    fi
done < rounds-test-results.csv


total_rounds=$((  fail_count + error_count))
summary_file="flaky-result/summary.txt"
{
    echo "Summary:"
    echo "Passes: $pass_count"
    echo "Failures: $fail_count"
    echo "Errors: $error_count"
} > "$summary_file"


mv rounds-test-results.csv "flaky-result"
dir_to_python_script="$(pwd)"
#bash ./coverage_generator.sh "$module" "$dir_to_python_script" "$flakytest" 1 

