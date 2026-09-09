#!/bin/sh
# Entry point for installing and checking ad_policy + proxy.
#
#   ./setup.sh              answer the questions in .env.example, write .env
#   ./setup.sh --advanced   also ask the advanced questions
#   ./setup.sh check [id]   verify the running containers (optionally look up id)
#   ./setup.sh cert         print the certificate to upload to Pexip
#
# Needs Docker. Uses the host's python3 for the wizard when it is 3.8 or
# newer, otherwise runs it in a throwaway python container.
set -e
cd "$(dirname "$0")"

case "${1:-}" in
  check)
    shift
    exec sh setup/check.sh "$@"
    ;;
  cert)
    f=certs/proxy/fullchain.pem
    if [ ! -f "$f" ]; then
      echo "No certificate at $f yet. Start the containers first (docker compose up -d)." >&2
      exit 1
    fi
    echo "Upload this file to Pexip: Certificates > Trusted CA certificates"
    echo "  $(pwd)/$f"
    echo
    if command -v openssl >/dev/null 2>&1; then
      openssl x509 -in "$f" -noout -subject -enddate -ext subjectAltName 2>/dev/null || true
      echo
    fi
    cat "$f"
    ;;
  -h|--help|help)
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
      exec python3 setup/wizard.py "$@"
    fi
    echo "python3 not found; running the wizard in a container" >&2
    user_flag=""
    if [ "$(uname -s)" = "Linux" ]; then user_flag="-u $(id -u):$(id -g)"; fi
    # shellcheck disable=SC2086
    exec docker run --rm -it $user_flag -v "$(pwd):/work" -w /work python:3.14-alpine python setup/wizard.py "$@"
    ;;
esac
