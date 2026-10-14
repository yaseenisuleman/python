#!/usr/bin/env sh
# Builds the wheels and publishes them to the Azure DevOps "Python Packages" repo.
#   ./publish-azure.sh               # build and publish every library
#   ./publish-azure.sh pytoolkit     # build and publish one library (folder name)
# The Azure repo is a separate clone (default: ../python-packages). Override with
# AZURE_REPO=/path/to/clone ./publish-azure.sh
set -e
root="$(cd "$(dirname "$0")" && pwd)"
azure_repo="${AZURE_REPO:-$root/../python-packages}"
azure_url="https://dev.azure.com/paycorpgroup/Data%20Flow%20Ops/_git/Python%20Packages"

if [ ! -d "$azure_repo/.git" ]; then
    echo "Cloning Azure DevOps repo -> $azure_repo"
    git clone "$azure_url" "$azure_repo"
fi

git -C "$azure_repo" pull --ff-only

sh "$root/build.sh" "$@"

mkdir -p "$azure_repo/dist"
cp "$root"/dist/*.whl "$azure_repo/dist/"

git -C "$azure_repo" add dist/*.whl
if git -C "$azure_repo" diff --cached --quiet; then
    echo "Wheels unchanged - nothing to publish."
    exit 0
fi

wheels="$(git -C "$azure_repo" diff --cached --name-only | xargs -n1 basename | tr '\n' ' ')"
source_commit="$(git -C "$root" rev-parse --short HEAD)"
git -C "$azure_repo" commit -m "Update ${wheels% }" -m "Built from source commit $source_commit."
git -C "$azure_repo" push
echo "Published to Azure DevOps: $wheels"
