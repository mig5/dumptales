#!/bin/bash
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

bash ./tests.sh

if command -v filedust >/dev/null 2>&1; then
  filedust -y .
fi

mkdir -p dist
poetry build

# A missing C compiler must fail the release rather than ship a slow wheel.
export DUMPTALES_RELEASE_VERSION="$(poetry version -s)"
python3 - <<'PY'
import os
from pathlib import Path
from zipfile import ZipFile
version = os.environ['DUMPTALES_RELEASE_VERSION']
wheels = list(Path('dist').glob(f'dumptales-{version}-*.whl'))
if not wheels or not Path(f'dist/dumptales-{version}.tar.gz').is_file():
    raise SystemExit('Poetry did not produce a wheel and source distribution')
for wheel in wheels:
    with ZipFile(wheel) as archive:
        if not any(name.startswith('_dumptales_native') and name.endswith(('.so', '.pyd')) for name in archive.namelist()):
            raise SystemExit(f'{wheel}: C extension missing')
PY

# A standalone single-file executable
poetry run pyinstaller --noconfirm --clean --onefile --console --name dumptales \
  --distpath dist --workpath build/pyinstaller --specpath build \
  --hidden-import _dumptales_native dumptales.py
dist/dumptales --help >/dev/null

for file in dist/*; do
  [ -f "$file" ] || continue
  case "$file" in *.asc) continue ;; esac
  qubes-gpg-client --batch --armor --detach-sign "$file" > "$file.asc"
done

DISTS=(
  debian:bookworm
  debian:trixie
  ubuntu:noble
)
for dist in "${DISTS[@]}"; do
  release=${dist#*:}
  mkdir -p "dist/${release}"
  docker build -f Dockerfile.debbuild -t "dumptales-deb:${release}" \
    --no-cache --progress=plain --build-arg BASE_IMAGE="$dist" .
  docker run --rm \
    -e SUITE="$release" \
    -v "$PWD":/src \
    -v "$PWD/dist/${release}":/out \
    "dumptales-deb:${release}"
  debfiles=(dist/"${release}"/dumptales_*.deb)
  if [ "${#debfiles[@]}" -ne 1 ] || [ ! -f "${debfiles[0]}" ]; then
    echo "Expected one main dumptales .deb in dist/${release}" >&2
    exit 1
  fi
  reprepro -b /home/user/git/repo includedeb "${release}" "${debfiles[0]}"
done

sudo apt-get -y install createrepo-c rpm
RPM_DISTS=(
  fedora:43
)
KEYID="54A91143AE0AB4F7743B01FE888ED1B423A3BC99"
REPO_ROOT="${HOME}/git/repo_rpm"
REMOTE="ashpool.mig5.net:/opt/repo_rpm"
BUILD_OUTPUT="$PWD/dist"
mkdir -p dist/rpm
for dist in "${RPM_DISTS[@]}"; do
  release=$(echo "${dist}" | cut -d: -f2)
  REPO_RELEASE_ROOT="${REPO_ROOT}/${release}"
  RPM_REPO="${REPO_RELEASE_ROOT}/rpm/x86_64"
  mkdir -p "$RPM_REPO"
  docker build -f Dockerfile.rpmbuild -t "dumptales-rpm:${release}" \
    --no-cache --progress=plain --build-arg BASE_IMAGE="$dist" .

  mkdir -p "$PWD/dist/rpm"
  find "$PWD/dist/rpm" -maxdepth 1 -type f -delete

  docker run --rm -v "$PWD":/src -v "$PWD/dist/rpm":/out "dumptales-rpm:${release}"
  sudo chown -R "${USER}" "$PWD/dist"

  for file in "$BUILD_OUTPUT"/rpm/*.rpm; do
    [ -f "$file" ] || continue
    rpmsign --addsign "$file"
  done

  cp "$BUILD_OUTPUT/rpm/"*.rpm "$RPM_REPO/"
  createrepo_c "$RPM_REPO"

  echo "==> Signing repomd.xml..."
  qubes-gpg-client --local-user "$KEYID" --detach-sign --armor \
    "$RPM_REPO/repodata/repomd.xml" > "$RPM_REPO/repodata/repomd.xml.asc"
done

poetry publish

echo "==> Syncing rpm repo to server..."
rsync -aHPvz --exclude=.git --delete "$REPO_ROOT/" "$REMOTE/"

echo "Done."
