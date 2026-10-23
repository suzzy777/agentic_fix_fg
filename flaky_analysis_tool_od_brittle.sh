#!/bin/bash

TEST_FOLDER_NAME=$1
DATA_FOLDER=$2
MODULE=$3
PRECEDING_TEST=$4
BRITTLE_TEST=$5
ITERATIONS=${6:-100}
CODE_VERSION=${7:-"All"}

BASE_DIR="data/${TEST_FOLDER_NAME}"
ZIP_DATA_CONTAINER="data/${DATA_FOLDER}"
RESULT_DIR="${BASE_DIR}/result"

FLAKY_DIR="${BASE_DIR}/Flaky"
FIXED_DIR="${BASE_DIR}/Fixed"
FLAKY_PASSING_DIR="${BASE_DIR}/FlakyPssingOrder"
FIXED_PASSING_DIR="${BASE_DIR}/FixedPssingOrder"
FIXED_PATCH="${BASE_DIR}/Fixed.patch"

FLAKY_M2_DIR="${BASE_DIR}/Flakym2/.m2"
if [ -d "${BASE_DIR}/Fixedm2" ]; then
    FIXED_M2_DIR="${BASE_DIR}/Fixedm2/.m2"
else
    FIXED_M2_DIR="${BASE_DIR}/Flakym2/.m2"
fi

BASE_IMAGE_NAME="flaky_base_jdk8_od_cov"
CONTAINER_NAME="$TEST_FOLDER_NAME"

if [ -f "${ZIP_DATA_CONTAINER}.zip" ]; then
    mkdir -p "${BASE_DIR}"
    unzip -o "${ZIP_DATA_CONTAINER}.zip" -d "${BASE_DIR}" > /dev/null || { echo "Failed to unzip"; exit 1; }
    if [ -d "${BASE_DIR}/${DATA_FOLDER}" ]; then
        mv "${BASE_DIR}/${DATA_FOLDER}/"* "${BASE_DIR}/"
        rmdir "${BASE_DIR}/${DATA_FOLDER}"
    fi
fi

if [ -d "$FLAKY_DIR" ]; then
    [ -d "$FLAKY_DIR/python-scripts" ] && rm -rf "$FLAKY_DIR/python-scripts"
    cp jacocoagent.jar "$FLAKY_DIR/" || { echo "jacocoagent.jar"; exit 1; }
    cp jacococli.jar "$FLAKY_DIR/" || { echo "jacococli.jar"; exit 1; }
    cp coverage_generator.sh "$FLAKY_DIR/" || { echo "Failed to copy coverage_generator.sh"; exit 1; }
    cp modify_pom_for_coverage.sh "$FLAKY_DIR/" || { echo "Failed to copy modify_pom_for_coverage.sh"; exit 1; }
    cp -r python-scripts "$FLAKY_DIR/" || { echo "Failed to copy Python scripts"; exit 1; }
    cp od_statistics_generator.sh "$FLAKY_DIR/"
else
    exit 1
fi

create_folder_with_patch() {
    BASE_DIR=$1
    PATCH_FILE=$2
    TARGET_DIR=$3
    
    rm -rf "$TARGET_DIR" 
    cp -r "$BASE_DIR" "$TARGET_DIR" || { echo "Failed to copy $BASE_DIR to $TARGET_DIR"; exit 1; }
    patch -p1 -d "$TARGET_DIR" < "$PATCH_FILE" || { echo "Failed to apply patch $PATCH_FILE to $TARGET_DIR"; exit 1; }
}


if [[ "$CODE_VERSION" == "All" || "$CODE_VERSION" == "Fixed" ]]; then
    if [[ ! -d "$FIXED_DIR" ]]; then
     rm -rf "$FIXED_DIR"  
        create_folder_with_patch "$FLAKY_DIR" "$FIXED_PATCH" "$FIXED_DIR"
    fi
fi

if [[ "$CODE_VERSION" == "All" || "$CODE_VERSION" == "FlakyPssingOrder" ]]; then
    if [[ ! -d "$FLAKY_PASSING_DIR" ]]; then
        rm -rf "$FLAKY_PASSING_DIR"  
        cp -r "$FLAKY_DIR" "$FLAKY_PASSING_DIR" || { echo "Failed to copy $FLAKY_DIR to $FLAKY_PASSING_DIR"; exit 1; }
    fi
fi


if [[ "$CODE_VERSION" == "All" || "$CODE_VERSION" == "FixedPssingOrder" ]]; then
    if [[ ! -d "$FIXED_PASSING_DIR" ]]; then
     rm -rf "$FIXED_PASSING_DIR" 
        create_folder_with_patch "$FLAKY_DIR" "$FIXED_PATCH" "$FIXED_PASSING_DIR"
    fi
fi

[ -d "$RESULT_DIR" ] && rm -rf "$RESULT_DIR"
mkdir -p "$RESULT_DIR"
rm -rf "$RESULT_DIR/$MODULE"

