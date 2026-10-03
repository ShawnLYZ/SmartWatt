#include <unity.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <utility>
#include <vector>

#include "fingerprints.h"
#include "knn.h"

void setUp() {}
void tearDown() {}

/// A table with three well-separated classes, four rows each.
static FingerprintTable separable() {
  FingerprintTable table;
  table.count = 0;
  std::strcpy(table.fingerprint_id, "abcd1234");
  for (int i = 0; i < kFeatureCount; ++i) table.active[i] = true;

  struct Seed {
    const char* label;
    double p;
    double q1;
  };
  const Seed seeds[] = {
      {"kettle", 1800.0, 30.0},
      {"desk_fan", 45.0, 33.0},
      {"incandescent_lamp", 40.0, 0.2},
  };

  for (const Seed& seed : seeds) {
    for (int n = 0; n < 4; ++n) {
      FingerprintRow& row = table.rows[table.count];
      std::snprintf(row.training_id, sizeof(row.training_id), "%s-on-%03d",
                    seed.label, n + 1);
      std::strncpy(row.label, seed.label, sizeof(row.label) - 1);
      row.label[sizeof(row.label) - 1] = '\0';
      row.features = FeatureVector{};
      row.features[kDeltaP] = seed.p + n * 0.5;
      row.features[kDeltaQ1] = seed.q1 + n * 0.2;
      row.features[kLogDeltaP] = std::log1p(seed.p);
      ++table.count;
    }
  }

  compute_normalisation(table);
  table.rejection_threshold = derive_threshold(table);
  return table;
}

static FeatureVector probe(double p, double q1) {
  FeatureVector f;
  f[kDeltaP] = p;
  f[kDeltaQ1] = q1;
  f[kLogDeltaP] = std::log1p(p);
  return f;
}

/// A probe in the two dimensions the hand-built fixtures further down vary,
/// with every other dimension left at zero.
///
/// This helper used to exist to dodge a bug: those fixtures carry no
/// log_delta_p, so their standard deviation for it is zero, and under the old
/// 1e-9 floor probe()'s log1p(p) was divided by that floor and drowned every
/// real difference under a distance of order 1e9. The floor is gone -- a
/// degenerate column now scales by 1.0 -- and the helper is kept for the
/// reason it should have had all along: those fixtures are ABOUT the two
/// dimensions they vary, and their arithmetic is quoted exactly in the
/// comments below. probe() would add log1p(p) as a constant offset in a
/// thirteenth dimension, identical for every row; that preserves the ranking
/// but changes every distance, and with it the inverse-distance weights the
/// contested-vote test derives by hand.
static FeatureVector plane_probe(double p, double q1) {
  FeatureVector f;
  f[kDeltaP] = p;
  f[kDeltaQ1] = q1;
  return f;
}

static void test_classifies_a_kettle() {
  const FingerprintTable table = separable();
  const Classification result = Knn(table).classify(probe(1801.0, 30.2));
  TEST_ASSERT_EQUAL_STRING("kettle", result.label);
  TEST_ASSERT_FALSE(result.rejected);
}

static void test_separates_the_fan_from_the_lamp() {
  // Same wattage, different fundamental reactive power. The project's
  // central discrimination claim.
  const FingerprintTable table = separable();
  const Knn knn(table);
  TEST_ASSERT_EQUAL_STRING("desk_fan", knn.classify(probe(45.4, 33.4)).label);
  TEST_ASSERT_EQUAL_STRING("incandescent_lamp",
                           knn.classify(probe(40.4, 0.5)).label);
}

static void test_returns_exactly_three_neighbours() {
  const Classification result = Knn(separable()).classify(probe(1801.0, 30.2));
  for (int n = 0; n < 3; ++n) {
    TEST_ASSERT_TRUE(std::strlen(result.neighbours[n].training_id) > 0);
  }
}

static void test_neighbours_are_sorted_by_distance() {
  const Classification result = Knn(separable()).classify(probe(1801.0, 30.2));
  // Strictly increasing, and the nearest strictly positive.
  //
  // The general contract allows ties, so a <= chain is what the ordering
  // claim needs -- but a <= chain is also satisfied by three zeroes, and
  // three zeroes is exactly what you get if the line that copies each
  // distance onto the Neighbour is dropped. Nothing else in this file reads
  // a distance, so US16's promise that `neighbours` "carries all three with
  // their distances" would ship unverified. This probe's three distances are
  // distinct -- 0.00068, 0.01351, 0.01356 -- so it can afford to be strict.
  TEST_ASSERT_TRUE(result.neighbours[0].distance > 0.0);
  TEST_ASSERT_TRUE(result.neighbours[0].distance < result.neighbours[1].distance);
  TEST_ASSERT_TRUE(result.neighbours[1].distance < result.neighbours[2].distance);
}

static void test_neighbours_name_real_training_events() {
  // US16: an evaluator must be able to check the classification rather than
  // take it on faith.
  const Classification result = Knn(separable()).classify(probe(1801.0, 30.2));
  TEST_ASSERT_TRUE(std::string(result.neighbours[0].training_id).find("kettle") !=
                   std::string::npos);
}

static void test_confidence_is_high_for_a_clean_match() {
  const Classification result = Knn(separable()).classify(probe(1800.5, 30.1));
  TEST_ASSERT_TRUE(result.confidence > 0.6);
}

static void test_confidence_is_bounded() {
  const Classification result = Knn(separable()).classify(probe(45.2, 33.1));
  TEST_ASSERT_TRUE(result.confidence >= 0.0 && result.confidence <= 1.0);
}

static void test_rejects_a_load_it_was_never_taught() {
  // The threshold is DERIVED from the training set, then validated against
  // an untaught load. It is never hardcoded from intuition.
  const FingerprintTable table = separable();
  FeatureVector alien = probe(305.0, 88.0);
  alien[kH3H1] = 0.18;
  alien[kDeltaCrest] = 1.98;
  const Classification result = Knn(table).classify(alien);
  TEST_ASSERT_TRUE(result.rejected);
}

static void test_a_rejected_classification_still_reports_neighbours() {
  const Classification result = Knn(separable()).classify(probe(9000.0, 4000.0));
  TEST_ASSERT_TRUE(result.rejected);
  TEST_ASSERT_TRUE(std::strlen(result.neighbours[0].training_id) > 0);
}

