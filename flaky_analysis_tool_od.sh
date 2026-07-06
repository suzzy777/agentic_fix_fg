#!/bin/bash

TEST_FOLDER_NAME=$1
DATA_FOLDER=$2
MODULE=$3
PRECEDING_TEST=$4
FLAKY_TEST=$5
ITERATIONS=${6:-100} 
CODE_VERSION=${7:-"All"} 



BASE_DIR="data/${TEST_FOLDER_NAME}"
ZIP_DATA_CONTAINER="data/${DATA_FOLDER}"

if [ -f "${ZIP_DATA_CONTAINER}.zip" ]; then
    echo "Unzipping ${ZIP_DATA_CONTAINER}.zip into ${BASE_DIR}..."
    mkdir -p "${BASE_DIR}"
    unzip -o "${ZIP_DATA_CONTAINER}.zip" -d "${BASE_DIR}" > /dev/null || { echo "Failed to unzip ${ZIP_DATA_CONTAINER}.zip"; exit 1; }
    if [ -d "${BASE_DIR}/${DATA_FOLDER}" ]; then
        mv "${BASE_DIR}/${DATA_FOLDER}/"* "${BASE_DIR}/"
        rmdir "${BASE_DIR}/${DATA_FOLDER}"
    fi
fi

BASE_IMAGE_NAME="flaky_base_jdk8_od_cov"
CONTAINER_NAME="$TEST_FOLDER_NAME"
RESULT_DIR="${BASE_DIR}/result"

FLAKY_DIR="${BASE_DIR}/Flaky"
FLAKY_M2_DIR="${BASE_DIR}/Flakym2/.m2"
FLAKY_CODE_CHANGE="${BASE_DIR}/FlakyCodeChange"
FIXED_DIR="${BASE_DIR}/Fixed"
FLAKY_PASSING_DIR="${BASE_DIR}/FlakyPssingOrder"
FIXED_PASSING_DIR="${BASE_DIR}/FixedPssingOrder"
FIXED_PATCH="${BASE_DIR}/Fixed.patch"

if [ -d "${BASE_DIR}/Fixedm2" ]; then
    FIXED_M2_DIR="${BASE_DIR}/Fixedm2/.m2"
else
    FIXED_M2_DIR="${BASE_DIR}/Flakym2/.m2"
fi

if [ -d "$FLAKY_DIR" ]; then

    cp jacocoagent.jar "$FLAKY_DIR/" || { echo "Failed to copy jacocoagent.jar"; exit 1; }
    cp jacococli.jar "$FLAKY_DIR/" || { echo "Failed to copy jacococli.jar"; exit 1; }
    cp coverage_generator.sh "$FLAKY_DIR/" || { echo "Failed to copy coverage_generator.sh"; exit 1; }
    cp -r python-scripts "$FLAKY_DIR/" || { echo "Failed to copy Python scripts"; exit 1; }
    cp modify_pom_for_coverage.sh "$FLAKY_DIR/" || { echo "Failed to copy modify_pom_for_coverage.sh"; exit 1; }
    cp od_statistics_generator.sh "$FLAKY_DIR/" || { echo "Failed to copy od_statistics_generator.sh"; exit 1; }
else
    exit 1
fi


# create_folder_with_patch() {
#     BASE_DIR=$1
#     PATCH_FILE=$2
#     TARGET_DIR=$3
    
#     rm -rf "$TARGET_DIR" 
#     cp -r "$BASE_DIR" "$TARGET_DIR" || { echo "Failed to copy $BASE_DIR to $TARGET_DIR"; exit 1; }
#     patch -p1 -d "$TARGET_DIR" < "$PATCH_FILE" || { echo "Failed to apply patch $PATCH_FILE to $TARGET_DIR"; exit 1; }
# }

