import { motion } from "motion/react";
import type { ReactNode } from "react";
import { useId } from "react";
import { SNAP } from "../core/motion";

export interface Segment<T extends string> {
  value: T;
  label: string;
  icon?: ReactNode;
  /** Icon-only segments still need a spoken name. */
  iconOnly?: boolean;
}

interface SegmentedProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  options: Segment<T>[];
  label: string;
  size?: "sm" | "md";
  stretch?: boolean;
}

/** A radio group drawn as one track with a thumb that slides between choices. */
export function Segmented<T extends string>({ value, onChange, options, label, size = "md", stretch }: SegmentedProps<T>) {
  const group = useId();
  return (
    <div
      className={`segmented ${size === "sm" ? "segmented-sm" : ""} ${stretch ? "segmented-stretch" : ""}`}
      role="radiogroup"
      aria-label={label}
    >
      {options.map((option, index) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={option.iconOnly ? option.label : undefined}
            title={option.iconOnly ? option.label : undefined}
            tabIndex={active ? 0 : -1}
            className="segment"
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => {
              const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
              if (!step) return;
              event.preventDefault();
              const next = options[(index + step + options.length) % options.length];
              onChange(next.value);
              const buttons = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button");
              buttons?.[(index + step + options.length) % options.length]?.focus();
            }}
          >
            {active ? (
              <motion.span
                layoutId={`segment-${group}`}
                className="seg-slider"
                transition={SNAP}
              />
            ) : null}
            <span className="segment-content">
              {option.icon}
              {option.iconOnly ? null : option.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}
