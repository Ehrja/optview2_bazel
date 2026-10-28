#include "src/aliasing.h"

namespace e2e {

void scale_slow(Buffer& buf, const float& factor) {
    for (std::size_t i = 0; i < buf.size; ++i) {
        buf.data[i] *= factor;
    }
}

void scale_fast(Buffer& buf, float factor) {
    float* __restrict data = buf.data;
    const std::size_t n = buf.size;
    for (std::size_t i = 0; i < n; ++i) {
        data[i] *= factor;
    }
}

float dot(const float* a, const float* b, std::size_t n) {
    float acc = 0.0F;
    for (std::size_t i = 0; i < n; ++i) {
        acc += a[i] * b[i];
    }
    return acc;
}

}  // namespace e2e
