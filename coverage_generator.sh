#!/usr/bin/env bash
set -euo pipefail

# Collect JaCoCo coverage of a FAILING run, across all four flaky-test
# categories (id/od/td/nio), via a unified `mode` dispatch. For id mode (see
# the `id)` cases below and the seed normalization), a plain -DargLine is
# silently ignored by NonDex (it reads the agent from the `argLine` *project
# property*, not the CLI flag -- jacoco:prepare-agent sets exactly that
# property, so it must run in the same mvn invocation, in front of the nondex
# goal). id mode therefore goes straight to the working single-invocation form.
#
# Per-category mechanism (see each mode's case below for exactly what
# differs and why):
#   id  - targets one specific known-failing NonDex seed (from
#         test_config.csv's nondexSeed, normally called once per seed with
#         iterations=1 and a start_tag so repeated calls don't collide).
#   od  - no seed concept; non-determinism is test ORDER. Runs
#         "preceding_test,flaky_test" with -Dsurefire.runOrder=testorder.
#         Typically called once, upfront, for a fixed iteration count,
#         *before* the reproduction-stats loop runs (unlike id/nio/td,
#         which target an already-known-failing run).
#   nio - uses the testrunner:testplugin goal (iDFlakies' DTFixingTools),
#         which runs the test twice in its own JVM and writes a result
#         JSON; the agent is injected via -Dtestplugin.javaopts (comma-free,
#         since that property is comma-split) since the plugin owns its own
#         JVM command line and ignores -DargLine/jacoco:prepare-agent.
#   td  - falls through to the default case: a plain single mvn test run
#         with -DargLine. TD's distinguishing feature (a third,
#         sleep-injected "FlakyCodeChange" source tree) is a directory-level
#         concern handled by whichever script invokes this one from inside
#         that checkout -- this script itself doesn't need to know about it,
#         since `basedir="$(pwd)"` just operates on whatever tree it's run
#         from.
#   (unrecognized/omitted mode also falls through to the same default case.)

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
start_tag=${8:-1}             # base index for output filenames/tags; lets a
                               # caller invoke this script once per
                               # already-known-failing run (iterations=1)
                               # without each call's files overwriting the last

# test_config.csv sometimes carries the nondex seed as a float ("933178.0");
# strip that before it reaches -DnondexSeed.
if [[ "$mode" == "id" && -n "$extra" ]]; then
  extra="${extra%%.*}"
fi

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

# nio mode mirrors nio_statistics_generator.sh
CHECKSTYLE_OPTS="-P!checkstyle -Dcheckstyle.skipExec=true -Dmaven.checkstyle.skip=true -DskipCheckstyle=true \
-Dcheckstyle.config.location=google_checks.xml -Dcheckstyle.failsOnError=false -Dspotless.skip=true -Dskip.format=true"

DETECTOR_OPTS="-Ddt.detector.original_order.all_must_pass=false -Ddt.randomize.rounds=0 \
-Ddt.detector.original_order.retry_count=1 -Dtestplugin.runner.idempotent.num.runs=2 \
-Dtestplugin.runner.consec.idempotent=true -Ddt.detector.forceJUnit4=true -Ddetector.detector_type=original"

method_only="${full_test_name#*#}"
class_name="${full_test_name%%#*}"
formatted_test_name="${full_test_name//#/.}"

if ! command -v xmlstarlet >/dev/null 2>&1; then
  echo "ERROR: xmlstarlet not found. Try: sudo apt-get install -y xmlstarlet"
  exit 1
fi

# Patch the root and module poms plus every pom that configures surefire --
# argLine is often set in an intermediate parent pom (e.g. Hadoop's
# hadoop-project/pom.xml), and the agent never reaches the test JVM unless
# that pom's argLine is rewritten to honor ${argLine}.
{
  printf '%s\n' pom.xml "$module/pom.xml"
  grep -rl --include=pom.xml 'maven-surefire-plugin' . 2>/dev/null \
    | grep -vE '^\./(testrunner|iDFlakies)/' || true
} | awk 'NF && !seen[$0]++' | while IFS= read -r pomfile; do
    [[ -f "$pomfile" ]] || continue
    bash ./modify_pom_for_coverage.sh "$pomfile" || echo "Failed to patch $pomfile, continuing"
