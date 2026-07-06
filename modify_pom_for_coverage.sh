#!/bin/bash
set -euo pipefail

POM_FILE="${1:-pom.xml}"
NS="http://maven.apache.org/POM/4.0.0"

cp "$POM_FILE" "$POM_FILE.bak"

# 1) Ensure <configuration> exists under each surefire plugin
xmlstarlet ed -L -N x="$NS" \
  -s "//x:plugin[x:artifactId='maven-surefire-plugin' and not(x:configuration)]" \
    -t elem -n configuration -v "" \
  "$POM_FILE"

# 2) Remove duplicate <argLine> nodes (keep the first one only)
xmlstarlet ed -L -N x="$NS" \
  -d "//x:plugin[x:artifactId='maven-surefire-plugin']/x:configuration/x:argLine[position()>1]" \
  "$POM_FILE"

# 3) If <configuration> has no <argLine>, add one
xmlstarlet ed -L -N x="$NS" \
  -s "//x:plugin[x:artifactId='maven-surefire-plugin']/x:configuration[not(x:argLine)]" \
    -t elem -n argLine -v '${argLine}' \
  "$POM_FILE"

# 4) If <argLine> exists but lacks ${argLine} (and isn't using @{argLine}), prefix it
xmlstarlet ed -L -N x="$NS" \
  -u "//x:plugin[x:artifactId='maven-surefire-plugin']/x:configuration/x:argLine[not(contains(., '\${argLine}')) and not(contains(., '@{argLine}'))]" \
  -x "concat('\${argLine} ', .)" \
  "$POM_FILE"