create_folder_with_patch() {
    BASE_DIR=$1
    PATCH_FILE=$2
    TARGET_DIR=$3

    echo "===== PATCH DEBUG START ====="
    echo "BASE_DIR=$BASE_DIR"
    echo "PATCH_FILE=$PATCH_FILE"
    echo "TARGET_DIR=$TARGET_DIR"
    echo "PWD=$(pwd)"

    echo
    echo "Patch file:"
    ls -lah "$PATCH_FILE" || true

    echo
    echo "Patch target files:"
    grep -E '^(---|\+\+\+) [ab]/' "$PATCH_FILE" || true

    echo
    echo "Patch hunk headers:"
    grep -n '^@@' "$PATCH_FILE" || true

    echo
    echo "First 220 lines of patch:"
    sed -n '1,220p' "$PATCH_FILE" || true

    rm -rf "$TARGET_DIR"
    cp -r "$BASE_DIR" "$TARGET_DIR" || {
        echo "Failed to copy $BASE_DIR to $TARGET_DIR"
        exit 1
    }

    echo
    echo "Checking target files before patch:"
    grep -E '^\+\+\+ b/' "$PATCH_FILE" | sed 's#^\+\+\+ b/##' | while read -r f; do
        echo "--- target file: $TARGET_DIR/$f"
        if [[ -f "$TARGET_DIR/$f" ]]; then
            ls -lah "$TARGET_DIR/$f"
            echo "sha256:"
            sha256sum "$TARGET_DIR/$f" || true

            echo "Lines 70-150:"
            nl -ba "$TARGET_DIR/$f" | sed -n '70,150p' || true
        else
            echo "MISSING TARGET FILE: $TARGET_DIR/$f"
        fi
    done

    echo
    echo "Dry-run patch output:"
    if ! patch --dry-run --verbose -p1 -d "$TARGET_DIR" < "$PATCH_FILE"; then
        echo
        echo "Dry-run failed. Trying real patch now so .rej files are produced..."
    fi

    echo
    echo "Real patch output:"
    if ! patch --verbose -p1 -d "$TARGET_DIR" < "$PATCH_FILE"; then
        echo
        echo "Patch failed. Reject files:"
        find "$TARGET_DIR" -name "*.rej" -print || true

        echo
        find "$TARGET_DIR" -name "*.rej" -print | while read -r rej; do
            echo "===== REJECT FILE: $rej ====="
            cat "$rej" || true
        done

        echo
        echo "Target files after failed patch:"
        grep -E '^\+\+\+ b/' "$PATCH_FILE" | sed 's#^\+\+\+ b/##' | while read -r f; do
            echo "===== TARGET AFTER FAILED PATCH: $TARGET_DIR/$f ====="
            if [[ -f "$TARGET_DIR/$f" ]]; then
                nl -ba "$TARGET_DIR/$f" | sed -n '70,160p' || true
            fi
        done

        echo "===== PATCH DEBUG END: FAILED ====="
        echo "Failed to apply patch $PATCH_FILE to $TARGET_DIR"
        exit 1
    fi

    echo "Patch applied successfully."
    echo "===== PATCH DEBUG END: SUCCESS ====="
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

if [ -d "$RESULT_DIR" ]; then
    rm -rf "$RESULT_DIR"
fi

if ! docker images | grep -q "$BASE_IMAGE_NAME"; then
    docker build -t $BASE_IMAGE_NAME -f Dockerfile.od .
fi



mkdir -p "$RESULT_DIR"
rm -rf "$RESULT_DIR/$MODULE"  

SOURCE_DIRS=()
M2_DIRS=()
case "$CODE_VERSION" in
    "All")
        SOURCE_DIRS=("$FLAKY_DIR" "$FIXED_DIR" "$FLAKY_PASSING_DIR" "$FIXED_PASSING_DIR")
        M2_DIRS=("$FLAKY_M2_DIR" "$FIXED_M2_DIR" "$FLAKY_M2_DIR" "$FLAKY_M2_DIR")
        PRECEDING_TESTS=("$PRECEDING_TEST" "$PRECEDING_TEST" "$FLAKY_TEST" "$FLAKY_TEST")
        FLAKY_TESTS=("$FLAKY_TEST" "$FLAKY_TEST" "$PRECEDING_TEST" "$PRECEDING_TEST")
        ;;

    "FlakyPssingOrder")
        if [ -d "$FLAKY_PASSING_DIR" ]; then
            SOURCE_DIRS=("$FLAKY_PASSING_DIR")
            M2_DIRS=("$FLAKY_M2_DIR")
            PRECEDING_TESTS=("$FLAKY_TEST")
            FLAKY_TESTS=("$PRECEDING_TEST")
        else
            exit 1
        fi
        ;;

     "FixedPssingOrder")
        if [ -d "$FIXED_PASSING_DIR" ]; then
            SOURCE_DIRS=("$FIXED_PASSING_DIR")
            M2_DIRS=("$FLAKY_M2_DIR")
            PRECEDING_TESTS=("$FLAKY_TEST")
            FLAKY_TESTS=("$PRECEDING_TEST")
        else
            exit 1
        fi
        ;;


    "Flaky")
        if [ -d "$FLAKY_DIR" ]; then
            SOURCE_DIRS=("$FLAKY_DIR")
            M2_DIRS=("$FLAKY_M2_DIR")
            PRECEDING_TESTS=("$PRECEDING_TEST")
            FLAKY_TESTS=("$FLAKY_TEST")
        else
            exit 1
        fi
        ;;
    "Fixed")
        if [ -d "$FIXED_DIR" ]; then
            SOURCE_DIRS=("$FIXED_DIR")
            M2_DIRS=("$FIXED_M2_DIR")
            PRECEDING_TESTS=("$PRECEDING_TEST")
            FLAKY_TESTS=("$FLAKY_TEST")
        else
            exit 1
        fi
        ;;
    *)
        exit 1
        ;;
esac

for i in "${!SOURCE_DIRS[@]}"; do
    SRC_DIR="${SOURCE_DIRS[$i]}"
    M2_DIR="${M2_DIRS[$i]}"
    CURRENT_PRECEDING_TEST="${PRECEDING_TESTS[$i]}"
    CURRENT_FLAKY_TEST="${FLAKY_TESTS[$i]}"
    DIR_NAME=$(basename "$SRC_DIR")
    FLAKY_RESULT_DIR="$RESULT_DIR/$DIR_NAME"
    HOST_SRC_ABS="$(readlink -f "$SRC_DIR")"
    HOST_M2_ABS="$(readlink -f "$M2_DIR")"
    [[ -d "$HOST_SRC_ABS" ]] || { echo "Missing source dir: $HOST_SRC_ABS"; exit 1; }
    #[[ -d "$HOST_M2_ABS" ]] || { echo "Missing m2 dir: $HOST_M2_ABS"; exit 1; }
    [[ -d "$HOST_M2_ABS" ]] || mkdir -p "$HOST_M2_ABS"

    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER_NAME" \
    --mount type=bind,source="$HOST_SRC_ABS",target=/app/source \
    --mount type=bind,source="$HOST_M2_ABS",target=/root/.m2 \
    "$BASE_IMAGE_NAME" \
    tail -f /dev/null
    docker exec -i $CONTAINER_NAME /bin/bash -c "cd /app/source && chmod +x od_statistics_generator.sh && ./od_statistics_generator.sh \"$MODULE\" \"$CURRENT_PRECEDING_TEST\" \"$CURRENT_FLAKY_TEST\" \"$ITERATIONS\""
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