done

# testrunner:testplugin is a standalone goal (no test-compile phase runs before
# it), so nio mode must keep test classes; the other modes compile tests during
# their own `mvn test` run.
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

# Run the test once with the JaCoCo agent attached, using the same
# failure-triggering command as the matching statistics generator.
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
      # NonDex reads the agent from the `argLine` *project property*
      # (mavenProject.getProperties()), so a plain -DargLine=... on the CLI
      # is ignored. jacoco:prepare-agent sets exactly that property, so it
      # must run in the same invocation, in front of the nondex goal --
      # this is the only strategy for id mode; there is no separate
      # argLine-based "primary" attempt to fall back from.
      mvn -pl "$module" \
        org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent \
        edu.illinois:nondex-maven-plugin:${nondex_version}:nondex \
        $seed_param -DnondexRuns=1 -Dtest="$full_test_name" \
        -Djacoco.destFile="$destfile" -Djacoco.append=false \
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
      # td and any unrecognized mode: plain single run.
      mvn -pl "$module" test \
        -Dtest="$full_test_name" \
        -DargLine="-javaagent:$jacoco_agent=output=file,destfile=$destfile" \
        $MVNOPTIONS || true
      ;;
  esac
}

# Fallback when direct agent injection produced no exec file: bind the agent
# through jacoco:prepare-agent instead, keeping the mode-specific goals.
# `|| true`: a failing test (the run we want) makes mvn exit non-zero, which
# would otherwise abort this script under set -e before the report is written.
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
        $MVNOPTIONS || true
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
        $MVNOPTIONS || true
      ;;
    *)
      # nio has no prepare-agent equivalent (testrunner builds its own JVM
      # command line and ignores argLine), so fall back to a plain single run.
      mvn -q -Dmaven.repo.local=/root/.m2/repository \
        -pl "$module" -am \
        org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent \
        test \
        org.jacoco:jacoco-maven-plugin:0.8.12:report \
        -Dtest="$full_test_name" \
        -Drat.skip=true \
        -Dsurefire.failIfNoSpecifiedTests=false \
        -DfailIfNoTests=false \
        $MVNOPTIONS || true
      ;;
  esac
}

# The pipeline wants coverage of a FAILING run. Each agent run is classified
# from its own report (failure / pass / error = no readable report) so a
# passing run is flagged, and the script exits 3 when no run failed.
surefire_result() {
  [[ -n "${1:-}" && -f "$1" ]] || { echo error; return; }
  python3 - "$1" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suite = root if root.tag == "testsuite" else root.find(".//testsuite")
if suite is None:
    print("error"); sys.exit()
tests = int(suite.get("tests", 0))
failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
print("error" if tests == 0 else "failure" if failed > 0 else "pass")
PY
}