static void test_z_score_normalisation_uses_mean_and_stddev() {
  const FingerprintTable table = separable();
  // delta_p spans 40 to 1800, so its stddev must be large; delta_q1 spans
  // 0.2 to 33, so its stddev must be much smaller. Without normalisation
  // delta_p would dominate every distance.
  TEST_ASSERT_TRUE(table.stddev[kDeltaP] > table.stddev[kDeltaQ1] * 10.0);
}

static void test_the_metric_divides_by_the_training_standard_deviation() {
  // The spec asks for Euclidean over z-score normalised features, "using
  // training-set mean and standard deviation" -- explicitly NOT min-max.
  //
  // No behavioural assertion in this file can enforce that on its own:
  // derive_threshold() and classify() share whichever metric is in use, so
  // swapping the scale vector rescales the threshold by the same factor and
  // every accept/reject verdict survives unchanged. The test above only
  // compares two stored stddev fields, which a metric is free to ignore. So
  // pin the metric itself, by value.
  //
  // Rows 0 and 1 are kettle-on-001 and kettle-on-002. They differ by 0.5 W in
  // delta_p and 0.2 var in delta_q1, and not at all in log_delta_p. The
  // table's own standard deviations are 828.49614848564954 and
  // 14.807393048369077, so a z-score metric must return
  //     sqrt((0.5/828.4961...)^2 + (0.2/14.80739...)^2) = 0.013520242475502028
  // Raw Euclidean would return sqrt(0.5^2 + 0.2^2)      = 0.53851648071345048
  // Min-max would divide by the ranges, 1761.5 and 33.4 = 0.0059947477929703638
  // Three answers a factor of two to forty apart.
  const FingerprintTable table = separable();
  TEST_ASSERT_DOUBLE_WITHIN(1e-11, 828.49614848564954, table.stddev[kDeltaP]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-11, 14.807393048369077, table.stddev[kDeltaQ1]);
  TEST_ASSERT_DOUBLE_WITHIN(
      1e-15, 0.013520242475502028,
      normalised_distance(table.rows[0].features, table.rows[1].features, table));
}

/// Two features with deliberately mismatched shapes: delta_p is spread wide
/// and near-bimodal, delta_q1 is concentrated with a short tail. Their
/// standard deviations stand in a ratio of 2.157 while their RANGES stand in
/// a ratio of 1.429, and that gap between the two ratios is the whole point
/// of the fixture -- it is the only thing that can make a z-score metric and
/// a min-max metric disagree about which class is nearer.
static FingerprintTable mismatched_spread() {
  FingerprintTable table;
  table.count = 0;
  for (int i = 0; i < kFeatureCount; ++i) table.active[i] = true;

  struct Group {
    const char* label;
    int rows;
    double p;
    double q1;
  };
  const Group groups[] = {
      {"x_class", 3, 84.0, 50.0},  // 34 from the probe, along delta_p
      {"y_class", 3, 50.0, 70.0},  // 20 from the probe, along delta_q1
      {"ballast_hi", 17, 100.0, 0.0},  // shape the two spreads, and sit far
      {"ballast_lo", 17, 0.0, 0.0},    // enough out never to be counted
  };

  for (const Group& group : groups) {
    for (int n = 0; n < group.rows; ++n) {
      FingerprintRow& row = table.rows[table.count];
      std::snprintf(row.training_id, sizeof(row.training_id), "%s-%03d",
                    group.label, n + 1);
      std::snprintf(row.label, sizeof(row.label), "%s", group.label);
      row.features = FeatureVector{};
      row.features[kDeltaP] = group.p;
      row.features[kDeltaQ1] = group.q1;
      ++table.count;
    }
  }

  compute_normalisation(table);
  // Set by hand, and generously. Every group here sits at a single point, so
  // the within-class spread is zero and derive_threshold() would fall back to
  // half the smallest between-class separation -- a number this fixture has no
  // interest in. It is about which class WINS, not where the threshold falls.
  table.rejection_threshold = 10.0;
  return table;
}

static void test_normalisation_is_z_score_and_not_min_max() {
  const FingerprintTable z_score = mismatched_spread();
  TEST_ASSERT_DOUBLE_WITHIN(1e-11, 46.959530449100541, z_score.stddev[kDeltaP]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-11, 21.771541057077240, z_score.stddev[kDeltaQ1]);

  // The same table scaled by each feature's RANGE instead of its standard
  // deviation. normalised_distance divides by table.stddev and reads nothing
  // else, so overwriting that vector is precisely min-max normalisation with
  // no other change.
  FingerprintTable min_max = z_score;
  for (int i = 0; i < kFeatureCount; ++i) {
    double lowest = min_max.rows[0].features[i];
    double highest = lowest;
    for (int r = 0; r < min_max.count; ++r) {
      lowest = std::min(lowest, min_max.rows[r].features[i]);
      highest = std::max(highest, min_max.rows[r].features[i]);
    }
    if (highest > lowest) min_max.stddev[i] = highest - lowest;
  }
  TEST_ASSERT_DOUBLE_WITHIN(1e-11, 100.0, min_max.stddev[kDeltaP]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-11, 70.0, min_max.stddev[kDeltaQ1]);

  // The probe sits 34 along delta_p from x_class and 20 along delta_q1 from
  // y_class. That ratio, 1.70, falls between the range ratio (100/70 = 1.43)
  // and the standard-deviation ratio (46.96/21.77 = 2.16), which is exactly
  // the condition for the two normalisations to disagree:
  //     z-score:  34/46.96 = 0.724  beats  20/21.77 = 0.919  -> x_class
  //     min-max:  34/100   = 0.340  loses  20/70    = 0.286  -> y_class
  // The first assertion is the requirement. The second is what keeps the
  // fixture honest: if a later edit made the two metrics agree again, this
  // test would stop discriminating and would say so rather than pass quietly.
  const FeatureVector between = plane_probe(50.0, 50.0);
  TEST_ASSERT_EQUAL_STRING("x_class", Knn(z_score).classify(between).label);
  TEST_ASSERT_EQUAL_STRING("y_class", Knn(min_max).classify(between).label);
}

