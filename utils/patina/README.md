# Incremental solver scheduling experiment

This branch starts with measurement. The compiler algorithm is unchanged until
the unmodified source build and application replay baselines pass.

`dependencies.json` pins upstream Swift and the three dependencies needed for a
macOS compiler build. The benchmark tools require Python 3 and its standard
library. In Kitchen, invoke Python with `uv` and run compiler work through Pat.

## Evidence boundaries

There are three compiler identities:

1. Xcode's compiler is a reference for the current developer experience.
2. An unmodified source build at the pinned upstream revision is the baseline.
3. A scheduling patch built with the same dependency pins and build options is
   the candidate.

Only 2 versus 3 attributes a change to our patch. An A/A control uses the same
binary twice to check the harness and estimate noise. It cannot claim a win.

Application source, compiler transcripts, dependency modules, manifests, and
measurement outputs stay outside this public repository. The committed files
are tools, tests, and source dependency pins only. Do not publish private app
source as a reduced test without reviewing it first.

## Build the compiler

Use Swift's `utils/update-checkout` to obtain sibling `llvm-project`,
`swift-syntax`, and `cmark` checkouts, then check out the exact commits from
`dependencies.json`. Skip all other repositories and skip updating `swift`
itself. Cloning with `--skip-history --skip-tags --partial-clone` keeps Git data
small. The build script refuses mismatched or edited dependencies.

From a Kitchen worktree:

```sh
./pat run --on local --cost heavy --cores 4 -- \
  'cd /absolute/swift-project/swift && uv run --no-project python utils/patina/build_compiler.py --name upstream --jobs 4 --out /absolute/swift-project/artifacts/upstream'
```

The script respects a smaller `PAT_JOB_CORES` grant, builds Release with
assertions, includes Swift compiler modules through `hosttools`, and limits
LLVM to AArch64. It does not install or replace a system toolchain. The retained
binary, source diff, dependency-lock digest, build options, host compiler
versions, and linked libraries are recorded together. Rebuilds reuse the build
directory; each retained compiler output directory must be new.

Both builds disable appending the Git revision to the compiler's user-visible
version. Swift serializes that string into modules, so a bookkeeping commit
would otherwise break byte-for-byte module comparisons. The records retain the
actual Git revision, source diff, and binary hash; toolchain identities also
include the binary hash. No module compatibility check is disabled.

## Capture real Angles jobs

Use one isolated, source-cold diagnostic build to discover representative
workloads. Keep this discovery build separate from ordinary incremental timing:

```sh
./pat ios build angles --profile --threshold 100 --on local --json > /absolute/artifacts/pat-profile.json
```

The passed Pat result names the full `xcodebuild-log` transcript in its evidence.
Capture it with:

```sh
uv run --no-project python /absolute/swift/utils/patina/solver_bench.py capture \
  --transcript /absolute/path/from/pat/evidence.log \
  --pat-receipt /absolute/artifacts/pat-profile.json \
  --module Angles --module Lookout --module PatinaUI \
  --out /absolute/artifacts/angles.json
```

Use repeated `--source <relative-path-suffix>` arguments for a small pilot chosen
from Pat's outliers. Without those filters, capture all compile jobs of the
selected modules, including ordinary expressions. Preserve this broader corpus
for detecting regressions rather than selecting only cases that improve.

The manifest records the producing frontend's version and binary hash, batch
boundaries, primary files, all source arguments, the
working directory, response files, explicit imported modules/PCMs, plugin
executables, and SDK settings. These inputs are content-hashed. It does not copy
or mutate Pat's source lane or DerivedData. If Pat later reuses that lane and an
input changes, the capture must be recreated; benchmark runs refuse stale inputs.

Replay changes `-c` to `-typecheck` and strips output-producing and verbose timing
flags. Object files, diagnostics files, indexes, and module outputs are never
written into the captured build. Module caches belong to each benchmark arm.
Unknown output-producing options are rejected instead of guessed.

Use `--kind both` to also capture module-emission jobs. Those retain the original
module-emission action and skipped-body policy, with every output redirected to
the trial's private output directory. Successful emission must produce a
nonempty module. Include the Angles module-emission job: large repeated
declaration work can dominate an edit even when its changed file is cheap.

`synthetic_workloads.py --out <new-directory>` generates overload-chain and
generic-nesting controls at sizes 8, 16, and 32. Run their manifest with the same
runner and compiler pair. These public generated inputs isolate scaling and
help reduce regressions; only application measurements establish Angles impact.

## Run the A/A control and A/B measurement

Record Xcode's actual frontend executable for the control:

```sh
uv run --no-project python /absolute/swift/utils/patina/solver_bench.py record-compiler \
  --compiler /absolute/Xcode.xctoolchain/usr/bin/swift-frontend \
  --out /absolute/artifacts/apple-reference.json
```

Run through Pat with one core, replacing the paths below:

```sh
./pat run --on local --cost heavy --cores 1 -- \
  'uv run --no-project python /absolute/swift/utils/patina/solver_bench.py run --manifest /absolute/artifacts/angles.json --baseline /absolute/artifacts/apple-reference.json --candidate /absolute/artifacts/apple-reference.json --control --out /absolute/artifacts/control-01'
```

