// GENERATED FILE - do not edit by hand.
// Regenerate with: uv run python contract/generate.py
// Source: contract/schemas/*.schema.json

export interface TelemetryElectrical {
  vrms: number;
  irms: number;
  p: number;
  q1: number;
  dist: number;
  s: number;
  pf_true: number;
  pf_disp: number;
  freq: number;
  range: "low" | "high";
}

export interface TelemetryAttributionActiveItem {
  id: string;
  w: number;
  since: number;
}

export interface TelemetryAttribution {
  active: TelemetryAttributionActiveItem[];
  residual_w: number;
  floor_w: number;
}

export interface TelemetryEnergy {
  wh_session: number;
  wh_today: number;
}

export interface TelemetryHealth {
  isr_overruns: number;
  worst_isr_us: number;
  cycles_dropped: number;
}

export interface Telemetry {
  schema: "smartwatt.telemetry.v1";
  ts: number;
  source: "device" | "simulator" | "replay";
  seq: number;
  fingerprint_id: string | null;
  electrical: TelemetryElectrical;
  attribution: TelemetryAttribution;
  energy: TelemetryEnergy;
  health: TelemetryHealth | null;
}

export interface SmartWattEventFeatures {
  delta_p: number;
  delta_q1: number;
  delta_dist: number;
  delta_s: number;
  pf_disp: number;
  pf_true: number;
  delta_irms: number;
  delta_crest: number;
  h3_h1: number;
  h5_h1: number;
  h7_h1: number;
  inrush_ratio: number;
  settle_cycles: number;
  log_delta_p: number;
}

export interface SmartWattEventNeighboursItem {
  label: string;
  distance: number;
  training_id: string;
}

export interface SmartWattEvent {
  schema: "smartwatt.event.v1";
  ts: number;
  source: "device" | "simulator" | "replay";
  seq: number;
  edge: "on" | "off";
  label: string | null;
  confidence: number;
  rejected: boolean;
  ambiguous: boolean;
  reason: null | "below_floor" | "no_settle" | "overlapping_edges" | "distance_threshold";
  attributed_to: string | null;
  features: SmartWattEventFeatures;
  neighbours: SmartWattEventNeighboursItem[];
}

export interface Command {
  device: string;
  command: "ON" | "OFF";
}

export interface FeatureMap {
  delta_p: number;
  delta_q1: number;
  delta_dist: number;
  delta_s: number;
  pf_disp: number;
  pf_true: number;
  delta_irms: number;
  delta_crest: number;
  h3_h1: number;
  h5_h1: number;
  h7_h1: number;
  inrush_ratio: number;
  settle_cycles: number;
  log_delta_p: number;
}

export interface FingerprintsNormalisation {
  mean: FeatureMap;
  std: FeatureMap;
}

export interface FingerprintsRowsItem {
  training_id: string;
  label: string;
  edge: "on" | "off";
  features: FeatureMap;
}

export interface Fingerprints {
  schema: "smartwatt.fingerprints.v1";
  fingerprint_id: string;
  rejection_threshold: number;
  active_features: ("delta_p" | "delta_q1" | "delta_dist" | "delta_s" | "pf_disp" | "pf_true" | "delta_irms" | "delta_crest" | "h3_h1" | "h5_h1" | "h7_h1" | "inrush_ratio" | "settle_cycles" | "log_delta_p")[];
  normalisation: FingerprintsNormalisation;
  rows: FingerprintsRowsItem[];
}
