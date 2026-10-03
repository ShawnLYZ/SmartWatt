#include <string>

#include <unity.h>

#include "../fixtures/expected.h"

void setUp() {}
void tearDown() {}

static void test_toolchain_builds() { TEST_ASSERT_TRUE(true); }

static void test_fixtures_were_generated() {
  TEST_ASSERT_GREATER_THAN(0, EXPECTED_COUNT);
}

static void test_pure_resistive_fixture_is_present() {
  bool found = false;
  for (int i = 0; i < EXPECTED_COUNT; ++i) {
    if (std::string(EXPECTED[i].name) == "pure-resistive") found = true;
  }
  TEST_ASSERT_TRUE_MESSAGE(found, "run sim waveform to generate fixtures");
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_toolchain_builds);
  RUN_TEST(test_fixtures_were_generated);
  RUN_TEST(test_pure_resistive_fixture_is_present);
  return UNITY_END();
}
