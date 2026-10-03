#include <unity.h>

#include <cstdio>
#include <cstring>
#include <string>

#include "tracker.h"

void setUp() {}
void tearDown() {}

static Classification classified(const char* label, bool rejected = false,
                                 const char* second = nullptr) {
  Classification c;
  c.rejected = rejected;
  c.confidence = rejected ? 0.0 : 0.94;
  if (!rejected) std::snprintf(c.label, sizeof(c.label), "%s", label);
  std::snprintf(c.neighbours[0].label, sizeof(c.neighbours[0].label), "%s",
                label);
  c.neighbours[0].distance = 0.31;
  std::snprintf(c.neighbours[1].label, sizeof(c.neighbours[1].label), "%s",
                second ? second : label);
  c.neighbours[1].distance = 0.44;
  std::snprintf(c.neighbours[2].label, sizeof(c.neighbours[2].label), "%s",
                label);
  c.neighbours[2].distance = 0.52;
  c.neighbour_count = 3;
  return c;
}

/// A classification with the neighbour list spelled out rank by rank, plus the
/// count Knn::classify publishes beside it. A null name ends the list, which
/// is exactly what a fingerprint table with fewer than three rows produces:
/// the trailing slots stay default-constructed and the count says so.
static Classification ranked(const char* label, const char* first,
                             const char* second, const char* third) {
  Classification c;
  c.rejected = false;
  c.confidence = 0.61;
  std::snprintf(c.label, sizeof(c.label), "%s", label);
  const char* const names[3] = {first, second, third};
  const double distances[3] = {0.31, 0.44, 0.52};
  for (int n = 0; n < 3; ++n) {
    if (names[n] == nullptr) break;
    std::snprintf(c.neighbours[n].label, sizeof(c.neighbours[n].label), "%s",
                  names[n]);
    c.neighbours[n].distance = distances[n];
    ++c.neighbour_count;
  }
  return c;
}

static DetectedEvent edge(Edge direction, double delta,
                          EventFlag flag = EventFlag::Clean) {
  DetectedEvent e;
  e.edge = direction;
  e.flag = flag;
  e.valid = true;
  e.before = CycleMetrics{};
  e.after = CycleMetrics{};
  e.before.valid = e.after.valid = true;
  if (direction == Edge::On) {
    e.before.p = 0.0;
    e.after.p = delta;
  } else {
    e.before.p = delta;
    e.after.p = 0.0;
  }
  // The settled step the detector judged from, signed. The tracker reads
  // this, never the cycle difference.
  e.delta_p = (direction == Edge::On) ? delta : -delta;
  return e;
}

static bool is_active(const Tracker& tracker, const char* id) {
  for (int n = 0; n < tracker.active_count(); ++n) {
    if (std::strcmp(tracker.active()[n].id, id) == 0) return true;
  }
  return false;
}

static double watts_of(const Tracker& tracker, const char* id) {
  for (int n = 0; n < tracker.active_count(); ++n) {
    if (std::strcmp(tracker.active()[n].id, id) == 0) {
      return tracker.active()[n].watts;
    }
  }
  return 0.0;
}

static void test_an_on_event_adds_to_the_active_set() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  TEST_ASSERT_TRUE(is_active(tracker, "kettle"));
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 1800.0, watts_of(tracker, "kettle"));
}

static void test_an_off_event_removes_from_the_active_set() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.apply(edge(Edge::Off, 1800.0), classified("kettle"), 200.0);
  TEST_ASSERT_FALSE(is_active(tracker, "kettle"));
}

static void test_since_records_when_it_came_on() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 45.0), classified("desk_fan"), 1754035188.0);
  TEST_ASSERT_DOUBLE_WITHIN(0.01, 1754035188.0, tracker.active()[0].since);
}

/// `since` is per ACTIVATION, not per first-ever sighting. The watts estimate
/// outlives the appliance switching off; the timestamp must not, or every
/// uptime figure reports the first time the appliance was ever seen and grows
/// without bound.
static void test_since_is_per_activation_not_first_ever_seen() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.apply(edge(Edge::Off, 1800.0), classified("kettle"), 200.0);
  tracker.apply(edge(Edge::On, 1700.0), classified("kettle"), 300.0);
  TEST_ASSERT_EQUAL_INT(1, tracker.active_count());
  TEST_ASSERT_DOUBLE_WITHIN(0.01, 300.0, tracker.active()[0].since);
}

