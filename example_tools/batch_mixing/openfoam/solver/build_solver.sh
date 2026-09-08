#!/usr/bin/env bash
# Build matofxMixingFoam — the Batch Mixing tool's OpenFOAM solver.
#
# It is stock ESI `twoLiquidMixingFoam` (v2606) + MRF (frozen-rotor) support, so
# the pitched-blade impeller can spin a rotating cell zone. Everything else is
# unchanged. In particular, the TURBULENT DIFFUSIVITY of the mixture fraction is
# ALREADY in the stock solver — `alphaDiffusionEqn.H` uses
#     fvm::laplacian(Dab + alphatab*turbulence->nut(), alpha1)
# where `alphatab` is the reciprocal turbulent Schmidt number (set in
# transportProperties). So NO diffusivity change is needed (this was the risk
# flagged in designs/cstr-mixing-demo.md §3.2 — it is handled upstream in v2606).
#
# The ONLY change vs stock is the MRF wiring:
#   createFields.H : #include "createMRF.H"
#   UEqn.H         : MRF.correctBoundaryVelocity(U);  + MRF.DDt(rho, U)
#   pEqn.H         : MRF.makeRelative(phiHbyA);  constrainPressure(..., MRF)
# (createMRF.H is on the include path; MRF lives in libfiniteVolume — no new lib.)
#
# Run inside WSL (Ubuntu) after OpenFOAM v2606 is installed. No sudo needed —
# it installs to $FOAM_USER_APPBIN. Idempotent: re-run to rebuild.
#
#   bash build_solver.sh
#
set -u
OF_BASHRC="${OF_BASHRC:-/usr/lib/openfoam/openfoam2606/etc/bashrc}"
# shellcheck disable=SC1090
source "$OF_BASHRC"

SRC="$WM_PROJECT_DIR/applications/solvers/multiphase/twoLiquidMixingFoam"
DST="${MATOFX_SOLVER_DIR:-$HOME/matofx/solvers/matofxMixingFoam}"

rm -rf "$DST"; mkdir -p "$(dirname "$DST")"
cp -r "$SRC" "$DST"
cd "$DST" || { echo "cd $DST failed"; exit 1; }
mv twoLiquidMixingFoam.C matofxMixingFoam.C

printf 'matofxMixingFoam.C\n\nEXE = $(FOAM_USER_APPBIN)/matofxMixingFoam\n' > Make/files

# --- MRF wiring (idempotent guards so re-running is safe) ---
grep -q createMRF createFields.H || \
  sed -i '/#include "createPhi.H"/a #include "createMRF.H"' createFields.H

grep -q 'MRF.correctBoundaryVelocity' UEqn.H || \
  sed -i 's|    fvVectorMatrix UEqn|    MRF.correctBoundaryVelocity(U);\n\n    fvVectorMatrix UEqn|' UEqn.H
grep -q 'MRF.DDt' UEqn.H || \
  sed -i 's|      + fvm::div(rhoPhi, U)|      + fvm::div(rhoPhi, U)\n      + MRF.DDt(rho, U)|' UEqn.H

grep -q 'MRF.makeRelative' pEqn.H || \
  sed -i 's|    adjustPhi(phiHbyA, U, p_rgh);|    MRF.makeRelative(phiHbyA);\n    adjustPhi(phiHbyA, U, p_rgh);|' pEqn.H
grep -q 'rAUf, MRF' pEqn.H || \
  sed -i 's|constrainPressure(p_rgh, U, phiHbyA, rAUf);|constrainPressure(p_rgh, U, phiHbyA, rAUf, MRF);|' pEqn.H

echo "Patches:"
grep -q createMRF createFields.H && echo "  createFields: createMRF include OK" || { echo "  createFields FAILED"; exit 1; }
grep -q 'MRF.DDt' UEqn.H && echo "  UEqn: MRF.DDt OK" || { echo "  UEqn FAILED"; exit 1; }
grep -q 'rAUf, MRF' pEqn.H && echo "  pEqn: constrainPressure(MRF) OK" || { echo "  pEqn FAILED"; exit 1; }

wclean >/dev/null 2>&1
wmake
if [ -x "$FOAM_USER_APPBIN/matofxMixingFoam" ]; then
    echo "BUILD OK -> $FOAM_USER_APPBIN/matofxMixingFoam"
else
    echo "BUILD FAILED"; exit 1
fi
