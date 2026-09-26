#!/usr/bin/env sh
# Builds the wheel (.whl) for one or more libraries into the root dist/ folder.
#   ./build.sh               # build every library
#   ./build.sh pytoolkit     # build one library (folder name)
# dist/ is committed to git and cloned onto the Kestra hosts.
set -e
root="$(cd "$(dirname "$0")" && pwd)"
dist="$root/dist"
mkdir -p "$dist"

# uv creates dist/.gitignore containing "*" when none exists, which would stop the
# wheels being committed. Keep our own copy in place so uv leaves it alone.
if [ ! -f "$dist/.gitignore" ] || [ "$(tr -d '[:space:]' < "$dist/.gitignore")" = "*" ]; then
    printf '%s\n' \
        '# Built wheels in this folder are committed on purpose (Kestra hosts clone this repo).' \
        '# This file stops uv from creating its own ".gitignore" containing "*" here.' \
        > "$dist/.gitignore"
fi

if [ "$#" -eq 0 ]; then
    set -- $(for d in "$root"/*/; do [ -f "$d/pyproject.toml" ] && basename "$d"; done)
fi

for lib in "$@"; do
    echo "Building $lib -> $dist"
    uv build "$root/$lib" --wheel --out-dir "$dist"
done
ls -1 "$dist"/*.whl
