# Interpreting optview2_bazel / OptView2 results

Optimization remarks are the compiler explaining *why* it did not do something. They are not
warnings and most of them are harmless: a typical TU produces hundreds. The value lies in the few
that sit in **hot code** and have a **cheap source-level fix**. This page explains how to find
those.

## 1. Where to start

1. Open `summary.md`. Read the **Categories** table to get a feel for the code base, then go
   through **Top sweet spots** from the top.
2. For each candidate, click through the OptView2 HTML (`html/index.html` → file page). The remark
   is shown inline below the source line; the caret (`^`) marks the column.
3. Ask: *is this code hot?* (called per sample/point/cell/iteration in a cyclic function?) If you
   do not know, measure (perf, benchmark, or PGO hotness — see §5). Cold code: ignore.
4. Apply the fix pattern, rebuild the report and check the remark disappeared — and benchmark.

## 2. Ranking

All missed-optimization remarks of one category on one source line form a *finding* ("sweet
spot"; the HTML report keeps the individual remarks). Its score is the highest *weight* (static
estimate of typical impact) of its remarks × *relative hotness* (only with PGO); the message shown
is the one of that remark. Weights:

| Category | Weight | Typical cost if hot |
|---|---:|---|
| `aliasing-loop-reload` (licm `LoadWithLoopInvariantAddressInvalidated`) | 5 | extra load per iteration **and** usually blocks vectorization |
| `loop-not-vectorized` (loop-vectorize) | 4 | up to 2–8× for simple arithmetic loops |
| `aliasing-redundant-load` (gvn `LoadClobbered`) | 3 | redundant memory access, longer dependency chains |
| `inline-no-definition` (inline `NoDefinition`) | 2.5 | call overhead + optimizer must assume the callee touches memory |
| `inline-too-costly` (inline `TooCostly`) | 2 | call overhead, lost constant propagation |
| `register-pressure`, `loop-unroll`, `inline-other` | 1.5 | situational |
| `slp-not-vectorized`, `other` | 1 | usually small |

Calls to unmangled (C) functions such as `printf`, `memcpy`, `sqrt` that cannot be inlined are
down-weighted to 0.5 — nothing to fix at source level.

**The score is a triage aid, not a measurement.** A weight-5 remark in an init function is
worth nothing; a weight-2.5 remark in the innermost loop of a 10 ms cyclic task may be the best
find of the day.

## 3. Categories and fix patterns

### aliasing-loop-reload — `licm: failed to move load with loop-invariant address because the loop may invalidate its value`

The loop reads something whose address does not change (a member, `*ref`, `vec.size()`), but
there is a store or opaque call in the loop that *might* write to it. C++'s aliasing rules let
`double&` alias `double*`, and `char`/`unsigned char`/`std::byte` alias **everything**.

```cpp
void scale_down(std::vector<double>& v, const double& a) {   // a may point into v!
    for (auto& x : v) x /= a;                                  // a reloaded every iteration
}
void scale_down(std::vector<double>& v, double a) { ... }      // fix: by value
```

Other fixes: copy members/sizes into `const` locals before the loop, write member state back
after the loop, `__restrict` on non-aliasing pointer parameters, avoid writing through `uint8_t*`
buffers in loops that read object state (serialization!).

`LoadWithLoopInvariantAddressCondExecuted` (weight 2): the load is in a conditional branch, so
hoisting is not always legal — often fine as is.

### loop-not-vectorized — `loop not vectorized` (+ reasons)

optview2_bazel attaches the vectorizer's *Analysis* remarks ("why") to the loop:

| Reason | Meaning / fix |
|---|---|
| `CantReorderFPOps` | FP reduction (`sum += a[i]`) would change rounding. Only fixable by allowing reassociation (`-ffp-model=fast`, `-fassociative-math`, `#pragma clang fp reassociate(on)` locally) — **check numerical requirements first** (ASIL/determinism!) |
| `CantVectorizeLibcall` / `CantVectorizeCall` | Call in the loop: make it inlinable (header/`constexpr`) or use vectorizable math |
| `CantIdentifyArrayBounds` / `UnsafeDep` | Possible overlap of input/output: `__restrict`, separate buffers, `#pragma clang loop vectorize(assume_safety)` |
| `CantComputeNumberOfIterations`, `CFGNotUnderstood`, `UnsupportedUncountableLoop` | Early `break`/`return`, iterator loops with complex end conditions: restructure into a counted loop |
| `NonReductionValueUsedOutsideLoop` | A value carried out of the loop is not a recognized reduction (e.g. `count_++` on a member) — use a local accumulator |
| `VectorizationNotBeneficial` (cost model) | Usually OK: trip count too low or data layout (AoS) bad |