/// Three classes on one axis, placed so that a probe at 10.0 W lands 1.0, 1.1
/// and 1.2 W from its three nearest rows -- one row of one class, two of
/// another, so the vote actually has to resolve something.
static FingerprintTable a_contested_probe() {
  FingerprintTable table;
  table.count = 0;
  for (int i = 0; i < kFeatureCount; ++i) table.active[i] = true;

  struct Seed {
    const char* id;
    const char* label;
    double p;
  };
  const Seed seeds[] = {
      {"x_class-001", "x_class", 11.0},   // 1.0 from the probe: the nearest
      {"y_class-001", "y_class", 8.9},    // 1.1
      {"y_class-002", "y_class", 8.8},    // 1.2
      {"z_class-001", "z_class", 100.0},  // far enough never to be counted
      {"z_class-002", "z_class", 100.0},
      {"z_class-003", "z_class", 100.0},
  };

  for (const Seed& seed : seeds) {
    FingerprintRow& row = table.rows[table.count];
    std::snprintf(row.training_id, sizeof(row.training_id), "%s", seed.id);
    std::snprintf(row.label, sizeof(row.label), "%s", seed.label);
    row.features = FeatureVector{};
    row.features[kDeltaP] = seed.p;
    ++table.count;
  }

  compute_normalisation(table);
  // By hand and generous, for the same reason as mismatched_spread(): this
  // fixture is about the vote, not about where the threshold falls.
  table.rejection_threshold = 1.0;
  return table;
}

static void test_a_split_vote_can_outweigh_the_nearest_neighbour() {
  // Every other probe in this file draws a unanimous neighbour set. The vote
  // therefore never sees a second label, confidence comes out at exactly 1.0
  // every time, and an implementation that simply took the nearest
  // neighbour's label and reported confidence 1.0 would pass all of them --
  // including the two tests named for confidence.
  //
  // Here the three neighbours are x_class at 1.0 W, y_class at 1.1 W and
  // y_class at 1.2 W. The metric divides all three by the same standard
  // deviation, so it cancels out of the weight RATIOS and the confidence can
  // be derived from the watts alone:
  //     x = 1/1.0 = 1        y = 1/1.1 + 1/1.2 = 10/11 + 5/6 = 115/66
  //     total = 1 + 115/66 = 181/66
  //     winner's share = (115/66) / (181/66) = 115/181 = 0.63536
  // So the nearest neighbour LOSES the vote, two further neighbours outweigh
  // it, and the confidence lands strictly between 0 and 1.
  const FingerprintTable table = a_contested_probe();
  const Classification result = Knn(table).classify(plane_probe(10.0, 0.0));

  TEST_ASSERT_EQUAL_STRING("x_class", result.neighbours[0].label);
  TEST_ASSERT_EQUAL_STRING("y_class", result.neighbours[1].label);
  TEST_ASSERT_EQUAL_STRING("y_class", result.neighbours[2].label);

  TEST_ASSERT_FALSE(result.rejected);
  TEST_ASSERT_EQUAL_STRING("y_class", result.label);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 115.0 / 181.0, result.confidence);
  TEST_ASSERT_TRUE(result.confidence > 0.0 && result.confidence < 1.0);
}

/// Two loads a real table would hold, four captures each, with settle_cycles
/// held at 3 in every row.
///
/// THE CONSTANT COLUMN IS THE POINT, and it is not contrived: detector.cpp
/// sets settle_cycles to transient_cycles_, and DetectorConfig::settle_run
/// defaults to 3, so every capture whose run settles at the minimum records
/// the same 3. A correctly trained table really does contain that column.
///
/// The rest of the fixture is realistic on purpose too. The harmonic, crest
/// and inrush columns scatter capture to capture and barely separate the two
/// classes, which is what puts the within-class spread on the same scale as
/// the table-wide spread -- and therefore what makes the derived threshold
/// large enough for a one-cycle difference to sit inside it. separable() has
/// only three non-zero columns and derives a threshold of 0.030, which is
/// smaller than any single raw unit and could not show this either way.
static FingerprintTable a_constant_column() {
  FingerprintTable table;
  table.count = 0;
  for (int i = 0; i < kFeatureCount; ++i) table.active[i] = true;

  struct Capture {
    const char* label;
    double p;
    double q1;
    double crest;
    double h3;
    double h5;
    double h7;
    double inrush;
  };
  const Capture captures[] = {
      {"desk_fan", 45.0, 33.00, 0.21, 0.062, 0.031, 0.014, 1.9},
      {"desk_fan", 46.4, 34.10, 0.27, 0.048, 0.024, 0.019, 2.3},
      {"desk_fan", 44.1, 32.40, 0.16, 0.071, 0.038, 0.011, 1.6},
      {"desk_fan", 45.8, 33.60, 0.24, 0.055, 0.029, 0.016, 2.1},
      {"incandescent_lamp", 40.0, 0.20, 0.05, 0.014, 0.006, 0.003, 8.4},
      {"incandescent_lamp", 41.2, 0.31, 0.09, 0.021, 0.009, 0.005, 9.6},
      {"incandescent_lamp", 39.4, 0.14, 0.03, 0.009, 0.004, 0.002, 7.1},
      {"incandescent_lamp", 40.6, 0.25, 0.07, 0.017, 0.007, 0.004, 8.9},
  };

  for (const Capture& c : captures) {
    FingerprintRow& row = table.rows[table.count];
    std::snprintf(row.training_id, sizeof(row.training_id), "%s-on-%03d",
                  c.label, table.count + 1);
    std::snprintf(row.label, sizeof(row.label), "%s", c.label);
    row.features = FeatureVector{};
    row.features[kDeltaP] = c.p;
    row.features[kDeltaQ1] = c.q1;
    row.features[kDeltaCrest] = c.crest;
    row.features[kH3H1] = c.h3;
    row.features[kH5H1] = c.h5;
    row.features[kH7H1] = c.h7;
    row.features[kInrushRatio] = c.inrush;
    row.features[kSettleCycles] = 3.0;  // the constant column
    row.features[kLogDeltaP] = std::log1p(c.p);
    ++table.count;
  }

  compute_normalisation(table);
  table.rejection_threshold = derive_threshold(table);
  return table;
}

