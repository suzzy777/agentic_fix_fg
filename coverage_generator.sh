#!/usr/bin/env bash
set -euo pipefail

echo "------------------------------------------------INSIDE COVERAGE GENERATOR ---------------------------------------------------------------------"

mkdir -p flaky-result/coverage
log_file="flaky-result/coverage/coverage.log"
: > "$log_file"

exec > >(tee -a "$log_file") 2>&1


module=${1:-.}

dir_to_python_script=${2:? "path to python-scripts dir is required"}
full_test_name=${3:? "full_test_name (pkg.Class#method) is required"}
iterations=${4:-5}
mode=${5:-td}                 # td | od | id | nio : how the flaky failure is triggered
extra=${6:-}                  # od: preceding test(s); id: nondex seed
nondex_version=${7:-2.1.1}    # id mode only

basedir="$(pwd)"

jacoco_agent="$basedir/jacocoagent.jar"
jacoco_cli="$basedir/jacococli.jar"

[[ -f "$jacoco_agent" ]] || { echo "ERROR: $jacoco_agent not found"; exit 1; }
[[ -f "$jacoco_cli"   ]] || { echo "ERROR: $jacoco_cli not found"; exit 1; }

MVNOPTIONS="-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false \
-Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip \
-Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dfindbugs.skip \
-Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip \
-Dxml.skip -Dcobertura.skip=true -Dfindbugs.skip=true"

CHECKSTYLE_OPTS="-P!checkstyle -Dcheckstyle.skipExec=true -Dmaven.checkstyle.skip=true -DskipCheckstyle=true \
-Dcheckstyle.config.location=google_checks.xml -Dcheckstyle.failsOnError=false -Dspotless.skip=true -Dskip.format=true"

DETECTOR_OPTS="-Ddt.detector.original_order.all_must_pass=false -Ddt.randomize.rounds=0 \
-Ddt.detector.original_order.retry_count=1 -Dtestplugin.runner.idempotent.num.runs=2 \
-Dtestplugin.runner.consec.idempotent=true -Ddt.detector.forceJUnit4=true -Ddetector.detector_type=original"

method_only="${full_test_name#*#}"
formatted_test_name="${full_test_name//#/.}"

if ! command -v xmlstarlet >/dev/null 2>&1; then
  echo "ERROR: xmlstarlet not found. Try: sudo apt-get install -y xmlstarlet"
  exit 1
fi

{
  printf '%s\n' pom.xml "$module/pom.xml"
  grep -rl --include=pom.xml 'maven-surefire-plugin' . 2>/dev/null \
    | grep -vE '^\./(testrunner|iDFlakies)/' || true
} | awk 'NF && !seen[$0]++' | while IFS= read -r pomfile; do
    [[ -f "$pomfile" ]] || continue
    bash ./modify_pom_for_coverage.sh "$pomfile" || echo "Failed to patch $pomfile, continuing"
done

if [[ "$mode" == "nio" ]]; then
    mvn clean install -pl "$module" -am -DskipTests $MVNOPTIONS $CHECKSTYLE_OPTS
else
    mvn clean install -pl "$module" -am -Dmaven.test.skip=true $MVNOPTIONS
fi

if [[ "$mode" == "od" && -n "$extra" ]]; then
    test_selector="$extra,$full_test_name"
else
    test_selector="$full_test_name"
fi

seed_param=""
if [[ "$mode" == "id" && -n "$extra" ]]; then
    seed_param="-DnondexSeed=$extra"
fi