/// THE named regression test.
///
/// Blending from a default on the FIRST observation drags every subsequent
/// estimate toward that default in a way that is silent, permanent, and
/// invisible in aggregate figures. First observation SETS.
static void test_tracker_first_observation_sets_not_blends() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  // If the first observation blended with a zero default, this would be
  // roughly half of 1800.
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 1800.0, watts_of(tracker, "kettle"));
}

static void test_second_observation_blends() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.apply(edge(Edge::Off, 1800.0), classified("kettle"), 200.0);
  tracker.apply(edge(Edge::On, 1700.0), classified("kettle"), 300.0);
  const double watts = watts_of(tracker, "kettle");
  TEST_ASSERT_TRUE_MESSAGE(watts > 1700.0 && watts < 1800.0,
                           "later observations blend toward the new value");
}

/// The estimate belongs to the APPLIANCE, not to one activation. It has to
/// survive the appliance switching off, and each new observation has to blend
/// from the accumulated value rather than from the first one -- otherwise
/// "blends" means nothing more than "averages the last two numbers".
static void test_estimate_survives_off_and_accumulates() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.apply(edge(Edge::Off, 1800.0), classified("kettle"), 200.0);

  tracker.apply(edge(Edge::On, 1700.0), classified("kettle"), 300.0);
  // 0.7 * 1800 + 0.3 * 1700. Had the estimate died with the active entry this
  // would be a bare 1700.
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 1770.0, watts_of(tracker, "kettle"));

  tracker.apply(edge(Edge::Off, 1700.0), classified("kettle"), 400.0);
  tracker.apply(edge(Edge::On, 1700.0), classified("kettle"), 500.0);
  // 0.7 * 1770 + 0.3 * 1700. Blending from the BLENDED value; blending from
  // the first observation again would give 1770 a second time.
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 1749.0, watts_of(tracker, "kettle"));
}

/// US17: an OFF event can only be attributed to an appliance currently
/// believed to be on.
static void test_impossible_off_is_reassigned_to_an_active_neighbour() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 40.0), classified("incandescent_lamp"), 100.0);
  const Attribution result = tracker.apply(
      edge(Edge::Off, 40.0),
      classified("desk_fan", false, "incandescent_lamp"), 200.0);

  TEST_ASSERT_EQUAL_STRING("desk_fan", result.label);
  TEST_ASSERT_EQUAL_STRING("incandescent_lamp", result.attributed_to);
  TEST_ASSERT_FALSE(is_active(tracker, "incandescent_lamp"));
}

/// "Highest-ranked candidate that IS on" means the neighbour list's own order
/// decides -- not the delta, and not whichever active entry happens to be
/// scanned last. Both neighbours here are on, and the OFF's delta matches the
/// LOWER-ranked one exactly.
static void test_reassignment_prefers_the_highest_ranked_active_neighbour() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 40.0), classified("incandescent_lamp"), 100.0);
  tracker.apply(edge(Edge::On, 2000.0), classified("washing_machine"), 110.0);

  const Attribution result = tracker.apply(
      edge(Edge::Off, 2000.0),
      ranked("desk_fan", "desk_fan", "incandescent_lamp", "washing_machine"),
      200.0);

  TEST_ASSERT_EQUAL_STRING("incandescent_lamp", result.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "washing_machine"));
  TEST_ASSERT_FALSE(is_active(tracker, "incandescent_lamp"));
}

/// A fingerprint table with fewer than three rows leaves trailing neighbour
/// slots unfilled. `neighbour_count` is what stops the reassignment walk at
/// the ones the classifier really produced.
static void test_empty_neighbour_slots_never_match_an_active_id() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  const Classification one_row =
      ranked("desk_fan", "desk_fan", nullptr, nullptr);
  TEST_ASSERT_EQUAL_INT(1, one_row.neighbour_count);

  const Attribution result =
      tracker.apply(edge(Edge::Off, 40.0), one_row, 200.0);
  TEST_ASSERT_EQUAL_STRING("desk_fan", result.label);
  TEST_ASSERT_EQUAL_STRING("", result.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "kettle"));
  TEST_ASSERT_EQUAL_INT(1, tracker.active_count());
}