if ! docker images | grep -q "$BASE_IMAGE_NAME"; then
    docker build -t $BASE_IMAGE_NAME -f Dockerfile.od .
fi

SOURCE_DIRS=()
M2_DIRS=()
PRECEDING_TESTS=()
BRITTLE_TESTS=()
SINGLE_TEST_MODE=()

case "$CODE_VERSION" in
    "All")
        SOURCE_DIRS=("$FLAKY_DIR" "$FIXED_DIR" "$FLAKY_PASSING_DIR" "$FIXED_PASSING_DIR")
        M2_DIRS=("$FLAKY_M2_DIR" "$FIXED_M2_DIR" "$FLAKY_M2_DIR" "$FLAKY_M2_DIR")
        PRECEDING_TESTS=("" "" "$PRECEDING_TEST" "$PRECEDING_TEST")
        BRITTLE_TESTS=("$BRITTLE_TEST" "$BRITTLE_TEST" "$BRITTLE_TEST" "$BRITTLE_TEST")
        SINGLE_TEST_MODE=("true" "true" "false" "false")
        ;;
    "Flaky")
        SOURCE_DIRS=("$FLAKY_DIR")
        M2_DIRS=("$FLAKY_M2_DIR")
        PRECEDING_TESTS=("")
        BRITTLE_TESTS=("$BRITTLE_TEST")
        SINGLE_TEST_MODE=("true")
        ;;
    "Fixed")
        SOURCE_DIRS=("$FIXED_DIR")
        M2_DIRS=("$FIXED_M2_DIR")
        PRECEDING_TESTS=("")
        BRITTLE_TESTS=("$BRITTLE_TEST")
        SINGLE_TEST_MODE=("true")
        ;;
    "FlakyPssingOrder")
        SOURCE_DIRS=("$FLAKY_PASSING_DIR")
        M2_DIRS=("$FLAKY_M2_DIR")
        PRECEDING_TESTS=("$PRECEDING_TEST")
        BRITTLE_TESTS=("$BRITTLE_TEST")
        SINGLE_TEST_MODE=("false")
        ;;
    "FixedPssingOrder")
        SOURCE_DIRS=("$FIXED_PASSING_DIR")
        M2_DIRS=("$FLAKY_M2_DIR")
        PRECEDING_TESTS=("$PRECEDING_TEST")
        BRITTLE_TESTS=("$BRITTLE_TEST")
        SINGLE_TEST_MODE=("false")
        ;;
    *)
        exit 1
        ;;
esac

for i in "${!SOURCE_DIRS[@]}"; do
    SRC_DIR="${SOURCE_DIRS[$i]}"
    M2_DIR="${M2_DIRS[$i]}"
    CUR_PRECEDING="${PRECEDING_TESTS[$i]}"
    CUR_BRITTLE="${BRITTLE_TESTS[$i]}"
    USE_SINGLE_TEST="${SINGLE_TEST_MODE[$i]}"
    DIR_NAME=$(basename "$SRC_DIR")
    FLAKY_RESULT_DIR="$RESULT_DIR/$DIR_NAME"
    HOST_SRC_ABS="$(readlink -f "$SRC_DIR")"
    HOST_M2_ABS="$(readlink -f "$M2_DIR")"
    [[ -d "$HOST_SRC_ABS" ]] || { echo "Missing source dir: $HOST_SRC_ABS"; exit 1; }
    [[ -d "$HOST_M2_ABS" ]] || { echo "Missing m2 dir: $HOST_M2_ABS"; exit 1; }
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER_NAME" \
    --mount type=bind,source="$HOST_SRC_ABS",target=/app/source \
    --mount type=bind,source="$HOST_M2_ABS",target=/root/.m2 \
    "$BASE_IMAGE_NAME" \
    tail -f /dev/null
    docker exec -i $CONTAINER_NAME /bin/bash -c "cd /app/source && chmod +x od_statistics_generator.sh && ./od_statistics_generator.sh \"$MODULE\" \"${USE_SINGLE_TEST:+$CUR_BRITTLE}${USE_SINGLE_TEST:+"$CUR_PRECEDING"}\" \"$CUR_BRITTLE\" \"$ITERATIONS\""
    mkdir -p "$FLAKY_RESULT_DIR"
    cp -a "$SRC_DIR/flaky-result/." "$FLAKY_RESULT_DIR/"
    docker stop $CONTAINER_NAME
    docker rm $CONTAINER_NAME
   rm -rf "$SRC_DIR" 2>/dev/null || docker run --rm -v "$(dirname "$HOST_SRC_ABS")":/host "$BASE_IMAGE_NAME" /bin/bash -lc "rm -rf \"/host/$(basename "$HOST_SRC_ABS")\""

done
for _m2 in "${M2_DIRS[@]}"; do
  _m2_abs="$(readlink -f "$_m2")"
  rm -rf "$_m2_abs" 2>/dev/null || docker run --rm -v "$(dirname "$_m2_abs")":/host "$BASE_IMAGE_NAME" /bin/bash -lc "rm -rf \"/host/$(basename "$_m2_abs")\""
done
