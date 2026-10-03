import type { ScatterPoint } from "../../lib/api";

/**
 * A quiet greyscale ramp, same pattern as `Live/StackedPower.tsx`'s
 * `APPLIANCE_INK`: one shade per class, cycling if there are more classes
 * than shades. Kept local rather than shared, same as that file -- neither
 * screen needs the other to agree on which index maps to which shade.
 */
const CLASS_INK = ["#16150f", "#3d3b31", "#5c5a4f", "#7b786c", "#9a978a"];

export interface SignatureScatterProps {
  points: ScatterPoint[];
  labels: string[];
}

const PAD = 12;
const VIEW = 320;

/** Maps a data range to the [PAD, VIEW-PAD] pixel range, flat if the range
 *  is degenerate (a single point, or every point sharing a coordinate). */
function scale(value: number, min: number, max: number, invert = false): number {
  const span = max - min;
  const t = span === 0 ? 0.5 : (value - min) / span;
  const pixel = PAD + t * (VIEW - 2 * PAD);
  return invert ? VIEW - pixel : pixel;
}

export function SignatureScatter({ points, labels }: SignatureScatterProps) {
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const xMin = xs.length ? Math.min(...xs) : -1;
  const xMax = xs.length ? Math.max(...xs) : 1;
  const yMin = ys.length ? Math.min(...ys) : -1;
  const yMax = ys.length ? Math.max(...ys) : 1;

  const colourFor = (label: string) => {
    const index = labels.indexOf(label);
    return CLASS_INK[(index < 0 ? 0 : index) % CLASS_INK.length];
  };

  return (
    <div data-testid="signature-scatter">
      <svg
        viewBox={`0 0 ${VIEW} ${VIEW}`}
        width="100%"
        role="img"
        aria-label="Signature scatter: 2-D projection of the trained classes"
        style={{ maxWidth: `${VIEW}px` }}
      >
        <rect x={0} y={0} width={VIEW} height={VIEW} fill="var(--color-paper)" />
        <line
          x1={PAD} y1={VIEW / 2} x2={VIEW - PAD} y2={VIEW / 2}
          stroke="var(--color-rule)"
        />
        <line
          x1={VIEW / 2} y1={PAD} x2={VIEW / 2} y2={VIEW - PAD}
          stroke="var(--color-rule)"
        />
        {points.map((point, index) => (
          <circle
            key={`${point.label}-${index}`}
            data-testid={`scatter-point-${index}`}
            cx={scale(point.x, xMin, xMax)}
            cy={scale(point.y, yMin, yMax, true)}
            r={4}
            fill={colourFor(point.label)}
            fillOpacity={0.85}
          >
            <title>{point.label}</title>
          </circle>
        ))}
      </svg>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 pt-2">
        {labels.map((label) => (
          <li
            key={label}
            data-testid={`scatter-legend-${label}`}
            className="flex items-center gap-1.5 text-xs"
          >
            <span
              className="inline-block h-2.5 w-2.5 rounded-full"
              style={{ background: colourFor(label) }}
              aria-hidden="true"
            />
            {label}
          </li>
        ))}
      </ul>
    </div>
  );
}