classify_run() {
  local xml json pass_n
  case "$mode" in
    nio)
      # testrunner result JSON: execution 1 PASS + execution 2 FAILURE = the NIO signature
      json=$(find "$basedir/$module/.dtfixingtools/test-runs/results" -maxdepth 1 -type f -print -quit 2>/dev/null || true)
      [[ -n "$json" ]] || { echo error; return; }
      pass_n=$( (grep -Eo '"result":"PASS"|"result":"SUCCESS"' "$json" || true) | wc -l)
      if grep -Eq '"result":"(FAILURE|ERROR|FAIL)"' "$json"; then
        (( pass_n >= 1 )) && echo failure || echo error
      else
        echo pass
      fi
      ;;
    id)
      # newest non-clean NonDex execution report for the class (we only ever
      # run one seeded execution per call, so "newest" unambiguously means it)
      xml=$(ls -t "$module"/.nondex/*/TEST-"$class_name".xml 2>/dev/null | grep -v '/clean_' | head -n 1 || true)
      surefire_result "$xml"
      ;;
    *)
      xml=$(find "$module/target/surefire-reports" -maxdepth 1 -name "TEST-${class_name}.xml" -print -quit 2>/dev/null || true)
      surefire_result "$xml"
      ;;
  esac
}

# Render one run's .exec into an XML report, falling back to main-classes-only
# when jacococli rejects the test-classes tree (e.g. duplicate class names
# some projects keep as non-class junk under target/test-classes).
render_report() {
  local destfile="$1" xml_out="$2"
  if ! java -jar "$jacoco_cli" report "$destfile" \
    --classfiles "$module/target/classes" \
    --classfiles "$module/target/test-classes" \
    --sourcefiles "$module/src/main/java" \
    --sourcefiles "$module/src/test/java" \
    --xml "$xml_out"; then
    echo "WARNING: jacoco report failed with test-classes; retrying with main classes only"
    java -jar "$jacoco_cli" report "$destfile" \
      --classfiles "$module/target/classes" \
      --sourcefiles "$module/src/main/java" \
      --xml "$xml_out"
  fi
}

failing_runs=0
for n in $(seq 1 "$iterations"); do
  i=$(( start_tag + n - 1 ))
  destfile="$basedir/${method_only}_${i}.exec"
  rm -f "$destfile"

  run_with_agent "$destfile"

  if [[ ! -f "$destfile" ]]; then
      echo "Jacoco exec not found, trying the other strategy"
      run_fallback
      xml_out="$module/target/site/jacoco/jacoco.xml"
  else
      xml_out="$module/target/${method_only}_jacoco_${i}.xml"
      render_report "$destfile" "$xml_out"
  fi

  # Neither strategy is guaranteed to produce a report (e.g. the fallback's
  # jacoco-maven-plugin:report goal can no-op if it captured no coverage
  # data at all). Under `set -e`, treating $xml_out as unconditionally
  # present here would kill the whole script -- including every later
  # iteration and the exit-3 convention below -- over one bad run, so this
  # run is logged as an error and skipped instead.
  if [[ ! -f "$xml_out" ]]; then
    echo "WARNING: coverage run $i produced no report at all ($xml_out); skipping this run"
    mv "$destfile" flaky-result/coverage/ 2>/dev/null || true
    echo "$i,$mode,no_report," >> flaky-result/coverage/runs.csv
    continue
  fi

  run_result=$(classify_run)
  echo "Coverage run $i ($mode) result: $run_result"
  if [[ "$run_result" == "failure" ]]; then
    failing_runs=$((failing_runs + 1))
  else
    echo "WARNING: coverage run $i did not reproduce the failure ($run_result); coverage kept but it is not a failing run"
  fi

  python "$dir_to_python_script/python-scripts/parse_coverage.py" "$method_only" "$xml_out"

  # Move artifacts for this iteration. jacoco_coverage.py picks up every
  # *jacoco*.xml under flaky-result/coverage, so a run that did NOT fail is
  # kept for inspection under a name and directory the pipeline never globs.
  mv "$destfile" flaky-result/coverage/ || true
  if [[ "$run_result" == "failure" ]]; then
    if cp "$xml_out" flaky-result/coverage/; then
      kept="flaky-result/coverage/$(basename "$xml_out")"
    else
      kept=""
    fi
  else
    mkdir -p flaky-result/coverage-not-failing
    kept="flaky-result/coverage-not-failing/${method_only}_run${i}_${run_result}.xml"
    cp "$xml_out" "$kept" || kept=""
  fi
  echo "$i,$mode,$run_result,$kept" >> flaky-result/coverage/runs.csv

  # Keep coverage-run leftovers out of the statistics run that follows
  if [[ "$mode" == "nio" && -d "$basedir/$module/.dtfixingtools" ]]; then
      mv "$basedir/$module/.dtfixingtools" "flaky-result/coverage/dtfixingtools-coverage-$i" || true
  fi
done

if [[ "$mode" == "id" && -d "$module/.nondex" ]]; then
    rm -rf "$module/.nondex"
fi

# If your parser produces a CSV, move it
if [[ -f "coverage_results.csv" ]]; then
  mv coverage_results.csv flaky-result/
fi

[[ -f flaky-result/coverage_results.csv ]] && echo "  - flaky-result/coverage_results.csv"

# Exit 3 = coverage was produced but no run failed (callers log and continue).
(( failing_runs > 0 )) || exit 3
