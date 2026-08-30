#!/usr/bin/env bash

set -u
set -o pipefail

EXPECTED_SHA256="a010cbd21e9d9c398048a2758e24e61a1ba133e0a9adcbf4ab03f6d3aa8c2a51"

PROBE_PAYLOAD="$(cat <<'EOF'
printf 'probe_version=1\n'
printf 'bash=BASH_OK\n'
python3 -c 'import hashlib; print("python_sha256=" + hashlib.sha256(b"orin-stage").hexdigest())'
printf 'dpkg_arch=%s\n' "$(dpkg --print-architecture)"
exit 42
EOF
)"

usage() {
    echo "usage:" >&2
    echo "  $0 orin-stage <data-root> <workspace>" >&2
    echo "  $0 native" >&2
}

mode="${1:-}"

case "$mode" in
    orin-stage)
        if [ "$#" -ne 3 ]; then
            usage
            exit 2
        fi

        data_root="$2"
        workspace="$3"

        if ! command -v ostg >/dev/null 2>&1; then
            echo "error: ostg not found" >&2
            exit 1
        fi

        output="$(
            ostg \
                --data-root "$data_root" \
                run \
                --workspace "$workspace" \
                -- \
                /bin/bash -lc "$PROBE_PAYLOAD"
        )"
        probe_rc=$?
        ;;

    native)
        if [ "$#" -ne 1 ]; then
            usage
            exit 2
        fi

        output="$(/bin/bash -lc "$PROBE_PAYLOAD")"
        probe_rc=$?
        ;;

    *)
        usage
        exit 2
        ;;
esac

normalized="${output}"$'\n'"exit_code=${probe_rc}"

expected="$(cat <<EOF
probe_version=1
bash=BASH_OK
python_sha256=${EXPECTED_SHA256}
dpkg_arch=arm64
exit_code=42
EOF
)"

if [ "$probe_rc" -ne 42 ]; then
    echo "error: probe returned ${probe_rc}, expected 42" >&2
    exit 1
fi

if [ "$normalized" != "$expected" ]; then
    echo "error: ARM64 reference probe result mismatch" >&2
    echo "expected:" >&2
    printf '%s\n' "$expected" >&2
    echo "actual:" >&2
    printf '%s\n' "$normalized" >&2
    exit 1
fi

printf '%s\n' "$normalized"
