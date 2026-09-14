#!/usr/bin/env bash

# Orin Stage — JP6.2.1 Physical Validation Live Demo
#
# This script performs live checks against:
#   1) the x86_64 development host,
#   2) the Orin Stage JP6.2.1 workspace,
#   3) the physical Jetson Orin NX reached through the SSH alias
#      "orin-nx-validation".
#
# It does NOT use saved output files to decide PASS/FAIL. Saved evidence is
# only integrity-checked at the beginning so the raw validation record stays
# tied to the already-verified evidence set.

set -u
set -o pipefail

ROOT="$HOME/orin-stage-validation-jp621"
DEMO="$ROOT/char-demo"

WORKSPACE="jp621-physical-validation"
JETSON="orin-nx-validation"

SOURCE="$DEMO/char_signedness.c"
X86_BIN="$DEMO/char-x86"
ARM64_BIN="$DEMO/char-arm64"

ORIN_STAGE_BIN="/tmp/orin-stage-char-arm64"
PHYSICAL_BIN="/home/ares/orin-stage-validation-jp621/char-arm64"

EXPECTED_EVIDENCE_MANIFEST_SHA="384de0dea4d73882fd3df75da678072fc87211e8c75059bc10908e1cee76915c"
EXPECTED_SOURCE_SHA="6fb25f760d92918129abe80e66b869ba7f9fd943c534308625cb5240c2fa5c02"
EXPECTED_ARM64_SHA="e2bd2447cfbe45acb0af980ca011bc7702d478e43edc640eee6f332d7b472a72"
EXPECTED_JETPACK="6.2.1+b38"
EXPECTED_L4T="36.4.4-20250616085344"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

pass() {
    printf 'PASS: %s\n' "$*"
}

section() {
    printf '\n=== %s ===\n' "$*"
}

sha_file() {
    sha256sum "$1" | awk '{print $1}'
}

# -----------------------------------------------------------------------------
# PRE-FLIGHT
# -----------------------------------------------------------------------------

for cmd in ostg ssh sha256sum diff uname grep sed wc awk; do
    command -v "$cmd" >/dev/null 2>&1 || fail "missing command: $cmd"
done

for path in \
    "$SOURCE" \
    "$X86_BIN" \
    "$ARM64_BIN" \
    "$ROOT/raw-evidence.sha256"
do
    [ -f "$path" ] || fail "missing file: $path"
done

# -----------------------------------------------------------------------------
# 1. FROZEN EVIDENCE
# -----------------------------------------------------------------------------

section "EVIDENCE INTEGRITY"

manifest_sha="$(sha_file "$ROOT/raw-evidence.sha256")"
[ "$manifest_sha" = "$EXPECTED_EVIDENCE_MANIFEST_SHA" ] \
    || fail "raw evidence manifest hash changed"

evidence_count="$(wc -l < "$ROOT/raw-evidence.sha256")"

(
    cd "$ROOT" || exit 1
    sha256sum -c raw-evidence.sha256 >/dev/null
) || fail "frozen evidence changed"

pass "Frozen evidence: ${evidence_count}/${evidence_count} files intact"

source_sha="$(sha_file "$SOURCE")"
[ "$source_sha" = "$EXPECTED_SOURCE_SHA" ] \
    || fail "source hash changed"

host_artifact_sha="$(sha_file "$ARM64_BIN")"
[ "$host_artifact_sha" = "$EXPECTED_ARM64_SHA" ] \
    || fail "ARM64 artifact hash changed"

# -----------------------------------------------------------------------------
# 2. NORMAL DEVELOPMENT HOST
# -----------------------------------------------------------------------------

section "DEVELOPMENT HOST"

host_arch="$(uname -m)"
[ "$host_arch" = "x86_64" ] \
    || fail "expected x86_64 host, got $host_arch"

printf 'Architecture: %s\n' "$host_arch"

"$X86_BIN" > "$TMP/x86.txt" || fail "x86 demo failed"
cat "$TMP/x86.txt"

grep -qx 'plain_char_signedness=signed' "$TMP/x86.txt" \
    || fail "unexpected x86 char behavior"

grep -qx 'value_as_int=-1' "$TMP/x86.txt" \
    || fail "unexpected x86 char value"

pass "Host native result captured"

generation_before="$(
    ostg inspect --workspace "$WORKSPACE" \
    | sed -n 's/^  Generation:[[:space:]]*//p' \
    | head -1
)"

[ -n "$generation_before" ] \
    || fail "could not read workspace generation"

# -----------------------------------------------------------------------------
# 3. ORIN STAGE
# -----------------------------------------------------------------------------

section "ORIN STAGE"

orin_identity="$(
    ostg run \
        --workspace "$WORKSPACE" \
        -- \
        /bin/bash -lc '
            printf "arch=%s\n" "$(dpkg --print-architecture)"
            dpkg-query -W \
              -f="\${Package} \${Version} \${Architecture}\n" \
              nvidia-jetpack nvidia-l4t-core
            exit 42
        '
)"
rc=$?

[ "$rc" -eq 42 ] \
    || fail "Orin Stage identity probe returned $rc"

printf '%s\n' "$orin_identity" \
    | grep -qx 'arch=arm64' \
    || fail "Orin Stage is not arm64"

printf '%s\n' "$orin_identity" \
    | grep -qx "nvidia-jetpack $EXPECTED_JETPACK arm64" \
    || fail "Orin Stage JetPack identity mismatch"

