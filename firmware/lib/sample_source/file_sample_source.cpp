#include "file_sample_source.h"

#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>

FileSampleSource::FileSampleSource(const char* path) {
  std::ifstream stream(path);
  if (!stream.is_open()) return;

  std::string line;
  if (!std::getline(stream, line)) return;  // header

  while (std::getline(stream, line)) {
    if (line.empty()) continue;
    std::istringstream row(line);
    std::string field;
    SampleSet set{};

    if (!std::getline(row, field, ',')) continue;
    // Skip rows with unparseable fields (stoi throws on non-numeric input
    // or overflow). Treat malformed data the same as missing/short rows.
    try {
      set.v = static_cast<uint16_t>(std::stoi(field));
    } catch (const std::exception&) {
      continue;
    }
    if (!std::getline(row, field, ',')) continue;
    try {
      set.i_low = static_cast<uint16_t>(std::stoi(field));
    } catch (const std::exception&) {
      continue;
    }
    if (!std::getline(row, field, ',')) continue;
    try {
      set.i_high = static_cast<uint16_t>(std::stoi(field));
    } catch (const std::exception&) {
      continue;
    }

    samples_.push_back(set);
  }

  ok_ = !samples_.empty();
}

bool FileSampleSource::next(SampleSet& out) {
  if (cursor_ >= samples_.size()) return false;
  out = samples_[cursor_++];
  return true;
}
