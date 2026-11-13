// The console's motion system: three springs and one curve, used everywhere,
// so things that behave alike move alike.

/** Small things that follow the pointer: nav pill, tab thumb, toggles of state. */
export const SNAP = { type: "spring", stiffness: 540, damping: 42 } as const;

/** Things arriving on the page: cards, messages, toasts, the save bar. */
export const SETTLE = { type: "spring", stiffness: 360, damping: 30 } as const;

/** Quantities changing: progress bars and meters glide rather than jump. */
export const GLIDE = { type: "spring", stiffness: 150, damping: 24 } as const;

/** The ease-out curve for timed (non-spring) entrances; matches --ease-out in CSS. */
export const EASE_OUT = [0.22, 1, 0.36, 1] as const;
