#pragma once

#include <cstddef>

namespace e2e {

struct Buffer {
    float* data;
    std::size_t size;
};

// Slow: `factor` is a reference that may alias `buf.data[i]`, `buf.size` is
// re-read every iteration because the store through `data` may modify it.
void scale_slow(Buffer& buf, const float& factor);

// Fast: scalars copied into locals, pointers marked __restrict.
void scale_fast(Buffer& buf, float factor);

float dot(const float* a, const float* b, std::size_t n);

}  // namespace e2e
