#include <unity.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>

#include "file_sample_source.h"
#include "sample_source.h"

void setUp() {}
void tearDown() {}

static std::string fixture(const char* name) {
  return std::string(FIXTURE_DIR) + "/" + name + ".csv";
}

static void test_loads_a_fixture() {
  FileSampleSource source(fixture("pure-resistive").c_str());
  TEST_ASSERT_TRUE(source.ok());
  TEST_ASSERT_GREATER_THAN(0, (int)source.size());
}

static void test_eight_cycles_is_640_samples() {
  FileSampleSource source(fixture("pure-resistive").c_str());
  TEST_ASSERT_EQUAL_INT(640, (int)source.size());
}

static void test_next_returns_false_at_end() {
  FileSampleSource source(fixture("idle").c_str());
  SampleSet set{};
  size_t count = 0;
  while (source.next(set)) ++count;
  TEST_ASSERT_EQUAL_INT((int)source.size(), (int)count);
  TEST_ASSERT_FALSE(source.next(set));
}

static void test_samples_are_in_adc_range() {
  FileSampleSource source(fixture("mixed-three").c_str());
  SampleSet set{};
  while (source.next(set)) {
    TEST_ASSERT_LESS_OR_EQUAL_UINT16(4095, set.v);
    TEST_ASSERT_LESS_OR_EQUAL_UINT16(4095, set.i_low);
    TEST_ASSERT_LESS_OR_EQUAL_UINT16(4095, set.i_high);
  }
}

static void test_idle_current_sits_at_bias() {
  FileSampleSource source(fixture("idle").c_str());
  SampleSet set{};
  while (source.next(set)) {
    TEST_ASSERT_EQUAL_UINT16(2048, set.i_low);
  }
}

static void test_rewind_replays_from_the_start() {
  FileSampleSource source(fixture("pure-resistive").c_str());
  SampleSet first{}, again{};
  source.next(first);
  while (source.next(again)) {}
  source.rewind();
  source.next(again);
  TEST_ASSERT_EQUAL_UINT16(first.v, again.v);
  TEST_ASSERT_EQUAL_UINT16(first.i_low, again.i_low);
}

static void test_missing_file_is_not_ok_and_yields_nothing() {
  FileSampleSource source("/no/such/fixture.csv");
  SampleSet set{};
  TEST_ASSERT_FALSE(source.ok());
  TEST_ASSERT_FALSE(source.next(set));
}

static void test_polymorphic_through_the_interface() {
  // The whole point of Seam 2: no caller can tell the implementations apart.
  FileSampleSource concrete(fixture("pure-resistive").c_str());
  ISampleSource& seam = concrete;
  SampleSet set{};
  TEST_ASSERT_TRUE(seam.next(set));
}

static void test_skips_malformed_rows_and_remains_ok() {
  // Create a temporary CSV with a header, good row, malformed row, good row.
  // Verify the source reads the two good rows and remains ok().
  //
  // The path comes from std::filesystem, not a hardcoded "/tmp": on Windows
  // that resolves to C:\tmp, which exists on some machines and not others,
  // so the ofstream failed silently on a fresh checkout and took this test
  // -- and the suite's green claim -- with it.
  const std::string temp_path =
      (std::filesystem::temp_directory_path() / "test_malformed.csv").string();
  {
    std::ofstream out(temp_path);
    TEST_ASSERT_TRUE_MESSAGE(out.is_open(),
                             "could not create the temporary CSV");
    out << "v,i_low,i_high\n";
    out << "100,200,300\n";      // good row
    out << ",500,600\n";           // malformed: empty v field
    out << "700,800,900\n";        // good row
  }

  FileSampleSource source(temp_path.c_str());
  TEST_ASSERT_TRUE(source.ok());
  TEST_ASSERT_EQUAL_INT(2, (int)source.size());

  SampleSet set1{};
  TEST_ASSERT_TRUE(source.next(set1));
  TEST_ASSERT_EQUAL_UINT16(100, set1.v);
  TEST_ASSERT_EQUAL_UINT16(200, set1.i_low);
  TEST_ASSERT_EQUAL_UINT16(300, set1.i_high);

  SampleSet set2{};
  TEST_ASSERT_TRUE(source.next(set2));
  TEST_ASSERT_EQUAL_UINT16(700, set2.v);
  TEST_ASSERT_EQUAL_UINT16(800, set2.i_low);
  TEST_ASSERT_EQUAL_UINT16(900, set2.i_high);

  TEST_ASSERT_FALSE(source.next(set1));

  // Clean up
  std::remove(temp_path.c_str());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_loads_a_fixture);
  RUN_TEST(test_eight_cycles_is_640_samples);
  RUN_TEST(test_next_returns_false_at_end);
  RUN_TEST(test_samples_are_in_adc_range);
  RUN_TEST(test_idle_current_sits_at_bias);
  RUN_TEST(test_rewind_replays_from_the_start);
  RUN_TEST(test_missing_file_is_not_ok_and_yields_nothing);
  RUN_TEST(test_polymorphic_through_the_interface);
  RUN_TEST(test_skips_malformed_rows_and_remains_ok);
  return UNITY_END();
}
