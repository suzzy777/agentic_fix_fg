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

method_only="${full_test_name#*#}"


if ! command -v xmlstarlet >/dev/null 2>&1; then
  echo "ERROR: xmlstarlet not found. Try: sudo apt-get install -y xmlstarlet"
  exit 1
fi

bash ./modify_pom_for_coverage.sh pom.xml || echo "Failed to patch pom.xml, continuing to the next coverage technique"
if [[ "$module" != "." && -f "$module/pom.xml" ]]; then
    bash./modify_pom_for_coverage.sh "$module/pom.xml" || echo "Failed to patch $module/pom.xml, continuing"
fi

mvn clean install -pl "$module" -am -Dmaven.test.skip=true $MVNOPTIONS

for i in $(seq 1 "$iterations"); do
  destfile="$basedir/${method_only}_${i}.exec"

  mvn -pl "$module" test \
    -Dtest="$full_test_name" \
    -DargLine="-javaagent:$jacoco_agent=output=file,destfile=$destfile" \
    $MVNOPTIONS || true


  if [[ ! -f "$destfile" ]]; then
      echo "Jacoco exec not found, trying the other strategy"

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
done

# If your parser produces a CSV, move it
if [[ -f "coverage_results.csv" ]]; then
  mv coverage_results.csv flaky-result/
fi

[[ -f flaky-result/coverage_results.csv ]] && echo "  - flaky-result/coverage_results.csv"