/// Slots PAST the count are not part of the classification, whatever they hold.
///
/// This is what makes the count the primary test rather than a second opinion
/// on the label. Slot 1 here names an appliance that really is on, so an
/// empty-label check alone would walk into it and retire that appliance -- on
/// the strength of a neighbour the classifier never reported. Only the count
/// can tell that slot 1 is leftover.
static void test_neighbours_past_the_count_are_not_considered() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 40.0), classified("incandescent_lamp"), 100.0);

  Classification stale = ranked("desk_fan", "desk_fan", nullptr, nullptr);
  TEST_ASSERT_EQUAL_INT(1, stale.neighbour_count);
  // Left over from an earlier event, or hand-built by a caller that filled the
  // array without the count. Non-empty, and active.
  std::snprintf(stale.neighbours[1].label, sizeof(stale.neighbours[1].label),
                "incandescent_lamp");
  stale.neighbours[1].distance = 0.44;

  const Attribution result = tracker.apply(edge(Edge::Off, 40.0), stale, 200.0);

  TEST_ASSERT_EQUAL_STRING("desk_fan", result.label);
  TEST_ASSERT_EQUAL_STRING("", result.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "incandescent_lamp"));
  TEST_ASSERT_EQUAL_INT(1, tracker.active_count());
}

/// The empty-label check behind the count is BELT AND BRACES, and it has to
/// stay one. A caller that hand-builds a Classification -- every fixture in
/// this file does, and so will S6 -- can set a count larger than the slots it
/// actually filled, and then the walk reaches a default-constructed Neighbour.
/// Its label is empty and its distance is 0.0, the value of a perfect match.
///
/// The count is deliberately WRONG here, which is the whole point: with the
/// belt-and-braces check removed the walk would compare "" against the active
/// ids and, if any of them were ever empty, retire the wrong appliance.
static void test_a_neighbour_count_larger_than_the_filled_slots_matches_none() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);

  Classification overstated = ranked("desk_fan", "desk_fan", nullptr, nullptr);
  overstated.neighbour_count = 3;  // two slots are still default-constructed
  const Attribution result =
      tracker.apply(edge(Edge::Off, 40.0), overstated, 200.0);

  TEST_ASSERT_EQUAL_STRING("", result.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "kettle"));
  TEST_ASSERT_EQUAL_INT(1, tracker.active_count());
}

/// ...and a count larger than the ARRAY may not read past its end either.
static void test_a_neighbour_count_past_the_array_is_bounded() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);

  Classification overrun = ranked("desk_fan", "desk_fan", nullptr, nullptr);
  overrun.neighbour_count = 99;
  const Attribution result =
      tracker.apply(edge(Edge::Off, 40.0), overrun, 200.0);

  TEST_ASSERT_EQUAL_STRING("", result.attributed_to);
  TEST_ASSERT_EQUAL_INT(1, tracker.active_count());
}

static void test_label_and_attributed_to_both_survive() {
  // Recording only the final answer would erase the evidence that the
  // state-consistency filter fired, which is what US16 and US49 ask an
  // evaluator to be able to inspect.
  Tracker tracker;
  tracker.apply(edge(Edge::On, 40.0), classified("incandescent_lamp"), 100.0);
  const Attribution result = tracker.apply(
      edge(Edge::Off, 40.0),
      classified("desk_fan", false, "incandescent_lamp"), 200.0);
  TEST_ASSERT_TRUE(std::strcmp(result.label, result.attributed_to) != 0);
}

static void test_impossible_off_with_no_active_candidate_is_unknown() {
  Tracker tracker;
  const Attribution result =
      tracker.apply(edge(Edge::Off, 40.0), classified("desk_fan"), 100.0);
  TEST_ASSERT_EQUAL_STRING("", result.attributed_to);
}