printf '%s\n' "$orin_identity" \
    | grep -qx "nvidia-l4t-core $EXPECTED_L4T arm64" \
    || fail "Orin Stage L4T identity mismatch"

printf 'Target: JetPack 6.2.1 / L4T 36.4.4 / arm64\n'

orin_hash_line="$(
    ostg run \
        --workspace "$WORKSPACE" \
        -- \
        /bin/bash -lc "sha256sum '$ORIN_STAGE_BIN'; exit 42"
)"
rc=$?

[ "$rc" -eq 42 ] \
    || fail "Orin Stage artifact hash probe returned $rc"

orin_artifact_sha="${orin_hash_line%% *}"
[ "$orin_artifact_sha" = "$EXPECTED_ARM64_SHA" ] \
    || fail "Orin Stage artifact differs from built artifact"

orin_output="$(
    ostg run \
        --workspace "$WORKSPACE" \
        -- \
        /bin/bash -lc "'$ORIN_STAGE_BIN'; exit 42"
)"
rc=$?

[ "$rc" -eq 42 ] \
    || fail "Orin Stage demo returned $rc"

printf '%s\n' "$orin_output" > "$TMP/orin.txt"
cat "$TMP/orin.txt"

grep -qx 'plain_char_signedness=unsigned' "$TMP/orin.txt" \
    || fail "unexpected Orin Stage char behavior"

grep -qx 'value_as_int=255' "$TMP/orin.txt" \
    || fail "unexpected Orin Stage char value"

pass "Orin Stage ARM64 target result captured"

# -----------------------------------------------------------------------------
# 4. PHYSICAL JETSON
# -----------------------------------------------------------------------------

section "PHYSICAL JETSON"

physical_identity="$(
    ssh "$JETSON" /bin/bash -s <<'REMOTE'
set -eu
printf 'model='
tr -d '\000' < /proc/device-tree/model
printf '\n'
printf 'arch=%s\n' "$(dpkg --print-architecture)"
dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' \
  nvidia-jetpack nvidia-l4t-core
REMOTE
)" || fail "could not query physical Jetson"

model="$(
    printf '%s\n' "$physical_identity" \
    | sed -n 's/^model=//p'
)"

case "$model" in
    *"Jetson Orin NX"*) ;;
    *) fail "unexpected physical device model: $model" ;;
esac

printf '%s\n' "$physical_identity" \
    | grep -qx 'arch=arm64' \
    || fail "physical Jetson is not arm64"

printf '%s\n' "$physical_identity" \
    | grep -qx "nvidia-jetpack $EXPECTED_JETPACK arm64" \
    || fail "physical JetPack identity mismatch"

printf '%s\n' "$physical_identity" \
    | grep -qx "nvidia-l4t-core $EXPECTED_L4T arm64" \
    || fail "physical L4T identity mismatch"

printf 'Device: %s\n' "$model"
printf 'Target: JetPack 6.2.1 / L4T 36.4.4 / arm64\n'

physical_hash_line="$(
    ssh "$JETSON" "sha256sum '$PHYSICAL_BIN'"
)" || fail "could not hash physical artifact"

physical_artifact_sha="${physical_hash_line%% *}"
[ "$physical_artifact_sha" = "$EXPECTED_ARM64_SHA" ] \
    || fail "physical artifact differs from built artifact"

ssh "$JETSON" "$PHYSICAL_BIN" > "$TMP/physical.txt" \
    || fail "physical demo failed"

cat "$TMP/physical.txt"

grep -qx 'plain_char_signedness=unsigned' "$TMP/physical.txt" \
    || fail "unexpected physical char behavior"

grep -qx 'value_as_int=255' "$TMP/physical.txt" \
    || fail "unexpected physical char value"

pass "Physical Jetson native ARM64 result captured"

# -----------------------------------------------------------------------------
# 5. MACHINE-CHECKED COMPARISON
# -----------------------------------------------------------------------------

section "VERIFICATION"

[ "$host_artifact_sha" = "$orin_artifact_sha" ] \
    || fail "host and Orin Stage artifact hashes differ"

[ "$host_artifact_sha" = "$physical_artifact_sha" ] \
    || fail "host and physical artifact hashes differ"

printf 'Artifact SHA256: %s\n' "$host_artifact_sha"
pass "Exact same ARM64 artifact on Orin Stage and physical Jetson"

diff -u "$TMP/orin.txt" "$TMP/physical.txt" >/dev/null \
    || fail "Orin Stage and physical outputs differ"

pass "Orin Stage output == physical Jetson output"

if diff -q "$TMP/x86.txt" "$TMP/orin.txt" >/dev/null; then
    fail "x86 and ARM64 outputs unexpectedly match"
fi

pass "x86 native behavior differs from ARM64 target behavior (expected)"

generation_after="$(
    ostg inspect --workspace "$WORKSPACE" \
    | sed -n 's/^  Generation:[[:space:]]*//p' \
    | head -1
)"

[ "$generation_after" = "$generation_before" ] \
    || fail "workspace generation changed during validation"

pass "Workspace generation unchanged: $generation_after"

# -----------------------------------------------------------------------------
# FINAL RESULT
# -----------------------------------------------------------------------------

printf '\n========================================\n'
printf 'PHYSICAL VALIDATION: PASS\n'
printf 'Exact JP6.2.1 identity and the demonstrated ARM64 userspace\n'
printf 'behavior matched the physical Jetson Orin NX reference.\n'
printf '========================================\n'