/// A zero-variance feature scales by 1.0, not by an epsilon.
///
/// This replaces an assertion that only checked `!isnan(confidence)`, which
/// passes under ANY divisor and so could not see the defect at all: the old
/// metric divided by a floor of 1e-9, so an event differing from a training
/// row by one settle cycle scored a distance of exactly 1e9 and was rejected,
/// while every training row scored 0 in that column and left the derived
/// threshold small. The guard against "a zero-variance feature collapsing the
/// distance metric" rejected every real event instead.
static void test_a_zero_variance_feature_scales_by_one_not_by_epsilon() {
  const FingerprintTable table = a_constant_column();
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.0, table.stddev[kSettleCycles]);

  // The metric itself, by value. Two vectors differing by exactly one unit in
  // the degenerate column and in nothing else are exactly 1.0 apart: the raw
  // difference, scaled by 1.0. Under the floor this was 1e9, and under a
  // "skip degenerate columns" rule it would be 0.0 -- three answers nine
  // orders of magnitude apart, so this pins the choice and not merely its
  // sign.
  FeatureVector here = table.rows[0].features;
  FeatureVector one_cycle_longer = here;
  one_cycle_longer[kSettleCycles] = 4.0;
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 1.0,
                            normalised_distance(here, one_cycle_longer, table));

  // And the behaviour that follows from it. The threshold this table derives
  // is 2.1015740392390050; an exact training row sits 0.29604358942207160
  // from its class centroid and the same event with settle_cycles one higher
  // sits 1.0429006696890668 -- both inside it. Under the floor the second was
  // 1e9 and came back rejected with an empty label.
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 2.1015740392390050,
                            table.rejection_threshold);

  const Classification on_the_row = Knn(table).classify(here);
  TEST_ASSERT_EQUAL_STRING("desk_fan", on_the_row.label);
  TEST_ASSERT_FALSE(on_the_row.rejected);

  const Classification shifted = Knn(table).classify(one_cycle_longer);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, shifted.neighbours[0].distance);
  TEST_ASSERT_EQUAL_STRING("desk_fan", shifted.label);
  TEST_ASSERT_FALSE(shifted.rejected);
  TEST_ASSERT_FALSE(std::isnan(shifted.confidence));
}

/// One class, twenty rows: eighteen sitting together at 1000 W, one a little
/// wide at 1010 W, and one a long way out at 1200 W.
///
/// Twenty rows is the point of it. `ceil(0.95n) - 1` equals `n - 1` for every
/// n up to 19, so across the twelve rows of separable() the 95th percentile
/// and the maximum are the same index and no test can tell the percentile
/// from `distances.back()`. At twenty they are index 18 and index 19, and the
/// outlier drives them a factor of eighteen apart. Real S8 tables run to
/// about a hundred rows, so twenty is the shape that ships, not twelve.
static FingerprintTable one_class_with_an_outlier() {
  FingerprintTable table;
  table.count = 0;
  for (int i = 0; i < kFeatureCount; ++i) table.active[i] = true;

  for (int n = 0; n < 20; ++n) {
    FingerprintRow& row = table.rows[table.count];
    std::snprintf(row.training_id, sizeof(row.training_id),
                  "space_heater-on-%03d", n + 1);
    std::snprintf(row.label, sizeof(row.label), "%s", "space_heater");
    row.features = FeatureVector{};
    row.features[kDeltaP] = (n < 18) ? 1000.0 : (n == 18 ? 1010.0 : 1200.0);
    ++table.count;
  }

  compute_normalisation(table);
  table.rejection_threshold = derive_threshold(table);
  return table;
}

static void test_rejection_measures_from_the_winning_class_centroid() {
  // The spec is explicit: "An event whose distance to the winning class
  // centroid exceeds it is emitted with rejected: true". Nearest-neighbour
  // distance is the tempting substitute, and on every other probe in this
  // file the two agree, so nothing here would notice the swap.
  //
  // These two probes make it impossible to hide. BOTH sit exactly on top of a
  // training row, so both have a nearest-neighbour distance of exactly zero
  // and any nearest-neighbour rule must accept both. Their distances to the
  // class centroid at 1010.5 W are 0.2412 and 4.3534 against a threshold of
  // 0.3618, so the centroid rule accepts one and rejects the other.
  const FingerprintTable table = one_class_with_an_outlier();

  const Classification on_the_cluster =
      Knn(table).classify(plane_probe(1000.0, 0.0));
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.0, on_the_cluster.neighbours[0].distance);
  TEST_ASSERT_FALSE(on_the_cluster.rejected);
  TEST_ASSERT_EQUAL_STRING("space_heater", on_the_cluster.label);

  const Classification on_the_outlier =
      Knn(table).classify(plane_probe(1200.0, 0.0));
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.0, on_the_outlier.neighbours[0].distance);
  TEST_ASSERT_TRUE(on_the_outlier.rejected);
  TEST_ASSERT_EQUAL_STRING("", on_the_outlier.label);
}

static void test_the_threshold_is_a_percentile_not_the_maximum() {
  const FingerprintTable table = one_class_with_an_outlier();
  TEST_ASSERT_EQUAL_INT(20, table.count);

  // Every row's distance to the single class centroid, which sits at
  // 1010.5 W. The largest is the outlier's.
  FeatureVector centroid;
  centroid[kDeltaP] = 1010.5;
  double largest = 0.0;
  for (int r = 0; r < table.count; ++r) {
    largest = std::max(
        largest, normalised_distance(table.rows[r].features, centroid, table));
  }

  // The 95th percentile lands on the cluster's 0.2412200155935344, not on the
  // outlier's 4.3534469480928353. Times 1.5 that is 0.3618 rather than 6.5302
  // -- and at 6.5302 the outlier probe in the test above would be accepted,
  // which is the failure this indexing exists to prevent.
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 4.3534469480928353, largest);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.36183002339030157,
                            table.rejection_threshold);
  TEST_ASSERT_TRUE(table.rejection_threshold < largest * 1.5);
}

static void test_threshold_is_derived_not_hardcoded() {
  const FingerprintTable table = separable();
  TEST_ASSERT_TRUE(table.rejection_threshold > 0.0);

  // The threshold tracks the training data rather than being a constant, so
  // a table whose classes sit TIGHTER around their own centroids must derive
  // a SMALLER threshold. Collapsing every row's delta_q1 onto its class
  // centroid shrinks the within-class spread and nothing else.
  //
  // Merely scaling delta_p -- the obvious perturbation -- proves nothing: it
  // scales the rows and the standard deviation by the same factor, so the
  // normalised within-class distances, and the threshold with them, come out
  // bit-identical.
  FingerprintTable tighter = separable();
  for (int n = 0; n < tighter.count; ++n) {
    double sum = 0.0;
    int members = 0;
    for (int r = 0; r < table.count; ++r) {  // read the UNMUTATED rows
      if (std::strcmp(table.rows[r].label, tighter.rows[n].label) == 0) {
        sum += table.rows[r].features[kDeltaQ1];
        ++members;
      }
    }
    tighter.rows[n].features[kDeltaQ1] = sum / static_cast<double>(members);
  }
  compute_normalisation(tighter);

  const double narrower = derive_threshold(tighter);
  TEST_ASSERT_TRUE(narrower > 0.0);
  TEST_ASSERT_TRUE(narrower < table.rejection_threshold);
}

