import React, { useEffect, useRef } from "react";

import { sessionStartUrl, signInView, signsInElsewhere } from "./domain/operator-access/access.ts";
import { SignIn } from "./domain/operator-access/sign-in.tsx";
import { useSnapshot } from "./useSnapshot.js";

// While signed out through the provider over an open console, how often Central is asked whether
// this browser has a session again (decision 0035: the one polled read while signed out).
const SESSION_CHECK_MS = 2000;

/**
 * The sign-in screen (pass A §7; decision 0035). How to sign in is Central's to say
 * (useSnapshot's `signInMethod`): through the deployment's identity provider, one "Sign in with
 * <provider>" button; or, where no provider is set, an "Operator token" field and a "Sign in"
 * button that exchange the token once for the session cookie. The field is cleared on submit and
 * the token is kept nowhere. The card itself is the catalog's (domain/operator-access/sign-in.tsx).
 *
 * On a first load with no session it is the whole page, and the provider's button navigates
 * the page to Central's `session/start`. When a session ends while
 * signed in (expired, token rotated) it is an OVERLAY: the shell stays mounted,
 * hidden and inert, with the last snapshot and every draft, until signing in again
 * (flow design §6 (a), Question 6); it then says the work is kept. There the provider's button
 * opens `session/start` in a NEW TAB, and this screen asks Central every 2 s whether the browser
 * has a session again; when it has, the console resumes as after any sign-in.
 *
 * Both are ONE native `<dialog>`, always mounted and opened with `showModal()` while
 * `open`, covering the viewport. The top layer puts it above anything the shell had
 * open — a confirmation's own modal `<dialog>` would otherwise keep the whole
 * document, this form included, inert. Opening focuses the card's first control ("Operator
 * token", or the provider's button). It cannot be
 * dismissed (Esc is refused, and a close that still gets through re-opens it); it
 * closes when the session is back, and closing returns focus to where it was when it
 * opened (the dialog's own focus restoration), so work resumes where it stopped.
 *
 * `children` (the origin refusal's notice) sit at its top, where they stay reachable; `notice`
 * (why the tab is signed out) is said in the card.
 *
 * @param {{open: boolean, notice: import("./useSnapshot.js").AuthNotice,
 *          children?: React.ReactNode}} props
 */
export function SignInScreen({ open, notice, children }) {
  const { signIn, snapshot, signInMethod, readSignInMethod, checkSession } = useSnapshot();
  const dialogRef = useRef(/** @type {HTMLDialogElement|null} */ (null));
  const openRef = useRef(open);
  openRef.current = open;
  const held = snapshot !== null;
  const view = signInView(signInMethod, notice, held);
  const elsewhere = signsInElsewhere(signInMethod, held);
  const offered = view.body.kind;

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

  useEffect(() => {
    // The card's first control takes focus when the screen opens and when what it offers changes
    // (Central answered how to sign in).
    if (open) {
      dialogRef.current.querySelector(".signin__card input, .signin__card button:not([disabled])")?.focus();
    }
  }, [open, offered]);

  useEffect(() => {
    if (!open || !elsewhere) {
      return undefined;
    }
    const id = setInterval(() => {
      checkSession();
    }, SESSION_CHECK_MS);
    return () => clearInterval(id);
  }, [open, elsewhere, checkSession]);

  const startAtProvider = () => {
    const url = sessionStartUrl(window.location.origin, window.location.hash);
    if (elsewhere) {
      // The shell and every draft stay mounted here; the sign-in happens in the new tab.
      window.open(url, "_blank");
    } else {
      window.location.assign(url);
    }
  };

  return (
    <dialog ref={dialogRef} className="signin" aria-label="Sign in to Photo Wall" closedby="none">
      {children}
      <div className="signin__center">
        <div className="signin__card">
          <SignIn view={view} onToken={signIn} onProvider={startAtProvider} onRetry={readSignInMethod} />
        </div>
      </div>
    </dialog>
  );
}
