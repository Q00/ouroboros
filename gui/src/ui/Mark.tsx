// The Ouroboros mark: a ring that thins toward its tail and closes on its own
// head, in one color. Drawn here; it is not derived from any other logo.

function point(angle: number, radius: number): [number, number] {
  const radians = ((angle - 90) * Math.PI) / 180;
  return [12 + radius * Math.cos(radians), 12 + radius * Math.sin(radians)];
}

function arc(from: number, to: number, radius: number): string {
  const [x0, y0] = point(from, radius);
  const [x1, y1] = point(to, radius);
  const large = to - from > 180 ? 1 : 0;
  return `M${x0.toFixed(2)} ${y0.toFixed(2)}A${radius} ${radius} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

const BODY = arc(38, 250, 8);
const TAIL = arc(250, 338, 8);
const [HEAD_X, HEAD_Y] = point(38, 8);

interface MarkProps {
  size?: number;
  className?: string;
  title?: string;
}

export function Mark({ size = 24, className, title }: MarkProps) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      <path d={TAIL} stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.55" />
      <path d={BODY} stroke="currentColor" strokeWidth="2.7" strokeLinecap="round" />
      <circle cx={HEAD_X} cy={HEAD_Y} r="2.35" fill="currentColor" />
    </svg>
  );
}

/** The mark, turning: the page's one loading indicator. */
export function Spinner({ size = 16, label }: { size?: number; label?: string }) {
  return (
    <span className="spinner" role={label ? "status" : undefined} aria-label={label}>
      <Mark size={size} />
    </span>
  );
}