static void test_inactive_features_are_excluded_from_the_distance() {
  // S8 prunes to whichever dimensions earn their place by measured
  // permutation importance; the classifier must honour that.
  FingerprintTable masked_table = separable();
  for (int i = 0; i < kFeatureCount; ++i) {
    masked_table.active[i] = (i == kDeltaP);
  }
  const Knn masked(masked_table);
  // Identical delta_p, wildly different delta_q1. With delta_q1 excluded the
  // two probes are literally the same point, so they cannot come out apart.
  TEST_ASSERT_EQUAL_STRING(masked.classify(probe(45.0, 33.0)).label,
                           masked.classify(probe(45.0, 0.2)).label);

  // And it is the mask that did that, not the probes: with every dimension
  // active the same two points do NOT agree.
  const FingerprintTable full_table = separable();
  const Knn full(full_table);
  TEST_ASSERT_TRUE(std::strcmp(full.classify(probe(45.0, 33.0)).label,
                               full.classify(probe(45.0, 0.2)).label) != 0);
}

static void test_empty_table_rejects_everything() {
  FingerprintTable table;
  table.count = 0;
  table.rejection_threshold = 1.0;
  for (int i = 0; i < kFeatureCount; ++i) table.active[i] = true;
  TEST_ASSERT_TRUE(Knn(table).classify(probe(1800.0, 30.0)).rejected);
}

static void test_clear_restores_a_freshly_constructed_table() {
  FingerprintTable table = separable();
  table.rejection_threshold = 2.5;
  table.clear();

  const FingerprintTable fresh{};
  TEST_ASSERT_EQUAL_INT(fresh.count, table.count);
  TEST_ASSERT_EQUAL_DOUBLE(fresh.rejection_threshold,
                           table.rejection_threshold);
  TEST_ASSERT_EQUAL_MEMORY(fresh.fingerprint_id, table.fingerprint_id,
                           sizeof(table.fingerprint_id));
  TEST_ASSERT_EQUAL_MEMORY(fresh.active, table.active, sizeof(table.active));
  TEST_ASSERT_EQUAL_MEMORY(fresh.mean.v, table.mean.v, sizeof(table.mean.v));
  TEST_ASSERT_EQUAL_MEMORY(fresh.stddev.v, table.stddev.v,
                           sizeof(table.stddev.v));
  for (int r = 0; r < FingerprintTable::kMaxRows; ++r) {
    TEST_ASSERT_EQUAL_MEMORY(&fresh.rows[r], &table.rows[r],
                             sizeof(FingerprintRow));
  }
}

static void test_a_knn_bound_before_clear_sees_the_empty_table() {
  // main.cpp binds g_knn to g_fingerprints once, then clears the table when
  // the file is unusable. The classifier must see the cleared table, not
  // the rows that were there before.
  FingerprintTable table = separable();
  const Knn knn(table);
  TEST_ASSERT_FALSE(knn.classify(probe(1800.0, 30.0)).rejected);

  table.clear();
  TEST_ASSERT_TRUE(knn.classify(probe(1800.0, 30.0)).rejected);
}

static void test_memory_source_round_trips() {
  const FingerprintTable original = separable();
  MemoryFingerprintSource source(original);
  FingerprintTable loaded;
  TEST_ASSERT_TRUE(source.load(loaded));
  TEST_ASSERT_EQUAL_INT(original.count, loaded.count);
  TEST_ASSERT_EQUAL_STRING(original.fingerprint_id, loaded.fingerprint_id);
}

/// One line of the real 24-column fingerprints.csv from the S8 spec, with
/// delta_q1's cell supplied by the caller so a test can put a typo in it.
static std::string csv_row(const char* training_id, const char* label,
                           const char* edge, const std::string& delta_q1_cell) {
  std::string line = std::string(training_id) + ',' + label + ',' + edge;
  for (int i = 0; i < kFeatureCount; ++i) {
    line += ',';
    if (i == kDeltaQ1) {
      line += delta_q1_cell;
    } else if (i == kDeltaP) {
      line += "1800";
    } else {
      line += "0";
    }
  }
  return line + ",2026-08-13T10:04:11Z,sess-01,12.5,,239.8,50.01,quiet circuit";
}

/// Writes `table` out in the real 24-column fingerprints.csv layout from the
/// S8 spec -- training_id, label, edge, the fourteen features in
/// kFeatureNames order, then the seven capture-condition columns -- so what
/// gets tested is a loader that tolerates the file the project actually
/// produces, rather than a stripped-down invention of this test's own.
static void write_fingerprints_csv(const FingerprintTable& table,
                                   const std::string& path) {
  std::ofstream out(path);
  TEST_ASSERT_TRUE_MESSAGE(out.is_open(), "could not create the temporary CSV");

  out << "training_id,label,edge";
  for (int i = 0; i < kFeatureCount; ++i) out << ',' << kFeatureNames[i];
  out << ",ts,session_id,background_w,concurrent_ids,vrms_mean,freq_mean,notes\n";

  for (int r = 0; r < table.count; ++r) {
    // Both edges present in the file, and both discarded on the way in: the
    // classifier is structurally incapable of seeing edge direction.
    out << table.rows[r].training_id << ',' << table.rows[r].label << ','
        << (r % 2 == 0 ? "on" : "off");
    for (int i = 0; i < kFeatureCount; ++i) {
      char cell[40];
      std::snprintf(cell, sizeof(cell), "%.17g", table.rows[r].features[i]);
      out << ',' << cell;
    }
    out << ",2026-08-13T10:04:11Z,sess-01,12.5,,239.8,50.01,quiet circuit\n";
  }
}

