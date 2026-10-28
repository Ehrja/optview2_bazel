#pragma once

#include <cstddef>
#include <vector>

namespace examples {

// (1) Aliasing: `a` may alias an element of `v`, so it is reloaded every iteration.
void scale_down(std::vector<double>& v, const double& a);

// Fixed variant: pass the scalar by value.
void scale_down_by_value(std::vector<double>& v, double a);

// (2) Floating point reduction: not vectorized without -ffast-math / reassoc.
double sum(const double* data, std::size_t n);

// (3) Cross-TU call in a hot loop: callee body is not visible -> not inlined.
double apply_gain(double x);  // defined in gain.cc
void apply_gain_all(double* data, std::size_t n);

// (4) Member state reloaded because of stores through a char-typed pointer.
class Histogram {
  public:
    void accumulate(const unsigned char* samples, std::size_t n, unsigned char* trace);
    std::size_t count() const { return count_; }

  private:
    std::size_t count_{0};
    std::size_t bins_[16]{};
};

}  // namespace examples