run_with_agent() {
  local destfile="$1"
  case "$mode" in
    od)
      mvn -pl "$module" test \
        -Dsurefire.runOrder=testorder \
        -Dtest="$test_selector" \
        -DargLine="-javaagent:$jacoco_agent=output=file,destfile=$destfile" \
        $MVNOPTIONS || true
      ;;
    id)
      mvn -pl "$module" edu.illinois:nondex-maven-plugin:${nondex_version}:nondex \
        $seed_param -DnondexRuns=1 \
        -Dtest="$full_test_name" \
        -DargLine="-javaagent:$jacoco_agent=output=file,destfile=$destfile,append=false" \
        $MVNOPTIONS || true
      ;;
    nio)
      mkdir -p "$basedir/$module/.dtfixingtools"
      echo "$formatted_test_name" > "$basedir/$module/.dtfixingtools/original-order"
      # testplugin.javaopts is split on commas, so keep the agent spec comma-free
      mvn -pl "$module" testrunner:testplugin \
        -Dtestplugin.javaopts="-javaagent:$jacoco_agent=destfile=$destfile" \
        $MVNOPTIONS $CHECKSTYLE_OPTS $DETECTOR_OPTS || true
      ;;
    *)
      mvn -pl "$module" test \
        -Dtest="$full_test_name" \
        -DargLine="-javaagent:$jacoco_agent=output=file,destfile=$destfile" \
        $MVNOPTIONS || true
      ;;
  esac
}

run_fallback() {
  case "$mode" in
    od)
      mvn -q -Dmaven.repo.local=/root/.m2/repository \
        -pl "$module" -am \
        org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent \
        test \
        org.jacoco:jacoco-maven-plugin:0.8.12:report \
        -Dsurefire.runOrder=testorder \
        -Dtest="$test_selector" \
        -Drat.skip=true \
        -Dsurefire.failIfNoSpecifiedTests=false \
        -DfailIfNoTests=false \
        $MVNOPTIONS
      ;;
    id)
      mvn -q -Dmaven.repo.local=/root/.m2/repository \
        -pl "$module" -am \
        org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent \
        edu.illinois:nondex-maven-plugin:${nondex_version}:nondex \
        org.jacoco:jacoco-maven-plugin:0.8.12:report \
        $seed_param -DnondexRuns=1 \
        -Dtest="$full_test_name" \
        -Djacoco.append=false \
        -Drat.skip=true \
        -Dsurefire.failIfNoSpecifiedTests=false \
        -DfailIfNoTests=false \
        $MVNOPTIONS
      ;;
    *)
 
      mvn -q -Dmaven.repo.local=/root/.m2/repository \
        -pl "$module" -am \
        org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent \
        test \
        org.jacoco:jacoco-maven-plugin:0.8.12:report \
        -Dtest="$full_test_name" \
        -Drat.skip=true \
        -Dsurefire.failIfNoSpecifiedTests=false \
        -DfailIfNoTests=false \
        $MVNOPTIONS
      ;;
  esac
}

for i in $(seq 1 "$iterations"); do
  destfile="$basedir/${method_only}_${i}.exec"

  run_with_agent "$destfile"

  if [[ ! -f "$destfile" ]]; then
      echo "Jacoco exec not found, trying the other strategy"
      run_fallback
      xml_out="$module/target/site/jacoco/jacoco.xml"
  else
      xml_out="$module/target/${method_only}_jacoco_${i}.xml"
      java -jar "$jacoco_cli" report "$destfile" \
    --classfiles "$module/target/classes" \
    --classfiles "$module/target/test-classes" \
    --sourcefiles "$module/src/main/java" \
    --sourcefiles "$module/src/test/java" \
    --xml "$xml_out"

  fi

  python "$dir_to_python_script/python-scripts/parse_coverage.py" "$method_only" "$xml_out"

  # Move artifacts for this iteration
  mv "$destfile" flaky-result/coverage/ || true
  cp "$xml_out" flaky-result/coverage/

  # Keep coverage-run leftovers out of the statistics run that follows
  if [[ "$mode" == "nio" && -d "$basedir/$module/.dtfixingtools" ]]; then
      mv "$basedir/$module/.dtfixingtools" "flaky-result/coverage/dtfixingtools-coverage-$i" || true
  fi
done

if [[ "$mode" == "id" && -d "$module/.nondex" ]]; then
    rm -rf "$module/.nondex"
fi

if [[ -f "coverage_results.csv" ]]; then
  mv coverage_results.csv flaky-result/
fi

[[ -f flaky-result/coverage_results.csv ]] && echo "  - flaky-result/coverage_results.csv"