static void test_classifier_is_identical_through_any_source() {
  // S8 adds a transport, not a redesign: the classifier never learns where
  // its table came from.
  const FingerprintTable table = separable();

  MemoryFingerprintSource memory(table);
  FingerprintTable from_memory;
  TEST_ASSERT_TRUE(memory.load(from_memory));

  // The path comes from std::filesystem, not a hardcoded "/tmp": on Windows
  // that resolves to C:\tmp, which exists on some machines and not others,
  // so the ofstream fails silently on a fresh checkout.
  const std::string path =
      (std::filesystem::temp_directory_path() / "test_fingerprints.csv").string();
  write_fingerprints_csv(table, path);

  FileFingerprintSource file(path.c_str());
  IFingerprintSource& seam = file;  // no caller can tell the sources apart
  FingerprintTable from_file;
  TEST_ASSERT_TRUE(seam.load(from_file));
  TEST_ASSERT_EQUAL_INT(table.count, from_file.count);

  const FeatureVector kettle = probe(1801.0, 30.2);
  TEST_ASSERT_EQUAL_STRING(Knn(from_memory).classify(kettle).label,
                           Knn(from_file).classify(kettle).label);
  TEST_ASSERT_EQUAL_STRING("kettle", Knn(from_file).classify(kettle).label);

  // The fan/lamp pair too, which is where a wrong column offset would show.
  const FeatureVector fan = probe(45.4, 33.4);
  TEST_ASSERT_EQUAL_STRING(Knn(from_memory).classify(fan).label,
                           Knn(from_file).classify(fan).label);
  TEST_ASSERT_EQUAL_STRING("desk_fan", Knn(from_file).classify(fan).label);

  std::remove(path.c_str());
}

static void test_a_malformed_row_costs_one_row_not_the_table() {
  // US58's whole premise is a plain file that gets inspected and corrected by
  // hand, so one typo must cost one row rather than the whole table. That is
  // the contract file_sample_source.cpp already holds, and
  // test_skips_malformed_rows_and_remains_ok is its counterpart there.
  //
  // Before this, a single unparseable cell threw std::invalid_argument
  // straight out of load(). On the S6 target, where the Arduino/ESP-IDF
  // default is -fno-exceptions, it would have aborted instead -- one typo
  // turning into a reboot loop.
  const std::string path =
      (std::filesystem::temp_directory_path() / "test_fingerprints_bad.csv")
          .string();
  {
    std::ofstream out(path);
    TEST_ASSERT_TRUE_MESSAGE(out.is_open(),
                             "could not create the temporary CSV");
    out << "training_id,label,edge";
    for (int i = 0; i < kFeatureCount; ++i) out << ',' << kFeatureNames[i];
    out << ",ts,session_id,background_w,concurrent_ids,vrms_mean,freq_mean,notes\n";
    out << csv_row("good-001", "kettle", "on", "30") << '\n';
    out << csv_row("typo-002", "kettle", "on", "thirty") << '\n';
    out << csv_row("blank-003", "kettle", "on", "") << '\n';
    out << csv_row("huge-004", "kettle", "on", "1e999") << '\n';
    out << "short-005,kettle,on,1800,30\n";
    out << csv_row("good-006", "kettle", "off", "30.4") << '\n';
  }

  FileFingerprintSource source(path.c_str());
  FingerprintTable table;
  TEST_ASSERT_TRUE(source.load(table));

  // Four bad rows dropped, two good rows kept, in file order.
  TEST_ASSERT_EQUAL_INT(2, table.count);
  TEST_ASSERT_EQUAL_STRING("good-001", table.rows[0].training_id);
  TEST_ASSERT_EQUAL_STRING("good-006", table.rows[1].training_id);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 30.0, table.rows[0].features[kDeltaQ1]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 30.4, table.rows[1].features[kDeltaQ1]);

  std::remove(path.c_str());
}

/// Writes one data line per (training_id, label) pair, in the real 24-column
/// layout, with a delta_p that differs row to row so the table has a genuine
/// within-class spread.
static void write_labelled_csv(
    const std::string& path,
    const std::vector<std::pair<std::string, std::string>>& rows) {
  std::ofstream out(path);
  TEST_ASSERT_TRUE_MESSAGE(out.is_open(), "could not create the temporary CSV");
  out << "training_id,label,edge";
  for (int i = 0; i < kFeatureCount; ++i) out << ',' << kFeatureNames[i];
  out << ",ts,session_id,background_w,concurrent_ids,vrms_mean,freq_mean,notes\n";
  int n = 0;
  for (const auto& row : rows) {
    out << row.first << ',' << row.second << ",on";
    for (int i = 0; i < kFeatureCount; ++i) {
      out << ',' << (i == kDeltaP ? 100.0 + n : 0.0);
    }
    out << ",2026-08-13T10:04:11Z,sess-01,12.5,,239.8,50.01,quiet circuit\n";
    ++n;
  }
}

/// The predicate, at every boundary of the contract's id pattern.
///
/// `^([a-z][a-z0-9_]*|unknown_[0-9]+)$` is what telemetry.schema.json requires
/// of `attribution.active[].id`, and a trained label reaches that field
/// unchanged. The SECOND arm is refused here even though the schema admits it:
/// it is the shape Tracker generates, and tracker.cpp uses the shape itself to
/// decide which active entries an OFF may retire by delta-pairing.
static void test_is_emittable_label_matches_the_contract_pattern() {
  for (const char* good : {"kettle", "desk_fan", "incandescent_lamp", "tv2",
                           "a", "unknown_device", "unknown_", "unknown_1a",
                           "washing_machine_2"}) {
    TEST_ASSERT_TRUE_MESSAGE(is_emittable_label(good), good);
  }
  for (const char* bad : {"", "Desk Fan", "Kettle", "desk fan", "desk-fan",
                          "2kettle", "_kettle", "kettle!", "kettle\"s",
                          "unknown_1", "unknown_5", "unknown_42",
                          "unknown_0000"}) {
    TEST_ASSERT_FALSE_MESSAGE(is_emittable_label(bad), bad);
  }
  TEST_ASSERT_FALSE(is_emittable_label(nullptr));
}

