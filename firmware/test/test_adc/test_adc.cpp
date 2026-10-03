// Runs on BOTH environments.
//
// Under `native` the driver is a stub, and the tests below assert only the
// things that must hold regardless of hardware: that the type satisfies
// ISampleSource, and that the SPI command encoding for the MCP3208 is
// correct. The encoding is pure arithmetic and is worth testing on the
// laptop, because getting it wrong on the bench looks like a wiring fault.

#include <unity.h>

#include "adc_sample_source.h"
#include "sample_source.h"

void setUp() {}
void tearDown() {}

static void test_satisfies_the_seam() {
  AdcSampleSource source({10, 12, 13, 11});
  ISampleSource& seam = source;
  (void)seam;
  TEST_ASSERT_TRUE(true);
}

static void test_command_encoding_channel_0() {
  // MCP3208 24-clock frame: 5 leading zeros, START=1, SGL/DIFF=1, D2 D1 D0.
  uint8_t tx[3];
  encode_mcp3208_command(0, tx);
  TEST_ASSERT_EQUAL_HEX8(0x06, tx[0]);
  TEST_ASSERT_EQUAL_HEX8(0x00, tx[1]);
  TEST_ASSERT_EQUAL_HEX8(0x00, tx[2]);
}

static void test_command_encoding_channel_1() {
  uint8_t tx[3];
  encode_mcp3208_command(1, tx);
  TEST_ASSERT_EQUAL_HEX8(0x06, tx[0]);
  TEST_ASSERT_EQUAL_HEX8(0x40, tx[1]);
}

static void test_command_encoding_channel_2() {
  uint8_t tx[3];
  encode_mcp3208_command(2, tx);
  TEST_ASSERT_EQUAL_HEX8(0x06, tx[0]);
  TEST_ASSERT_EQUAL_HEX8(0x80, tx[1]);
}

static void test_command_encoding_channel_4_sets_d2() {
  uint8_t tx[3];
  encode_mcp3208_command(4, tx);
  TEST_ASSERT_EQUAL_HEX8(0x07, tx[0]);
  TEST_ASSERT_EQUAL_HEX8(0x00, tx[1]);
}

static void test_result_decoding() {
  // Twelve data bits: low nibble of rx[1], then all of rx[2].
  TEST_ASSERT_EQUAL_UINT16(0x0800, decode_mcp3208_result(0x08, 0x00));
  TEST_ASSERT_EQUAL_UINT16(0x0FFF, decode_mcp3208_result(0xFF, 0xFF));
  TEST_ASSERT_EQUAL_UINT16(2048, decode_mcp3208_result(0x08, 0x00));
}

static void test_result_ignores_the_high_nibble() {
  TEST_ASSERT_EQUAL_UINT16(decode_mcp3208_result(0x08, 0x00),
                           decode_mcp3208_result(0xF8, 0x00));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_satisfies_the_seam);
  RUN_TEST(test_command_encoding_channel_0);
  RUN_TEST(test_command_encoding_channel_1);
  RUN_TEST(test_command_encoding_channel_2);
  RUN_TEST(test_command_encoding_channel_4_sets_d2);
  RUN_TEST(test_result_decoding);
  RUN_TEST(test_result_ignores_the_high_nibble);
  return UNITY_END();
}
