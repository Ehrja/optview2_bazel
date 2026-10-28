#include "examples/kernels.h"

namespace examples {

void scale_down(std::vector<double>& v, const double& a) {
    for (auto& item : v) {
        item /= a;
    }
}

void scale_down_by_value(std::vector<double>& v, double a) {
    for (auto& item : v) {
        item /= a;
    }
}

double sum(const double* data, std::size_t n) {
    double acc = 0.0;
    for (std::size_t i = 0; i < n; ++i) {
        acc += data[i];
    }
    return acc;
}

void apply_gain_all(double* data, std::size_t n) {
    for (std::size_t i = 0; i < n; ++i) {
        data[i] = apply_gain(data[i]);
    }
}

void Histogram::accumulate(const unsigned char* samples, std::size_t n, unsigned char* trace) {
    for (std::size_t i = 0; i < n; ++i) {
        bins_[samples[i] & 0xF] += 1;
        trace[i] = static_cast<unsigned char>(count_);
        ++count_;
    }
}

}  // namespace examples
