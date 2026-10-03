#include "console.h"

#include <cstring>

CommandLine::Result CommandLine::feed(char c) {
  if (c == '\r') return Result::Pending;
  if (c == '\b' || c == 0x7f) {
    if (len_ > 0) --len_;
    return Result::Pending;
  }
  if (c == '\n') {
    buf_[len_] = '\0';
    const bool too_long = overflowed_;
    const bool empty = len_ == 0;
    len_ = 0;
    overflowed_ = false;
    if (too_long) return Result::Overflowed;
    return empty ? Result::Pending : Result::Complete;
  }
  if (len_ < kCapacity - 1) {
    buf_[len_++] = c;
  } else {
    overflowed_ = true;
  }
  return Result::Pending;
}

Command parse_command(const char* line) {
  if (std::strcmp(line, "CAPTURE") == 0) {
    return Command{CommandKind::Capture, line};
  }
  if (std::strcmp(line, "CAL?") == 0) {
    return Command{CommandKind::CalQuery, line};
  }
  if (std::strncmp(line, "CAL", 3) == 0 &&
      (line[3] == '\0' || line[3] == ' ' || line[3] == '\t')) {
    return Command{CommandKind::Cal, line + 3};
  }
  return Command{CommandKind::Unknown, line};
}