For A/B, remove `--control` and supply the `compiler.json` files produced by the
unmodified and patched source builds. Both must have matching upstream revision,
dependency-lock digest, and build options; identical compiler binaries are
rejected. Imported binary modules must be compatible with both frontends. A
captured frontend version or binary mismatch with the baseline is a failed run,
never an accepted timing sample;
regenerate the application dependencies using the experimental compiler before
attempting that comparison.

Each arm has warmups, followed by at least seven paired rounds. The order
alternates AB/BA. Input verification and a separate operation-counter pass stay
outside timed compiler invocations. The receipt records individual samples,
wall time, CPU time, peak RSS, load averages, compiler hashes, diagnostics/output
hashes, counters, medians, and a paired bootstrap interval. It rejects failed,
timed-out, changed-input, and changed-compiler runs.

A replay improvement requires matching diagnostics, a lower 95% speedup interval
above 1.05, no per-case median slowdown over 5%, and no peak-RSS increase over 5%.
These are pilot acceptance thresholds, not guarantees of semantic equivalence.
Shared-host samples must be repeated under comparable load before acceptance.
The A/A receipt explicitly reports whether its interval is narrow enough to
qualify a 5% timing change. A completed but noisy control is calibration
evidence; it does not establish precision for a small improvement. CPU-time
ratios are reported separately from wall-time ratios.

## Prove incremental scheduling

After establishing the unmodified baseline, the next measurement patch should
add scheduling-work counters shared by both arms:

- disjunction selections and inactive constraints visited;
- disjunctions ranked;
- binding selections, type variables visited, and binding sets ranked;
- dirty candidates revisited and queue insertions/updates, once implemented.

These new counters are follow-on work. The current runner requires a pristine
baseline at the pinned revision. Pin the shared instrumentation revision and
extend that provenance check before comparing counter-instrumented builds.

Existing scope/trail counters measure search size. A scheduling improvement
should reduce ranking/scanning work without silently changing the selected
solution. Keep search counts, deterministic diagnostics, and overload-ranking
tests alongside timing evidence. A heap by itself does not prove incremental
scheduling: transitive inference, invalidation, ties, and backtracking must all
be accounted for.

Start with explicit unsolved-disjunction membership to remove the scan of all
inactive constraints. Then incrementally update binding inference and rankings.
Use a verification mode that compares incremental decisions with the existing
selector before enabling them by default.

Run upstream Sema and overload/diagnostic tests, its fast/slow type-checker
performance suites, and the captured application corpus. Include malformed
expressions and generic closures. Add scaling regressions that count operations.

## Whole-build acceptance

Frontend replay measures type checking and the selected module-emission jobs. It excludes object code generation, linking,
signing, module preparation, scheduling, and Pat overhead.

After the replay gate passes, use experimental, explicitly selected toolchains
with Pat and fresh benchmark-owned build identities. Measure source-cold, no-op,
private-body edit, public-API edit, and deliberate-error cohorts. Verify the
intended compiler actually ran; retain compiler hashes and the Pat source/graph/
destination identities with every sample. Keep each arm's mutable build state
separate, preserve warm state for incremental cohorts, and alternate arm order.
Do not infer whole-build improvement from a sum of frontend CPU times.

`prepare_toolchain.py --compiler-record <compiler.json> --out <name.xctoolchain>
--register` creates a user toolchain registration with a unique identifier. It
wraps Xcode's driver with an explicit retained frontend path and leaves default
selection alone. It validates `xcrun` resolution and the frontend version.
The registration and wrapper live only on the benchmark host. Selection is
checked against the driver's planned frontend invocation, since a driver's
`--version` output alone does not identify its selected frontend.
The wrapper's standalone version query reports the real frontend and its binary
SHA-256, so Pat's toolchain keys distinguish baseline and candidate. Incremental
cohorts also require separate retained build-state paths and matching warm
affinity for every timed sample; lane affinity alone is not a compiler-cache hit.

`angles_build_bench.py` implements the five cohorts above through `pat ios
build`. Supply the two compiler records, their toolchains' `selection.json`
files, a clean dedicated `--kitchen` worktree, `--cohort`, and a new `--out`
directory. Invoke it through `pat run --on local --cost heavy`; set a core budget
appropriate to a full application build. It seeds each arm before measurements,
alternates AB/BA, and adds two temporary Swift fixture files for body/API/error
cohorts. It removes only its own unchanged fixtures afterward. Each sample
retains the full Pat result, elapsed time, compilation phases, and source
snapshot; mismatched paired snapshots invalidate a successful-build comparison.
The `cold-profile` cohort includes diagnostic tracing overhead and must be
reported as such. It is distinct from an ordinary clean build.

App Store builds continue to use Xcode's bundled compiler. The experimental
toolchains are for development and compiler validation.

## Tool tests

```sh
uv run --no-project python -m unittest discover -s utils/patina -p 'test_*.py' -v
```
