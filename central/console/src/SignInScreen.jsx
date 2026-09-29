import React, { useEffect, useRef, useState } from "react";

import { useSnapshot } from "./useSnapshot.js";

/**
 * The sign-in screen (pass A §7): an "Operator token" field and a "Sign in" button
 * exchange the token once for the session cookie. The field is cleared on submit and
 * the token is kept nowhere.
 *
 * On a first load with no session it is the whole page. When a session ends while
 * signed in (expired, token rotated) it is an OVERLAY: the shell stays mounted,
 * hidden and inert, with the last snapshot and every draft, until signing in again
 * (flow design §6 (a), Question 6); it then says the work is kept.
 *
 * Both are ONE native `<dialog>`, always mounted and opened with `showModal()` while
 * `open`, covering the viewport. The top layer puts it above anything the shell had
 * open — a confirmation's own modal `<dialog>` would otherwise keep the whole
 * document, this form included, inert. Opening focuses "Operator token". It cannot be
 * dismissed (Esc is refused, and a close that still gets through re-opens it); it
 * closes when the session is back, and closing returns focus to where it was when it
 * opened (the dialog's own focus restoration), so work resumes where it stopped.
 *
 * `children` (the session's notices) sit at its top, where they stay reachable.
 *
 * @param {{open: boolean, children?: React.ReactNode}} props
 */
export function SignInScreen({ open, children }) {
  const { signIn, snapshot } = useSnapshot();
  const [tokenInput, setTokenInput] = useState("");
  const dialogRef = useRef(/** @type {HTMLDialogElement|null} */ (null));
  const tokenRef = useRef(/** @type {HTMLInputElement|null} */ (null));
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    const dialog = dialogRef.current;
    const onCancel = (event) => event.preventDefault();
    const onClose = () => {
      if (openRef.current) {
        // A close request got past the refused cancel: still signed out.
        dialog.showModal();
      }
    };
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", onClose);
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      dialog.removeEventListener("close", onClose);
    };
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (open && !dialog.open) {
      dialog.showModal();
      tokenRef.current?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
      // Chromium restores focus before the page behind stops being inert, which leaves
      // a text field focused but deaf to keys; focusing it again, now, wakes it.
      const restored = document.activeElement;
      if (restored instanceof HTMLElement && restored !== document.body) {
        restored.blur();
        restored.focus();
      }
    }
  }, [open]);

  const submit = (event) => {
    event.preventDefault();
    // The token goes into the one sign-in request and is cleared from the field at
    // once; nothing keeps it (pass A §7).
    const token = tokenInput;
    setTokenInput("");
    signIn(token);
  };

  return (
    <dialog ref={dialogRef} className="signin" aria-labelledby="signin-title" closedby="none">
      {children}
      <div className="signin__center">
        <form className="signin__card" aria-labelledby="signin-title" onSubmit={submit}>
          <h1 id="signin-title" className="signin__title">
            Sign in to Photo Wall
          </h1>
          {snapshot !== null && (
            <p className="signin__kept">Your unsaved work is kept until you sign in again.</p>
          )}
          <label className="console__token-field">
            Operator token
            <input
              ref={tokenRef}
              type="password"
              name="operator-token"
              autoComplete="off"
              value={tokenInput}
              onChange={(event) => setTokenInput(event.target.value)}
            />
          </label>
          <button type="submit">Sign in</button>
        </form>
      </div>
    </dialog>
  );
}
