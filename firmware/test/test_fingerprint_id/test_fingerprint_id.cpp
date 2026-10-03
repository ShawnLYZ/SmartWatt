#include <unity.h>

#include <string>

#include "fingerprints.h"

/// The cross-language pin for fingerprint_id.
///
/// The server hashes fingerprints.csv itself (publish_fingerprints.py) and
/// the device echoes the id of the table it loaded in its telemetry. The
/// setup wizard (server/smartwatt_server/wizard.py) leaves its PUSH step for
/// VERIFY only when the device's NEWEST telemetry reports the id of the
/// table the server built -- the broker accepting the retained publish is
/// not that gate. The device has no `smartwatt/fingerprints` subscriber yet,
/// so today the table reaches it by `pio run -e esp32-s3 -t uploadfs` (the
/// wizard writes firmware/data/fingerprints.csv by default) and is loaded
/// from LittleFS at boot.
///
/// Two implementations of one hash that drift apart would hold that gate
/// shut forever -- or, worse, let a stale table look current. So both are
/// pinned to the SAME literal over the SAME committed file: this suite on
/// the C++ side, and firmware/tests_py/test_fingerprint_id.py on the Python
/// side. Change the fixture and both literals must move together.

void setUp() {}
void tearDown() {}

static std::string golden() {
  return std::string(FIXTURE_DIR) + "/fingerprints-golden.csv";
}

// Static: a FingerprintTable is ~43 KB. Harmless on the host, but the house
// rule is never to put one on a stack.
static FingerprintTable table;

static void test_golden_table_loads_every_row() {
  table.clear();
  // Named, not golden().c_str() on a temporary: FileFingerprintSource keeps
  // the pointer, and a temporary's buffer is freed before load() reads it.
  const std::string path = golden();
  FileFingerprintSource source(path.c_str());
  TEST_ASSERT_TRUE(source.load(table));
  TEST_ASSERT_EQUAL_INT(4, table.count);
  TEST_ASSERT_EQUAL_STRING("apple_charger-on-001", table.rows[0].training_id);
  TEST_ASSERT_EQUAL_STRING("phone_charger-off-001", table.rows[3].training_id);
}

static void test_golden_fingerprint_id_is_pinned() {
  table.clear();
  // Named, not golden().c_str() on a temporary: FileFingerprintSource keeps
  // the pointer, and a temporary's buffer is freed before load() reads it.
  const std::string path = golden();
  FileFingerprintSource source(path.c_str());
  TEST_ASSERT_TRUE(source.load(table));
  TEST_ASSERT_EQUAL_STRING("3853d83d", table.fingerprint_id);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_golden_table_loads_every_row);
  RUN_TEST(test_golden_fingerprint_id_is_pinned);
  return UNITY_END();
}