/// US13, US14: a rejected but cleanly settled ON event gets a generated id
/// and enters the active set, so its energy is still counted.
static void test_rejected_on_becomes_an_unknown_load() {
  Tracker tracker;
  const Attribution result = tracker.apply(
      edge(Edge::On, 305.0), classified("laptop_charger", true), 100.0);
  TEST_ASSERT_EQUAL_STRING("", result.label);
  TEST_ASSERT_EQUAL_STRING("unknown_1", result.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_1"));
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 305.0, watts_of(tracker, "unknown_1"));
}

static void test_unknown_ids_increment() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 305.0), classified("x", true), 100.0);
  tracker.apply(edge(Edge::On, 900.0), classified("x", true), 200.0);
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_1"));
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_2"));
}

static void test_unknown_off_is_paired_by_delta_proximity() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 305.0), classified("x", true), 100.0);
  tracker.apply(edge(Edge::On, 900.0), classified("x", true), 200.0);
  const Attribution result =
      tracker.apply(edge(Edge::Off, 310.0), classified("x", true), 300.0);
  TEST_ASSERT_EQUAL_STRING("unknown_1", result.attributed_to);
  TEST_ASSERT_FALSE(is_active(tracker, "unknown_1"));
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_2"));
}

/// Proximity has to be a BOUND, not merely an argmin. A 900 W off against a
/// 305 W unknown is 195% out; pairing it anyway would silently retire a load
/// that is still drawing.
static void test_unknown_off_far_from_every_unknown_is_not_paired() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 305.0), classified("x", true), 100.0);
  const Attribution result =
      tracker.apply(edge(Edge::Off, 900.0), classified("x", true), 200.0);
  TEST_ASSERT_EQUAL_STRING("", result.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_1"));
}

/// Refusing to name a load never means losing track of its energy: an unknown
/// counts toward attributed power exactly like a named appliance does.
static void test_unknown_load_energy_counts_as_attributed() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 305.0), classified("laptop_charger", true),
                100.0);
  tracker.observe_total(400.0, 110.0);
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 95.0, tracker.residual());
}

static void test_ambiguous_event_changes_nothing() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  const int before = tracker.active_count();
  const Attribution result = tracker.apply(
      edge(Edge::On, 45.0, EventFlag::OverlappingEdges), classified("desk_fan"),
      200.0);
  TEST_ASSERT_TRUE(result.ambiguous);
  TEST_ASSERT_EQUAL_STRING("", result.attributed_to);
  TEST_ASSERT_EQUAL_INT(before, tracker.active_count());
}

/// ...and the energy it declined to attribute turns up in the residual, which
/// is what makes US18 honest rather than merely quiet.
static void test_ambiguous_energy_falls_to_the_residual() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.apply(edge(Edge::On, 45.0, EventFlag::OverlappingEdges),
                classified("desk_fan"), 200.0);
  tracker.observe_total(1845.0, 210.0);
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 45.0, tracker.residual());
}

static void test_no_settle_event_changes_nothing() {
  Tracker tracker;
  const Attribution result = tracker.apply(
      edge(Edge::On, 900.0, EventFlag::NoSettle), classified("kettle"), 100.0);
  TEST_ASSERT_TRUE(result.ambiguous);
  TEST_ASSERT_EQUAL_INT(0, tracker.active_count());
}

static void test_below_floor_event_changes_nothing() {
  Tracker tracker;
  const Attribution result = tracker.apply(
      edge(Edge::On, 4.2, EventFlag::BelowFloor), classified("led_bulb"), 100.0);
  TEST_ASSERT_TRUE(result.rejected);
  TEST_ASSERT_EQUAL_INT(0, tracker.active_count());
}

