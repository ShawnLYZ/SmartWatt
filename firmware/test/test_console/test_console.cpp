#include <unity.h>

#include <string>

#include "console.h"

void setUp() {}
void tearDown() {}

namespace {

/// Feeds every byte of `bytes`; returns the last non-Pending result, or
/// Pending if there was none. `completed` receives the line on Complete.
CommandLine::Result feed_all(CommandLine& line, const std::string& bytes,
                             std::string* completed = nullptr) {
  CommandLine::Result last = CommandLine::Result::Pending;
  for (const char c : bytes) {
    const CommandLine::Result result = line.feed(c);
    if (result == CommandLine::Result::Pending) continue;
    last = result;
    if (result == CommandLine::Result::Complete && completed != nullptr) {
      *completed = line.line();
    }
  }
  return last;
}

}  // namespace

static void test_a_line_completes_on_newline_without_its_carriage_return() {
  CommandLine line;
  std::string got;
  TEST_ASSERT_TRUE(feed_all(line, "CAPTURE\r\n", &got) ==
                   CommandLine::Result::Complete);
  TEST_ASSERT_EQUAL_STRING("CAPTURE", got.c_str());
}

static void test_a_bare_newline_is_not_a_command() {
  CommandLine line;
  TEST_ASSERT_TRUE(feed_all(line, "\r\n\n") == CommandLine::Result::Pending);
}

static void test_backspace_and_delete_erase() {
  CommandLine line;
  std::string got;
  feed_all(line, "CAX\bL?\n", &got);
  TEST_ASSERT_EQUAL_STRING("CAL?", got.c_str());

  feed_all(line, "\x7f\x7f" "CAPTURF\x7f" "E\n", &got);
  TEST_ASSERT_EQUAL_STRING("CAPTURE", got.c_str());
}

static void test_the_longest_line_that_fits_arrives_intact() {
  CommandLine line;
  std::string got;
  const std::string longest(CommandLine::kCapacity - 1, '7');
  TEST_ASSERT_TRUE(feed_all(line, longest + "\n", &got) ==
                   CommandLine::Result::Complete);
  TEST_ASSERT_EQUAL_STRING(longest.c_str(), got.c_str());
}

/// The reason the buffer grew: a CAL whose last digits do not fit must not
/// arrive as a shorter, still-valid number.
static void test_a_line_that_does_not_fit_is_discarded_whole() {
  CommandLine line;
  std::string got = "untouched";
  const std::string too_long =
      "CAL 0.2835 0.0040512 0.016201 0." + std::string(80, '1');
  TEST_ASSERT_TRUE(feed_all(line, too_long + "\n", &got) ==
                   CommandLine::Result::Overflowed);
  TEST_ASSERT_EQUAL_STRING("untouched", got.c_str());
}

static void test_backspace_cannot_rescue_an_overflowed_line() {
  CommandLine line;
  const std::string too_long(CommandLine::kCapacity + 5, 'A');
  TEST_ASSERT_TRUE(feed_all(line, too_long + "\b\b\b\b\b\b\b\b\n") ==
                   CommandLine::Result::Overflowed);
}

static void test_the_line_after_an_overflow_is_read_normally() {
  CommandLine line;
  std::string got;
  feed_all(line, std::string(200, 'A') + "\n");
  TEST_ASSERT_TRUE(feed_all(line, "CAL?\n", &got) ==
                   CommandLine::Result::Complete);
  TEST_ASSERT_EQUAL_STRING("CAL?", got.c_str());
}

static void test_commands_are_exact_words() {
  TEST_ASSERT_TRUE(parse_command("CAPTURE").kind == CommandKind::Capture);
  TEST_ASSERT_TRUE(parse_command("CAL?").kind == CommandKind::CalQuery);
  TEST_ASSERT_TRUE(parse_command("CAPTURE ").kind == CommandKind::Unknown);
  TEST_ASSERT_TRUE(parse_command("capture").kind == CommandKind::Unknown);
  TEST_ASSERT_TRUE(parse_command("CAL??").kind == CommandKind::Unknown);
  TEST_ASSERT_TRUE(parse_command("CALX 1 2 3 4").kind == CommandKind::Unknown);
  TEST_ASSERT_TRUE(parse_command("cal 1 2 3 4").kind == CommandKind::Unknown);
}

static void test_cal_hands_on_everything_after_the_word() {
  const Command with_args = parse_command("CAL 0.2835 0.004 0.016 0.05");
  TEST_ASSERT_TRUE(with_args.kind == CommandKind::Cal);
  TEST_ASSERT_EQUAL_STRING(" 0.2835 0.004 0.016 0.05", with_args.args);

  const Command tabbed = parse_command("CAL\t1 2 3 4");
  TEST_ASSERT_TRUE(tabbed.kind == CommandKind::Cal);
  TEST_ASSERT_EQUAL_STRING("\t1 2 3 4", tabbed.args);

  // Bare CAL is still a CAL, so the parser can say what it needs.
  const Command bare = parse_command("CAL");
  TEST_ASSERT_TRUE(bare.kind == CommandKind::Cal);
  TEST_ASSERT_EQUAL_STRING("", bare.args);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_a_line_completes_on_newline_without_its_carriage_return);
  RUN_TEST(test_a_bare_newline_is_not_a_command);
  RUN_TEST(test_backspace_and_delete_erase);
  RUN_TEST(test_the_longest_line_that_fits_arrives_intact);
  RUN_TEST(test_a_line_that_does_not_fit_is_discarded_whole);
  RUN_TEST(test_backspace_cannot_rescue_an_overflowed_line);
  RUN_TEST(test_the_line_after_an_overflow_is_read_normally);
  RUN_TEST(test_commands_are_exact_words);
  RUN_TEST(test_cal_hands_on_everything_after_the_word);
  return UNITY_END();
}