/// A label the contract's id pattern rejects costs one row, not the ledger.
///
/// Unfixed, `Desk Fan` loaded, classified confidently, was attributed, and
/// reached the wire as `{"id": "Desk Fan", ...}`. ingest.py validates the whole
/// frame before reading any of it, so the server drops every 1 Hz telemetry
/// frame for as long as that appliance is on -- taking every OTHER appliance's
/// watts and the residual out of the energy ledger with it, while the event
/// stream (whose `label` is unpatterned) keeps flowing and the device looks
/// healthy.
static void test_a_label_the_contract_rejects_costs_one_row_not_the_table() {
  const std::string path =
      (std::filesystem::temp_directory_path() / "test_fingerprint_labels.csv")
          .string();
  write_labelled_csv(path, {{"good-001", "kettle"},
                            {"spaced-002", "Desk Fan"},
                            {"empty-003", ""},
                            {"punct-004", "kettle!"},
                            {"digit-005", "2kettle"},
                            {"good-006", "desk_fan"},
                            // 24 characters: one too many for label[24], so
                            // snprintf would have stored a DIFFERENT id from
                            // the one in the file.
                            {"long-007", "aaaaaaaaaaaaaaaaaaaaaaaa"},
                            // 23 characters plus the NUL: the last that fits.
                            {"fits-008", "aaaaaaaaaaaaaaaaaaaaaaa"}});

  FileFingerprintSource source(path.c_str());
  FingerprintTable table;
  TEST_ASSERT_TRUE(source.load(table));

  TEST_ASSERT_EQUAL_INT(3, table.count);
  TEST_ASSERT_EQUAL_STRING("good-001", table.rows[0].training_id);
  TEST_ASSERT_EQUAL_STRING("good-006", table.rows[1].training_id);
  TEST_ASSERT_EQUAL_STRING("fits-008", table.rows[2].training_id);
  for (int r = 0; r < table.count; ++r) {
    TEST_ASSERT_TRUE_MESSAGE(is_emittable_label(table.rows[r].label),
                             table.rows[r].label);
  }
  std::remove(path.c_str());
}

/// `unknown_5` matches the contract's id pattern and must STILL be refused.
///
/// is_unknown_id() in tracker.cpp deliberately admits `unknown_device` as a
/// name while treating `unknown_<digits>` as an id the tracker generated -- and
/// nothing can tell a TRAINED `unknown_5` from a generated one, by
/// construction. Loaded, a named, confidently-classified `unknown_5` was
/// retired from the active set by an unrelated REJECTED off edge that merely
/// resembled it in size, through nearest_unknown()'s delta-pairing.
static void test_a_generated_unknown_id_is_not_an_admissible_label() {
  const std::string path =
      (std::filesystem::temp_directory_path() / "test_fingerprint_unknown.csv")
          .string();
  write_labelled_csv(path, {{"gen-001", "unknown_5"},
                            {"gen-002", "unknown_5"},
                            {"name-003", "unknown_device"},
                            {"name-004", "unknown_device"}});

  FileFingerprintSource source(path.c_str());
  FingerprintTable table;
  TEST_ASSERT_TRUE(source.load(table));

  TEST_ASSERT_EQUAL_INT(2, table.count);
  TEST_ASSERT_EQUAL_STRING("unknown_device", table.rows[0].label);
  TEST_ASSERT_EQUAL_STRING("unknown_device", table.rows[1].label);
  std::remove(path.c_str());
}

/// A degenerate table must classify or fail, never silently reject everything.
static void test_one_row_per_class_still_derives_a_usable_threshold() {
  // US58's bringup case: hand-write two rows and see it work. Every row is its
  // own class centroid, so every within-class distance is zero and the
  // percentile is zero -- which used to be returned as the threshold, and
  // rejected a probe one watt from a training row. It also fails
  // fingerprints.schema.json's `exclusiveMinimum: 0`.
  FingerprintTable table;
  table.count = 0;
  for (int i = 0; i < kFeatureCount; ++i) table.active[i] = true;
  const char* const labels[] = {"kettle", "desk_fan"};
  const double watts[] = {1800.0, 45.0};
  for (int n = 0; n < 2; ++n) {
    FingerprintRow& row = table.rows[table.count];
    std::snprintf(row.training_id, sizeof(row.training_id), "%s-001",
                  labels[n]);
    std::snprintf(row.label, sizeof(row.label), "%s", labels[n]);
    row.features = FeatureVector{};
    row.features[kDeltaP] = watts[n];
    ++table.count;
  }
  compute_normalisation(table);
  table.rejection_threshold = derive_threshold(table);

  // Only delta_p varies, over 1800 and 45: mean 922.5, population stddev
  // 877.5 exactly. The two centroids ARE the two rows, so they stand
  // 1755 / 877.5 = 2.0 apart, and half of that is exactly 1.0.
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 877.5, table.stddev[kDeltaP]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, table.rejection_threshold);
  TEST_ASSERT_TRUE(table.rejection_threshold > 0.0);

  // A probe one watt from a training row is 1/877.5 from its centroid.
  const Classification near = Knn(table).classify(plane_probe(1801.0, 0.0));
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 1.0 / 877.5, near.neighbours[0].distance);
  TEST_ASSERT_EQUAL_STRING("kettle", near.label);
  TEST_ASSERT_FALSE(near.rejected);
}

/// ...and a table that cannot derive one at all says so, rather than loading.
static void test_a_table_that_can_derive_no_threshold_fails_to_load() {
  // One class, one row: no within-class spread AND no second centroid to
  // measure against. Nothing this table could ever accept, so presenting it as
  // loaded is the silent reject-everything in its purest form.
  const std::string path =
      (std::filesystem::temp_directory_path() / "test_fingerprint_single.csv")
          .string();
  write_labelled_csv(path, {{"only-001", "kettle"}});

  FileFingerprintSource source(path.c_str());
  FingerprintTable table;
  TEST_ASSERT_FALSE(source.load(table));
}

/// More data lines than kMaxRows is a failure, not a smaller table.
static void test_a_file_larger_than_the_table_is_refused_not_truncated() {
  // Loading 256 of 300 rows and returning true drops rows 256-299 with no
  // diagnostic anywhere -- and those are the rows most recently APPENDED, i.e.
  // exactly the appliance somebody has just trained and is about to test.
  const std::string path =
      (std::filesystem::temp_directory_path() / "test_fingerprint_full.csv")
          .string();
  std::vector<std::pair<std::string, std::string>> rows;
  for (int r = 0; r < FingerprintTable::kMaxRows; ++r) {
    rows.emplace_back("row-" + std::to_string(r), "kettle");
  }

  // Exactly full still loads.
  write_labelled_csv(path, rows);
  FingerprintTable exact;
  TEST_ASSERT_TRUE(FileFingerprintSource(path.c_str()).load(exact));
  TEST_ASSERT_EQUAL_INT(FingerprintTable::kMaxRows, exact.count);

  // One more does not.
  rows.emplace_back("row-overflow", "kettle");
  write_labelled_csv(path, rows);
  FingerprintTable over;
  TEST_ASSERT_FALSE(FileFingerprintSource(path.c_str()).load(over));
  std::remove(path.c_str());
}