/// `reason` is the contract's enum and Task 4 emits it. Nothing else in this
/// suite reads it, so without this test every reason could stay nullptr and
/// the suite would still be green.
static void test_reasons_are_recorded() {
  Tracker tracker;

  const Attribution overlapping =
      tracker.apply(edge(Edge::On, 45.0, EventFlag::OverlappingEdges),
                    classified("desk_fan"), 100.0);
  TEST_ASSERT_EQUAL_STRING("overlapping_edges", overlapping.reason);

  const Attribution no_settle = tracker.apply(
      edge(Edge::On, 900.0, EventFlag::NoSettle), classified("kettle"), 200.0);
  TEST_ASSERT_EQUAL_STRING("no_settle", no_settle.reason);

  const Attribution below_floor = tracker.apply(
      edge(Edge::On, 4.2, EventFlag::BelowFloor), classified("led_bulb"), 300.0);
  TEST_ASSERT_EQUAL_STRING("below_floor", below_floor.reason);
  TEST_ASSERT_EQUAL_STRING("", below_floor.label);

  const Attribution unrecognised =
      tracker.apply(edge(Edge::On, 305.0), classified("x", true), 400.0);
  TEST_ASSERT_EQUAL_STRING("distance_threshold", unrecognised.reason);
}

/// The step is the detector's `delta_p`, never `after.p - before.p`.
///
/// `after` is whichever cycle the settle run happened to end on, so on a noisy
/// stream the two differ by the whole step and can disagree in sign.
/// Recomputing here would put a different number in the active set than the
/// one the feature vector and the BelowFloor test were judged from.
static void test_delta_comes_from_the_event_not_the_cycles() {
  DetectedEvent event;
  event.edge = Edge::On;
  event.flag = EventFlag::Clean;
  event.valid = true;
  event.before = CycleMetrics{};
  event.after = CycleMetrics{};
  event.before.valid = event.after.valid = true;
  event.before.p = 1200.0;
  // Wrong magnitude AND wrong sign against the settled step below.
  event.after.p = 1180.0;
  event.delta_p = 305.0;

  Tracker tracker;
  const Attribution result =
      tracker.apply(event, classified("laptop_charger", true), 100.0);
  TEST_ASSERT_EQUAL_STRING("unknown_1", result.attributed_to);
  // 305 from delta_p, not 20 from the cycle difference.
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 305.0, watts_of(tracker, "unknown_1"));
}

static void test_residual_is_total_minus_attributed() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.observe_total(1850.0, 110.0);
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 50.0, tracker.residual());
}

/// The residual is as of the last observe_total(), NOT the last apply().
///
/// Deliberate, and documented on Tracker::observe_total() and residual(),
/// because apply() has no measured total to recompute from -- only the cycle
/// stream carries one. It is pinned here so the header's contract is
/// executable rather than prose, and so a future edit that made apply()
/// invent a residual from its own numbers goes red.
///
/// The consequence for whoever wires the chain (S6) is the whole reason it is
/// written down: payload.cpp reads the active set LIVE beside this cached
/// number, so a frame emitted after an apply() but before the next
/// observe_total() carries a live active set and a stale residual. Below,
/// 1000 W measured against 1700 W attributed is truly -700, and the tracker
/// still reports the 300 the previous cycle computed.
static void test_the_residual_is_as_of_the_last_observe_total() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1000.0), classified("kettle"), 100.0);
  tracker.observe_total(1300.0, 110.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 300.0, tracker.residual());

  // A second load comes on. The active set changes; the residual does not.
  tracker.apply(edge(Edge::On, 700.0), classified("desk_fan"), 120.0);
  TEST_ASSERT_EQUAL_INT(2, tracker.active_count());
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 300.0, tracker.residual());

  // Only the next observe_total() reconciles the two, and it does so
  // unclamped: 1000 measured against 1700 attributed is -700.
  tracker.observe_total(1000.0, 130.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, -700.0, tracker.residual());
}

/// Non-negotiable #2: the residual may go negative and is reported anyway.
static void test_residual_may_be_negative_and_is_not_clamped() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.observe_total(1780.0, 110.0);
  TEST_ASSERT_TRUE(tracker.residual() < 0.0);
  TEST_ASSERT_DOUBLE_WITHIN(1.0, -20.0, tracker.residual());
}

static void test_residual_with_nothing_active_is_the_whole_total() {
  Tracker tracker;
  tracker.observe_total(72.0, 100.0);
  TEST_ASSERT_DOUBLE_WITHIN(0.01, 72.0, tracker.residual());
}

