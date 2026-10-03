#pragma once

#include <cstddef>
#include <vector>

#include "sample_source.h"

/// Reads a fixture CSV produced by `sim waveform`.
///
/// Header row is `v,i_low,i_high`; every subsequent row is one sample set
/// in ADC counts.
class FileSampleSource : public ISampleSource {
 public:
  explicit FileSampleSource(const char* path);

  bool next(SampleSet& out) override;

  bool ok() const { return ok_; }
  std::size_t size() const { return samples_.size(); }
  void rewind() { cursor_ = 0; }

 private:
  std::vector<SampleSet> samples_;
  std::size_t cursor_ = 0;
  bool ok_ = false;
};
