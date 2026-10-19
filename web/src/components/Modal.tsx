import { useEffect, useRef, type FormEvent, type ReactNode } from "react";

/**
 * A modal form on the native <dialog>: focus moves in and back, Escape closes it, and the
 * page behind is inert, without a dependency.
 */
export function Modal(props: {
  open: boolean;
  title: string;
  onClose: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  submitLabel: string;
  submitDisabled?: boolean;
  wide?: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (props.open && !dialog.open) dialog.showModal();
    if (!props.open && dialog.open) dialog.close();
  }, [props.open]);

  const titleId = `modal-${props.title.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <dialog
      ref={ref}
      className={props.wide ? "modal modal--wide" : "modal"}
      aria-labelledby={titleId}
      onClose={props.onClose}
      onCancel={props.onClose}
    >
      <form
        className="modal-form"
        onSubmit={(event) => {
          event.preventDefault();
          props.onSubmit(event);
        }}
      >
        <h2 className="modal-title" id={titleId}>
          {props.title}
        </h2>
        <div className="modal-body">{props.children}</div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={props.onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn--primary" disabled={props.submitDisabled}>
            {props.submitLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}
