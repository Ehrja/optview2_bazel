"""Knowledge base used to triage clang optimization remarks.

Every missed-optimization remark is mapped to a category with a weight (rough
typical performance impact, 1..5), an explanation and concrete remedies. The
weights only *rank* findings; they are no substitute for profiling.
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class Category:
    key: str
    title: str
    weight: float
    explanation: str
    remedies: tuple[str, ...]


ALIASING_LOOP = Category(
    key="aliasing-loop-reload",
    title="Loop-invariant load is reloaded every iteration (aliasing)",
    weight=5.0,
    explanation=(
        "LICM could not hoist a load out of a loop because a store (or opaque call) inside the "
        "loop may write to the same memory. The value is re-read from memory on every iteration, "
        "which usually also blocks vectorization."
    ),
    remedies=(
        "Copy the value into a local variable before the loop (`const auto n = obj.size_;`).",
        "Pass small read-only parameters by value instead of `const T&`.",
        "Use `__restrict` on pointer parameters that never alias.",
        "Avoid writing through `char*`/`unsigned char*`/`std::byte*` in the loop (they alias everything).",
        "Accumulate into locals and write member state back once after the loop.",
    ),
)

ALIASING_REDUNDANT_LOAD = Category(
    key="aliasing-redundant-load",
    title="Redundant load not eliminated (clobbered memory)",
    weight=3.0,
    explanation=(
        "GVN found a load that would be redundant, but an intervening store or call may modify "
        'the memory ("clobbered by"). The compiler must keep the memory access.'
    ),
    remedies=(
        "Cache values in locals across stores/calls instead of re-reading members or references.",
        "Make called helpers visible for inlining (header/`inline`/LTO) so the compiler can see they do not write.",
        "Use `__restrict` or distinct types; avoid `char`-typed buffers aliasing object state.",
    ),
)

LOOP_NOT_VECTORIZED = Category(
    key="loop-not-vectorized",
    title="Loop not vectorized",
    weight=4.0,
    explanation=(
        "The loop vectorizer gave up. The attached reasons (Analysis remarks) tell why: e.g. "
        "floating-point reassociation not allowed, unknown trip count, calls in the loop, possible "
        "memory dependences, or control flow it does not understand."
    ),
    remedies=(
        "FP reductions: allow reassociation for that loop/TU (e.g. `#pragma clang fp reassociate(on)`) "
        "only if numerically acceptable.",
        "Calls in loop: make the callee inlinable (header, `constexpr`, LTO) or hoist it.",
        "Possible dependences: use `__restrict`, separate input/output buffers, or "
        "`#pragma clang loop vectorize(assume_safety)`.",
        "Unknown bounds / complex CFG: simplify the loop (single exit, canonical index, no early break).",
    ),
)

SLP_NOT_VECTORIZED = Category(
    key="slp-not-vectorized",
    title="Straight-line code not vectorized (SLP)",
    weight=1.0,
    explanation=(
        "The SLP vectorizer tried to combine independent scalar operations into SIMD operations "
        "but the cost model said it is not beneficial or it is not possible. Usually low impact."
    ),
    remedies=("Only worth attention in measured hot spots; consider structuring data as arrays (SoA).",),
)

INLINE_NO_DEFINITION = Category(
    key="inline-no-definition",
    title="Call not inlined: callee definition not visible",
    weight=2.5,
    explanation=(
        "The callee is defined in another translation unit (or library), so it cannot be inlined "
        "and the optimizer must assume it may read/write any escaped memory. This is a typical "
        "root cause for subsequent aliasing and vectorization misses."
    ),
    remedies=(
        "Move small, hot functions (getters, math helpers) into the header as `inline`/`constexpr`.",
        "Enable (Thin)LTO for release builds to allow cross-TU inlining.",
        "Mark pure functions `[[gnu::pure]]`/`[[gnu::const]]` when they cannot be inlined.",
    ),
)

INLINE_TOO_COSTLY = Category(
    key="inline-too-costly",
    title="Call not inlined: too costly",
    weight=2.0,
    explanation=(
        "The inliner's cost model rejected the call (cost > threshold). Often fine for large "
        "callees; problematic for wrappers that are hot and only look big because of rarely used "
        "paths (error handling, logging, exceptions)."
    ),
    remedies=(
        "Split cold paths (error handling/logging) out of hot functions into `[[gnu::cold]]`/noinline helpers.",
        "Avoid large by-value objects/temporaries in hot wrappers.",
        "Use `[[gnu::always_inline]]` only after measuring.",
    ),
)

INLINE_OTHER = Category(
    key="inline-other",
    title="Call not inlined (other reason)",
    weight=1.5,
    explanation="The inliner declined for another reason (attributes, recursion, interposition, ...).",
    remedies=("Read the remark message; check `noinline`, recursion, `-fsemantic-interposition`.",),
)

UNROLL = Category(
    key="loop-unroll",
    title="Loop unrolling not performed",
    weight=1.5,
    explanation="The loop unroller did not (fully) unroll a loop, e.g. a requested `#pragma unroll` was not honored.",
    remedies=("Make trip counts compile-time constants where possible; check the pragma/trip count.",),
)

REGISTER_PRESSURE = Category(
    key="register-pressure",
    title="Register spills/reloads",
    weight=1.5,
    explanation=(
        "The register allocator had to spill values to the stack (and reload them). Inside loops "
        "this costs memory traffic every iteration."
    ),
    remedies=("Reduce live values in hot loops, split loops (loop fission), avoid huge inlined bodies.",),
)

OTHER = Category(
    key="other",
    title="Other missed optimization",
    weight=1.0,
    explanation="A missed optimization that is not specifically classified by optview2_bazel.",
    remedies=("See the remark message and the OptView2 HTML report for context.",),
)

CATEGORIES = {
    c.key: c
    for c in (
        ALIASING_LOOP,
        ALIASING_REDUNDANT_LOAD,
        LOOP_NOT_VECTORIZED,
        SLP_NOT_VECTORIZED,
        INLINE_NO_DEFINITION,
        INLINE_TOO_COSTLY,
        INLINE_OTHER,
        UNROLL,
        REGISTER_PRESSURE,
        OTHER,
    )
}

_BY_PASS_AND_NAME = {
    ("inline", "NoDefinition"): INLINE_NO_DEFINITION,
    ("inline", "TooCostly"): INLINE_TOO_COSTLY,
}

_BY_PASS = {
    "licm": ALIASING_LOOP,
    "gvn": ALIASING_REDUNDANT_LOAD,
    "loop-vectorize": LOOP_NOT_VECTORIZED,
    "slp-vectorizer": SLP_NOT_VECTORIZED,
    "inline": INLINE_OTHER,
    "loop-unroll": UNROLL,
    "loop-unroll-and-jam": UNROLL,
    "regalloc": REGISTER_PRESSURE,
}

# Remark names whose typical impact differs from their category's weight.
_WEIGHT_BY_NAME = {
    "LoadWithLoopInvariantAddressCondExecuted": 2.0,  # conditional load: hoisting not always legal
    "LoopSpillReloadCopies": 2.5,
}

# Calls to C functions (libc, OS APIs; unmangled names) are rarely actionable.
_C_CALLEE_WEIGHT = 0.5

# Explanations for loop-vectorize Analysis remark names (the "why").
VECTORIZE_REASONS = {
    "CantReorderFPOps": "FP reduction: reassociation not allowed (strict IEEE semantics)",
    "CantReorderMemOps": "memory operations cannot be reordered",
    "CantVectorizeLibcall": "loop contains a call that cannot be vectorized",
    "CantVectorizeCall": "loop contains a call that cannot be vectorized",
    "CantVectorizeInstruction": "loop contains an instruction that cannot be vectorized",
    "CantVectorizeInstructionReturnType": "instruction return type cannot be vectorized",
    "CantIdentifyArrayBounds": "cannot identify array bounds (possible aliasing, add __restrict)",
    "CantComputeNumberOfIterations": "trip count cannot be computed",
    "NonReductionValueUsedOutsideLoop": "a value computed in the loop is used after it (not a recognized reduction)",
    "UnsafeDep": "unsafe memory dependence between iterations",
    "UnsafeMemDep": "unsafe memory dependence between iterations",
    "CFGNotUnderstood": "control flow not understood (early exits / complex branches)",
    "NoInductionVariable": "no canonical induction variable",
    "UnsupportedUncountableLoop": "uncountable loop (early exit)",
    "VectorizationNotBeneficial": "cost model: vectorization not beneficial",
    "LoopContainsSwitch": "loop contains a switch",
    "NotInnerMostLoop": "not an innermost loop",
}

# Analysis remarks that do not explain a vectorization failure.
IGNORED_VECTORIZE_REASONS = frozenset({"InterleavingNotBeneficial"})


def classify(pass_name: str, name: str) -> Category:
    return _BY_PASS_AND_NAME.get((pass_name, name)) or _BY_PASS.get(pass_name, OTHER)


def weight(pass_name: str, name: str, callee: str = "") -> float:
    """Static weight of a remark. `callee` is the mangled callee of inline remarks."""
    w = _WEIGHT_BY_NAME.get(name, classify(pass_name, name).weight)
    if pass_name == "inline" and callee and not callee.startswith("_Z"):
        return min(w, _C_CALLEE_WEIGHT)
    return w
