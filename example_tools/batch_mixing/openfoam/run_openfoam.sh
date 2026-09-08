#!/usr/bin/env bash
# WSL driver for one Batch Mixing OpenFOAM run.
#
#   bash run_openfoam.sh <case_dir> [n_procs]
#
# <case_dir> is a rendered case (from render_case.py), typically a /mnt/c path in
# the MATOFX job tree. The script STAGES it into the WSL-native filesystem (fast
# ext4, not the slow /mnt/c 9p mount), runs the full pipeline, and copies the VTK
# snapshots + force history back next to the case. Prints "OK ..." on success;
# exits non-zero with a "FAIL <stage>" marker the orchestrator maps to an
# invalid/failed run.
# NB: no `set -u` — OpenFOAM's etc/bashrc references unset vars while loading.
OF_BASHRC="${OF_BASHRC:-/usr/lib/openfoam/openfoam2606/etc/bashrc}"
# shellcheck disable=SC1090
source "$OF_BASHRC"

CASE_SRC="$1"
NP="${2:-8}"
STAGE="$HOME/matofx/runs/stage_$$"

rm -rf "$STAGE"; mkdir -p "$(dirname "$STAGE")"
cp -r "$CASE_SRC" "$STAGE" || { echo "FAIL stage-copy"; exit 9; }
cd "$STAGE" || { echo "FAIL cd"; exit 9; }

run() { "$@" > "log.$1" 2>&1; }

blockMesh            > log.blockMesh 2>&1        || { echo "FAIL blockMesh"; exit 10; }
surfaceFeatureExtract > log.surfaceFeatureExtract 2>&1
snappyHexMesh -overwrite > log.snappyHexMesh 2>&1 || { echo "FAIL snappyHexMesh"; exit 11; }
topoSet             > log.topoSet 2>&1           || { echo "FAIL topoSet"; exit 12; }
mkdir -p 0; cp 0.orig/* 0/
setFields           > log.setFields 2>&1         || { echo "FAIL setFields"; exit 13; }

if [ "$NP" -gt 1 ]; then
    decomposePar -force > log.decomposePar 2>&1  || { echo "FAIL decomposePar"; exit 14; }
    mpirun -np "$NP" matofxMixingFoam -parallel > log.run 2>&1 || { echo "FAIL solver"; exit 15; }
    reconstructPar      > log.reconstructPar 2>&1
else
    matofxMixingFoam    > log.run 2>&1           || { echo "FAIL solver"; exit 15; }
fi

foamToVTK -fields '(alpha.additive U)' > log.foamToVTK 2>&1 || { echo "FAIL foamToVTK"; exit 16; }

rm -rf "$CASE_SRC/VTK" "$CASE_SRC/postProcessing"
cp -r VTK "$CASE_SRC/" 2>/dev/null
cp -r postProcessing "$CASE_SRC/" 2>/dev/null
cp log.run "$CASE_SRC/" 2>/dev/null

LAST=$(grep '^Time = ' log.run | tail -1)
rm -rf "$STAGE"
echo "OK ${LAST:-no-time}"
