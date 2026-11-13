import type { ButtonHTMLAttributes, ReactNode } from "react";
import { forwardRef } from "react";

import { Spinner } from "./Mark";
import { Tip } from "./Tip";

type Variant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md";
  /** The action is in flight: shows the loop and blocks a second click. */
  pending?: boolean;
  icon?: ReactNode;
  trailing?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", pending = false, icon, trailing, children, className, disabled, type, ...rest },
  ref,
) {
  const classes = ["btn", `btn-${variant}`, size === "sm" ? "btn-sm" : "", className ?? ""]
    .filter(Boolean)
    .join(" ");
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      className={classes}
      disabled={disabled || pending}
      aria-busy={pending || undefined}
      data-pending={pending || undefined}
      {...rest}
    >
      {pending ? <Spinner size={size === "sm" ? 13 : 15} /> : icon}
      {children !== undefined && children !== null ? <span className="btn-label">{children}</span> : null}
      {trailing}
    </button>
  );
});

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Spoken name and tooltip. */
  label: string;
  pending?: boolean;
  children: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, pending = false, children, className, disabled, type, ...rest },
  ref,
) {
  return (
    <Tip text={label}>
      <button
        ref={ref}
        type={type ?? "button"}
        className={`icon-btn ${className ?? ""}`}
        aria-label={label}
        aria-busy={pending || undefined}
        disabled={disabled || pending}
        {...rest}
      >
        {pending ? <Spinner size={15} /> : children}
      </button>
    </Tip>
  );
});