/// Fixed capacity, no dynamic allocation: the seventeenth simultaneous load
/// must be dropped, not written past the end of the array.
static void test_active_set_is_bounded() {
  Tracker tracker;
  char label[24];
  Attribution overflow;
  for (int n = 0; n < Tracker::kMaxActive + 4; ++n) {
    std::snprintf(label, sizeof(label), "load_%d", n);
    overflow =
        tracker.apply(edge(Edge::On, 100.0 + n), classified(label), 100.0 + n);
  }
  TEST_ASSERT_EQUAL_INT(Tracker::kMaxActive, tracker.active_count());
  TEST_ASSERT_TRUE(is_active(tracker, "load_0"));
  TEST_ASSERT_FALSE(is_active(tracker, "load_16"));
  // R12: the load was not tracked, so its watts sit in no active entry and
  // the residual absorbs them. The payload must not claim otherwise -- the
  // classifier's answer survives on `label`, but `attributed_to` records
  // what the tracker actually did, which was nothing.
  TEST_ASSERT_EQUAL_STRING("load_19", overflow.label);
  TEST_ASSERT_EQUAL_STRING("", overflow.attributed_to);
}

/// R12 on the rejected-ON path, where add() can fail the same way.
///
/// Also pins that a failed add does NOT burn the unknown number: the
/// numbering records loads the tracker holds, not attempts it made.
static void test_untracked_unknown_claims_no_attribution() {
  Tracker tracker;
  Attribution overflow;
  for (int n = 0; n < Tracker::kMaxActive + 2; ++n) {
    overflow = tracker.apply(edge(Edge::On, 300.0 + 10.0 * n),
                             classified("x", true), 100.0 + n);
  }
  TEST_ASSERT_EQUAL_INT(Tracker::kMaxActive, tracker.active_count());
  TEST_ASSERT_EQUAL_STRING("", overflow.attributed_to);
  TEST_ASSERT_EQUAL_STRING("distance_threshold", overflow.reason);

  // Free one slot. unknown_1 was the 300 W load, so an exact-delta OFF pairs
  // with it. The next unrecognised load then takes unknown_17 -- the next
  // number after the sixteen that were actually tracked, not after the two
  // that failed.
  tracker.apply(edge(Edge::Off, 300.0), classified("x", true), 200.0);
  const Attribution next =
      tracker.apply(edge(Edge::On, 999.0), classified("x", true), 300.0);
  TEST_ASSERT_EQUAL_STRING("unknown_17", next.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_17"));
}

/// R11: unknown_N ids never occupy a remembered-estimate slot.
///
/// Each one gets a fresh id and can only ever be observed once, so a stored
/// estimate for it is unreachable -- but the slot it took would be permanent.
/// Let enough of them through and the table fills with entries that can never
/// be looked up, at which point every REAL appliance quietly stops blending.
/// Nothing in the active set or the aggregate figures would show it.
static void test_unknown_loads_never_crowd_out_a_named_estimate() {
  Tracker tracker;
  // Twice the table's capacity, each paired off again so the ACTIVE set
  // never fills and only the known table is under pressure.
  for (int n = 0; n < Tracker::kMaxKnown * 2; ++n) {
    const double watts = 300.0 + n;
    tracker.apply(edge(Edge::On, watts), classified("x", true), 100.0 + 2 * n);
    tracker.apply(edge(Edge::Off, watts), classified("x", true), 101.0 + 2 * n);
  }
  TEST_ASSERT_EQUAL_INT(0, tracker.active_count());

  // A named appliance must still be remembered, and still blend.
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 1000.0);
  tracker.apply(edge(Edge::Off, 1800.0), classified("kettle"), 1100.0);
  tracker.apply(edge(Edge::On, 1700.0), classified("kettle"), 1200.0);
  // 0.7 * 1800 + 0.3 * 1700. If the unknowns had taken the slots this would
  // be a bare 1700: a first observation, forever.
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 1770.0, watts_of(tracker, "kettle"));
}

