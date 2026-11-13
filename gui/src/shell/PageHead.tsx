import type { ReactNode } from "react";

import type { Tone } from "../ui/bits";
import { Dot } from "../ui/bits";

interface PageHeadProps {
  title: string;
  lead?: ReactNode;
  actions?: ReactNode;
  /** Pill tabs shown above the title. */
  tabs?: ReactNode;
  /** A status dot before the title, when the title states a condition. */
  dot?: Tone;
  dotPulse?: boolean;
}

/** The block every page opens with: optional tabs, a tight title, actions at the right. */
export function PageHead({ title, lead, actions, tabs, dot, dotPulse }: PageHeadProps) {
  return (
    <header className="page-head">
      {tabs ? <div className="head-switch">{tabs}</div> : null}
      <div className="page-head-row">
        <div className="page-head-text">
          <h1>
            {dot ? <Dot tone={dot} pulse={dotPulse} /> : null}
            {title}
          </h1>
          {lead ? <p>{lead}</p> : null}
        </div>
        {actions ? <div className="page-head-actions">{actions}</div> : null}
      </div>
    </header>
  );
}
