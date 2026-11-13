import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";

import { useT } from "../i18n";

interface ModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  /** A panel pinned to the right edge instead of a centered dialog. */
  side?: boolean;
  wide?: boolean;
}

export function Modal({ open, onOpenChange, title, description, children, footer, side, wide }: ModalProps) {
  const { t } = useT();
  const shape = side ? "sheet" : `modal${wide ? " modal-wide" : ""}`;
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="scrim" />
        <Dialog.Content className={shape} aria-describedby={description ? undefined : undefined}>
          <header className="modal-head">
            <div>
              <Dialog.Title className="modal-title">{title}</Dialog.Title>
              {description ? (
                <Dialog.Description className="modal-desc">{description}</Dialog.Description>
              ) : (
                <Dialog.Description className="sr-only">{title}</Dialog.Description>
              )}
            </div>
            <Dialog.Close className="icon-btn" aria-label={t("common.close")}>
              <X size={16} aria-hidden />
            </Dialog.Close>
          </header>
          {children ? <div className="modal-body">{children}</div> : null}
          {footer ? <footer className="modal-foot">{footer}</footer> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
