import { useEffect, useRef, useState } from "react";

/**
 * The fleet dialogs' one lifecycle (Reboot, Stage app): a native modal `<dialog>` that sends
 * ONE frozen request through its verb's send function. Esc, Cancel and any close are blocked
 * while a send is in flight; a "done" answer closes the dialog, any other answer stays on
 * screen. The dialog decides nothing about the request: the send function judges it.
 *
 * @param {(result: object|null) => void} onClose called once the dialog has closed, with the
 *   last answer (null when nothing was sent)
 * @returns {{dialogRef: import("react").MutableRefObject<HTMLDialogElement|null>,
 *            cancelRef: import("react").MutableRefObject<HTMLButtonElement|null>,
 *            phase: "idle"|"in-flight"|"terminal", result: object|null,
 *            run: (send: () => Promise<{outcome: string}>, onSent: (result: object) => void) => Promise<void>,
 *            close: () => void}}
 */
export function useSendDialog(onClose) {
  const dialogRef = useRef(/** @type {HTMLDialogElement|null} */ (null));
  const cancelRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const flying = useRef(false);
  const resultRef = useRef(/** @type {object|null} */ (null));
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [phase, setPhase] = useState(/** @type {"idle"|"in-flight"|"terminal"} */ ("idle"));
  const [result, setResult] = useState(/** @type {object|null} */ (null));

  useEffect(() => {
    const dialog = dialogRef.current;
    const onCancel = (event) => {
      if (flying.current) event.preventDefault();
    };
    const onCloseEvent = () => {
      if (flying.current) {
        dialog.showModal(); // a close got past the guard: keep the dialog and its state
        return;
      }
      onCloseRef.current(resultRef.current);
    };
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", onCloseEvent);
    dialog.showModal();
    cancelRef.current?.focus();
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      dialog.removeEventListener("close", onCloseEvent);
    };
  }, []);

  const run = async (send, onSent) => {
    if (flying.current) return;
    flying.current = true;
    dialogRef.current.setAttribute("closedby", "none");
    dialogRef.current.focus();
    setPhase("in-flight");
    const sent = await send();
    flying.current = false;
    dialogRef.current?.setAttribute("closedby", "closerequest");
    resultRef.current = sent;
    onSent(sent);
    if (sent.outcome === "done") {
      dialogRef.current?.close();
      return;
    }
    setResult(sent);
    setPhase("terminal");
  };

  const close = () => {
    if (!flying.current) dialogRef.current.close();
  };

  return { dialogRef, cancelRef, phase, result, run, close };
}
