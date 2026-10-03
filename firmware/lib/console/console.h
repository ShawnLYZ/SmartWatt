#pragma once

/// The bench's serial console, minus the Serial calls: what main.cpp does
/// with a byte and with a finished line, carved out so it runs under
/// `native` like periods_late() and clock_is_set().

/// Assembles serial bytes into command lines, one byte at a time -- across
/// as many loop() calls as the bytes take.
class CommandLine {
 public:
  /// Bytes held, including the terminating NUL. A CAL line carries four
  /// numbers; 16, which was enough for CAPTURE, is not.
  static constexpr int kCapacity = 96;

  enum class Result {
    /// Nothing to act on yet.
    Pending,
    /// A non-empty line just ended; line() holds it until the next feed().
    Complete,
    /// A line just ended that did not fit. It is DISCARDED WHOLE, never
    /// truncated: a CAL cut off mid-number parses as a different, still
    /// plausible number, which would go into NVS as a trim nobody typed.
    Overflowed,
  };

  /// '\r' is ignored and '\n' ends the line. Backspace and DEL erase, so a
  /// typo fixed in the serial monitor arrives fixed, not with the erase
  /// bytes still in it -- except once a line has overflowed, when the bytes
  /// that did not fit are already gone and nothing can repair it.
  Result feed(char c);

  const char* line() const { return buf_; }

 private:
  char buf_[kCapacity] = {};
  int len_ = 0;
  bool overflowed_ = false;
};

enum class CommandKind { Capture, CalQuery, Cal, Unknown };

struct Command {
  CommandKind kind;
  /// For Cal, everything after the word CAL (possibly empty), for
  /// parse_calibration_args(). Otherwise the whole line.
  const char* args;
};

/// Exact, case-sensitive words: a command that writes NVS is not one to
/// guess at. "CAL" must be followed by whitespace or nothing, so "CALX" is
/// unknown rather than a CAL with junk arguments.
Command parse_command(const char* line);