/// Finding 1: the known table's capacity bound, and its documented
/// full-table policy ("SET, unrecorded, do not evict").
///
/// The active set got a dedicated bound test and this one did not, purely by
/// omission. Deleting `known_count_ < kMaxKnown` is an out-of-bounds write,
/// and no other test drives enough distinct NAMED ids to reach it -- the most
/// any single tracker sees elsewhere is 20, against a table of 32.
static void test_known_table_is_bounded_and_never_evicts() {
  Tracker tracker;
  char label[24];
  // Fill the estimate table exactly. Each load is switched off again so the
  // ACTIVE set never fills: only the known table is under pressure.
  for (int n = 0; n < Tracker::kMaxKnown; ++n) {
    std::snprintf(label, sizeof(label), "load_%d", n);
    tracker.apply(edge(Edge::On, 100.0 + n), classified(label), 100.0 + 2 * n);
    tracker.apply(edge(Edge::Off, 100.0 + n), classified(label), 101.0 + 2 * n);
  }
  TEST_ASSERT_EQUAL_INT(0, tracker.active_count());

  // One id beyond capacity. Policy is SET and NOT remembered, so its second
  // observation SETS again instead of blending.
  tracker.apply(edge(Edge::On, 900.0), classified("overflow_load"), 1000.0);
  tracker.apply(edge(Edge::Off, 900.0), classified("overflow_load"), 1100.0);
  tracker.apply(edge(Edge::On, 500.0), classified("overflow_load"), 1200.0);
  // Had it been recorded it would blend to 0.7 * 900 + 0.3 * 500 = 780.
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 500.0, watts_of(tracker, "overflow_load"));

  // ...and nothing already in the table was evicted to make room for it.
  tracker.apply(edge(Edge::On, 200.0), classified("load_0"), 1300.0);
  // 0.7 * 100 + 0.3 * 200. Evicted, load_0 would be a first observation again
  // and SET to a bare 200.
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 130.0, watts_of(tracker, "load_0"));
}

/// Finding 2: nearest_unknown() is an ARGMIN inside the bound, not merely the
/// first candidate that clears it.
///
/// Both unknowns here sit inside the 25% band, and the nearer one is second in
/// scan order: 480 W is 20.00% from unknown_1 (80/400) and 4.00% from
/// unknown_2 (20/500). First-in-tolerance-wins retires unknown_1; only a true
/// argmin retires unknown_2.
static void test_unknown_off_pairs_with_the_nearest_not_the_first_in_band() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 400.0), classified("x", true), 100.0);
  tracker.apply(edge(Edge::On, 500.0), classified("x", true), 200.0);
  const Attribution result =
      tracker.apply(edge(Edge::Off, 480.0), classified("x", true), 300.0);
  TEST_ASSERT_EQUAL_STRING("unknown_2", result.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_1"));
  TEST_ASSERT_FALSE(is_active(tracker, "unknown_2"));
}

/// A second ON for an appliance already believed on refreshes the estimate but
/// must NOT restart the clock: it never went off. The off->on path is covered
/// by test_since_is_per_activation_not_first_ever_seen; this is the other
/// branch of add(), which nothing else asserts.
static void test_a_repeated_on_while_already_active_keeps_since() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.apply(edge(Edge::On, 1700.0), classified("kettle"), 300.0);
  TEST_ASSERT_EQUAL_INT(1, tracker.active_count());
  TEST_ASSERT_DOUBLE_WITHIN(0.01, 100.0, tracker.active()[0].since);
  // The estimate still blends: 0.7 * 1800 + 0.3 * 1700.
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 1770.0, watts_of(tracker, "kettle"));
}

/// `unknown_device` is a legal NAMED label under the contract's id pattern
/// `^([a-z][a-z0-9_]*|unknown_[0-9]+)$`. R11 gave the `unknown_` prefix a
/// second job, so a bare prefix test would quietly hand a real appliance both
/// halves of the generated-id treatment.
static void test_a_named_label_beginning_unknown_is_not_a_generated_id() {
  Tracker tracker;

  // (a) It earns a persistent estimate and blends like any other appliance.
  tracker.apply(edge(Edge::On, 1800.0), classified("unknown_device"), 100.0);
  tracker.apply(edge(Edge::Off, 1800.0), classified("unknown_device"), 200.0);
  tracker.apply(edge(Edge::On, 1700.0), classified("unknown_device"), 300.0);
  // Treated as generated, every observation would SET and this would be 1700.
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 1770.0, watts_of(tracker, "unknown_device"));

  // (b) It is not eligible for delta-pairing. An unrecognised OFF matching its
  // wattage exactly must find no active unknown to retire.
  const Attribution result =
      tracker.apply(edge(Edge::Off, 1770.0), classified("x", true), 400.0);
  TEST_ASSERT_EQUAL_STRING("", result.attributed_to);
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_device"));
}

