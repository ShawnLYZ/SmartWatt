import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const SRC = resolve(__dirname, "../src");

/**
 * US6: charts are drawn with step interpolation and NEVER smoothed. A
 * smoothed line implies the system knows something between switching
 * events. It does not.
 */
const FORBIDDEN = ["monotone", "natural", "basis", "cardinal", "catmullRom"];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...walk(path));
    else if (/\.tsx?$/.test(path)) out.push(path);
  }
  return out;
}

describe("step interpolation only", () => {
  it("no smoothing curve type appears anywhere in the source", () => {
    const offenders: string[] = [];
    for (const file of walk(SRC)) {
      const text = readFileSync(file, "utf8");
      for (const curve of FORBIDDEN) {
        if (new RegExp(`["'\`]${curve}["'\`]`).test(text)) {
          offenders.push(`${file}: ${curve}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("every chart series declares a step type", () => {
    const charts = walk(SRC).filter((f) =>
      /<(Area|Line)\b/.test(readFileSync(f, "utf8")),
    );
    expect(charts.length).toBeGreaterThan(0);
    for (const file of charts) {
      const text = readFileSync(file, "utf8");
      const series = text.match(/<(Area|Line)\b[^>]*>/gs) ?? [];
      for (const tag of series) {
        expect(tag, `${file}: ${tag}`).toMatch(
          /type=\{?["']?step(After|Before)?["']?\}?/,
        );
      }
    }
  });
});