### aliasing-redundant-load — `gvn: load of type X not eliminated because it is clobbered by store/call`

Same root cause as above, outside of loops or across calls. The "clobbered by" location points at
the store/call that forces the reload. Fixes: cache in locals, make the clobbering call inlinable,
avoid `char`-buffers aliasing state.

### inline-no-definition — `X will not be inlined into Y because its definition is unavailable`

The callee lives in another `.cpp`. Besides the call overhead, the optimizer must assume the call
reads/writes any escaped memory → this frequently *causes* the aliasing remarks above. Fixes:
small hot functions into headers (`inline`/`constexpr`), or (Thin)LTO for release builds.

### inline-too-costly — `X not inlined into Y because too costly to inline (cost=…, threshold=…)`

Usually fine. Interesting when the callee is a thin wrapper that is "big" only due to cold paths
(logging, error handling, exception throwing): move those into a separate `[[gnu::cold]]`
`[[gnu::noinline]]` function.

### register-pressure — `SpillReloadCopies` / `LoopSpillReloadCopies`

Filtered by OptView2's default `config.yaml` (very noisy). Pass `--exclude-names=` (empty) to
see them; loop spills in innermost hot loops are worth a look (too many live values, loop fission).

### Most frequent clobbers (root-cause table)

`summary.md` aggregates the "clobbered by" location of all `gvn LoadClobbered` remarks. A location
that appears in many functions is a *systemic* cause, typically:

* an **opaque function** without `const`/`pure` attributes (out-of-line math wrappers, logging,
  clocks, allocators) — every call may write any memory, so all loads around it are reloaded and
  loops containing it cannot be vectorized. Fix at the declaration (`[[gnu::const]]`/`[[gnu::pure]]`
  where the contract allows) or hoist the call out of loops;
* a store through `char`/`std::byte` buffers (serialization) aliasing object state.

### Largest stack frames

From `prologepilog StackSize`. Big frames in cyclic code hint at large by-value objects/arrays on
the stack (cache pressure, stack budget on embedded targets).

## 4. Typical false positives / noise

* **Header code** (Eigen-like math, containers) shows up wherever it is instantiated; the remark is
  reported once at the header location. Fixes there pay off across all users.
* **Tests, benchmarks, fuzzers**: use `--include-file-regex` / `--exclude-file-regex` to focus on
  production code.
* **Debug/assert paths** (`-c dbg`, `fastbuild`): never analyze non-optimized builds.
* With `-ffp-model=strict`, `CantReorderFPOps` and FP `CantVectorizeLibcall` are consequences of a
  design decision, not bugs (see below).
* The analysis is for the *host* x86 toolchain/`-march`. Target compilers (QNX/aarch64) make
  different cost-model decisions; aliasing and inlining findings transfer well, vectorization
  findings only partially.

* **Header remarks from test TUs**: a header remark is reported in every TU that inlines the
  header, including tests/benchmarks. Use `--exclude-tu-regex` (driver) / `exclude_tu_regex`
  (rule) in addition to file filters.
* **Assertion/contract failure paths**: calls like `get_user_data()` or `assertion_failed()` inside
  `if (!cond)` branches are usually `[[noreturn]]` cold paths; their `NoDefinition` remarks are
  noise. They *do* prevent vectorization of the loop (early exit), which is a deliberate
  safety-vs-speed trade-off.
* **`CantVectorizeLibcall` on plain `+`/`*`**: under `-ffp-model=strict` (or `-frounding-math`
  + `-ffp-exception-behavior=strict`) clang lowers every FP operation to a *constrained intrinsic*
  call, and the loop vectorizer refuses all of them. The remark then points at an ordinary
  multiplication. Use a what-if run (README) to quantify the cost before discussing the flag.

## 5. Using hotness (PGO)

With an instrumented profile, remarks get a hotness value and the ranking becomes real:

```
--@optview2_bazel//optview2:extra_copts=-fprofile-instr-use=/abs/path/merged.profdata,-fdiagnostics-show-hotness
```

The summary then states `Hotness (PGO): yes` and scores are scaled by relative hotness.

## 6. Verifying a fix

1. Rebuild the report (only touched TUs recompile, everything else is cached).
2. The remark should be gone, and often others disappear with it (e.g. fixing aliasing turns
   `loop not vectorized` into a vectorized loop).
3. Confirm with a benchmark: remarks tell you what the compiler did not do, not how much it costs.
