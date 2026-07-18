import { ReactNode, useEffect, useRef } from "react";
import { createPortal } from "react-dom";

type AdminModalProps = {
  title: string;
  description?: string;
  children: ReactNode;
  onClose: () => void;
  returnFocus?: HTMLElement | null;
};

export function AdminModal({ title, description, children, onClose, returnFocus }: AdminModalProps) {
  const panel = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const returnFocusRef = useRef(returnFocus);
  onCloseRef.current = onClose;
  returnFocusRef.current = returnFocus;

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusable =
      panel.current?.querySelector<HTMLElement>("[data-autofocus]") ??
      panel.current?.querySelector<HTMLElement>(
        "input, textarea, select, button:not([disabled]), [href], [tabindex]:not([tabindex='-1'])",
      );
    focusable?.focus();

    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const items = [...panel.current.querySelectorAll<HTMLElement>(
        "input, textarea, select, button:not([disabled]), [href], [tabindex]:not([tabindex='-1'])",
      )];
      if (items.length === 0) return;
      const first = items[0];
      const last = items.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.body.style.overflow = previousOverflow;
      returnFocusRef.current?.focus();
    };
  }, []);

  return createPortal(
    <div className="admin-modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div
        ref={panel}
        className="admin-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-modal-title"
        aria-describedby={description ? "admin-modal-description" : undefined}
      >
        <div className="admin-modal-heading">
          <div>
            <h2 id="admin-modal-title">{title}</h2>
            {description && <p id="admin-modal-description">{description}</p>}
          </div>
          <button className="admin-icon-button" type="button" onClick={onClose} aria-label="Close dialog">Close</button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}