static void test_reset_clears_everything() {
  Tracker tracker;
  tracker.apply(edge(Edge::On, 1800.0), classified("kettle"), 100.0);
  tracker.observe_total(1850.0, 110.0);
  tracker.reset();
  TEST_ASSERT_EQUAL_INT(0, tracker.active_count());
  TEST_ASSERT_DOUBLE_WITHIN(0.01, 0.0, tracker.residual());
  // Unknown numbering restarts too.
  tracker.apply(edge(Edge::On, 305.0), classified("x", true), 200.0);
  TEST_ASSERT_TRUE(is_active(tracker, "unknown_1"));
  // ...and so does the watts estimate. A reset tracker has never seen a
  // kettle, so this SETS. A half-cleared reset would blend and give
  // 0.7 * 1800 + 0.3 * 1000 = 1560.
  tracker.apply(edge(Edge::On, 1000.0), classified("kettle"), 300.0);
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 1000.0, watts_of(tracker, "kettle"));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_an_on_event_adds_to_the_active_set);
  RUN_TEST(test_an_off_event_removes_from_the_active_set);
  RUN_TEST(test_since_records_when_it_came_on);
  RUN_TEST(test_since_is_per_activation_not_first_ever_seen);
  RUN_TEST(test_tracker_first_observation_sets_not_blends);
  RUN_TEST(test_second_observation_blends);
  RUN_TEST(test_estimate_survives_off_and_accumulates);
  RUN_TEST(test_impossible_off_is_reassigned_to_an_active_neighbour);
  RUN_TEST(test_reassignment_prefers_the_highest_ranked_active_neighbour);
  RUN_TEST(test_empty_neighbour_slots_never_match_an_active_id);
  RUN_TEST(test_neighbours_past_the_count_are_not_considered);
  RUN_TEST(test_a_neighbour_count_larger_than_the_filled_slots_matches_none);
  RUN_TEST(test_a_neighbour_count_past_the_array_is_bounded);
  RUN_TEST(test_label_and_attributed_to_both_survive);
  RUN_TEST(test_impossible_off_with_no_active_candidate_is_unknown);
  RUN_TEST(test_rejected_on_becomes_an_unknown_load);
  RUN_TEST(test_unknown_ids_increment);
  RUN_TEST(test_unknown_off_is_paired_by_delta_proximity);
  RUN_TEST(test_unknown_off_far_from_every_unknown_is_not_paired);
  RUN_TEST(test_unknown_load_energy_counts_as_attributed);
  RUN_TEST(test_ambiguous_event_changes_nothing);
  RUN_TEST(test_ambiguous_energy_falls_to_the_residual);
  RUN_TEST(test_no_settle_event_changes_nothing);
  RUN_TEST(test_below_floor_event_changes_nothing);
  RUN_TEST(test_reasons_are_recorded);
  RUN_TEST(test_delta_comes_from_the_event_not_the_cycles);
  RUN_TEST(test_residual_is_total_minus_attributed);
  RUN_TEST(test_the_residual_is_as_of_the_last_observe_total);
  RUN_TEST(test_residual_may_be_negative_and_is_not_clamped);
  RUN_TEST(test_residual_with_nothing_active_is_the_whole_total);
  RUN_TEST(test_active_set_is_bounded);
  RUN_TEST(test_untracked_unknown_claims_no_attribution);
  RUN_TEST(test_unknown_loads_never_crowd_out_a_named_estimate);
  RUN_TEST(test_known_table_is_bounded_and_never_evicts);
  RUN_TEST(test_unknown_off_pairs_with_the_nearest_not_the_first_in_band);
  RUN_TEST(test_a_repeated_on_while_already_active_keeps_since);
  RUN_TEST(test_a_named_label_beginning_unknown_is_not_a_generated_id);
  RUN_TEST(test_reset_clears_everything);
  return UNITY_END();
}