/// A short table publishes how many neighbours it actually found.
///
/// Without the count, the trailing slots are default-constructed -- empty
/// strings and a distance of 0.0, which is the value of a PERFECT match -- and
/// every consumer has to infer emptiness from a different field. The tracker
/// inferred it from the label and the emitter did not, so the two disagreed
/// about the same slots.
static void test_a_short_table_reports_how_many_neighbours_it_filled() {
  FingerprintTable table;
  table.count = 0;
  for (int i = 0; i < kFeatureCount; ++i) table.active[i] = true;
  const char* const labels[] = {"kettle", "desk_fan"};
  const double watts[] = {1800.0, 45.0};
  for (int n = 0; n < 2; ++n) {
    FingerprintRow& row = table.rows[table.count];
    std::snprintf(row.training_id, sizeof(row.training_id), "%s-001",
                  labels[n]);
    std::snprintf(row.label, sizeof(row.label), "%s", labels[n]);
    row.features = FeatureVector{};
    row.features[kDeltaP] = watts[n];
    ++table.count;
  }
  compute_normalisation(table);
  table.rejection_threshold = derive_threshold(table);

  const Classification result = Knn(table).classify(plane_probe(1800.0, 0.0));
  TEST_ASSERT_EQUAL_INT(2, result.neighbour_count);
  TEST_ASSERT_EQUAL_STRING("kettle", result.neighbours[0].label);
  TEST_ASSERT_EQUAL_STRING("desk_fan", result.neighbours[1].label);
  // The slot nobody filled, still holding the perfect-match default that made
  // the count necessary in the first place.
  TEST_ASSERT_EQUAL_STRING("", result.neighbours[2].label);
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.0, result.neighbours[2].distance);

  // A full table fills them all, and an empty one fills none.
  TEST_ASSERT_EQUAL_INT(
      3, Knn(separable()).classify(probe(1801.0, 30.2)).neighbour_count);
  FingerprintTable empty;
  empty.count = 0;
  empty.rejection_threshold = 1.0;
  for (int i = 0; i < kFeatureCount; ++i) empty.active[i] = true;
  TEST_ASSERT_EQUAL_INT(
      0, Knn(empty).classify(probe(1800.0, 30.0)).neighbour_count);
}

static void test_fingerprint_id_reflects_content() {
  // A stale table on the device has to be visible AS a stale table, rather
  // than presenting as "the wizard says trained but classification is wrong"
  // with no way to tell whether the push landed.
  FingerprintTable table = separable();
  compute_fingerprint_id(table);
  char before[9];
  std::strcpy(before, table.fingerprint_id);
  TEST_ASSERT_EQUAL_INT(8, static_cast<int>(std::strlen(before)));

  // Same content, same id ...
  compute_fingerprint_id(table);
  TEST_ASSERT_EQUAL_STRING(before, table.fingerprint_id);

  // ... one changed feature value, different id ...
  FingerprintTable changed_feature = separable();
  changed_feature.rows[5].features[kDeltaQ1] += 0.5;
  compute_fingerprint_id(changed_feature);
  TEST_ASSERT_TRUE(std::strcmp(before, changed_feature.fingerprint_id) != 0);

  // ... and one changed LABEL, different id too.
  //
  // A relabel-only correction -- "row 4 was the kettle, not the fan" -- moves
  // no feature value at all. A hash over features alone would report the
  // corrected table and the stale one as the same table: the wizard would
  // confirm a push that never landed and the device would go on classifying
  // from the old labels. That is precisely the silent failure this id exists
  // to prevent, so it has to be in the hash.
  FingerprintTable changed_label = separable();
  TEST_ASSERT_EQUAL_STRING("desk_fan", changed_label.rows[4].label);
  std::snprintf(changed_label.rows[4].label, sizeof(changed_label.rows[4].label),
                "%s", "kettle");
  compute_fingerprint_id(changed_label);
  TEST_ASSERT_TRUE(std::strcmp(before, changed_label.fingerprint_id) != 0);
  TEST_ASSERT_TRUE(std::strcmp(changed_feature.fingerprint_id,
                               changed_label.fingerprint_id) != 0);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_classifies_a_kettle);
  RUN_TEST(test_separates_the_fan_from_the_lamp);
  RUN_TEST(test_returns_exactly_three_neighbours);
  RUN_TEST(test_neighbours_are_sorted_by_distance);
  RUN_TEST(test_neighbours_name_real_training_events);
  RUN_TEST(test_confidence_is_high_for_a_clean_match);
  RUN_TEST(test_confidence_is_bounded);
  RUN_TEST(test_rejects_a_load_it_was_never_taught);
  RUN_TEST(test_a_rejected_classification_still_reports_neighbours);
  RUN_TEST(test_z_score_normalisation_uses_mean_and_stddev);
  RUN_TEST(test_the_metric_divides_by_the_training_standard_deviation);
  RUN_TEST(test_normalisation_is_z_score_and_not_min_max);
  RUN_TEST(test_a_split_vote_can_outweigh_the_nearest_neighbour);
  RUN_TEST(test_a_zero_variance_feature_scales_by_one_not_by_epsilon);
  RUN_TEST(test_rejection_measures_from_the_winning_class_centroid);
  RUN_TEST(test_the_threshold_is_a_percentile_not_the_maximum);
  RUN_TEST(test_threshold_is_derived_not_hardcoded);
  RUN_TEST(test_inactive_features_are_excluded_from_the_distance);
  RUN_TEST(test_empty_table_rejects_everything);
  RUN_TEST(test_clear_restores_a_freshly_constructed_table);
  RUN_TEST(test_a_knn_bound_before_clear_sees_the_empty_table);
  RUN_TEST(test_memory_source_round_trips);
  RUN_TEST(test_classifier_is_identical_through_any_source);
  RUN_TEST(test_a_malformed_row_costs_one_row_not_the_table);
  RUN_TEST(test_is_emittable_label_matches_the_contract_pattern);
  RUN_TEST(test_a_label_the_contract_rejects_costs_one_row_not_the_table);
  RUN_TEST(test_a_generated_unknown_id_is_not_an_admissible_label);
  RUN_TEST(test_one_row_per_class_still_derives_a_usable_threshold);
  RUN_TEST(test_a_table_that_can_derive_no_threshold_fails_to_load);
  RUN_TEST(test_a_file_larger_than_the_table_is_refused_not_truncated);
  RUN_TEST(test_a_short_table_reports_how_many_neighbours_it_filled);
  RUN_TEST(test_fingerprint_id_reflects_content);
  return UNITY_END();
}
