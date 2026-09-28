import React from "react";

import "./index.css";
import { Shell } from "./Shell.jsx";
import { SignInScreen } from "./SignInScreen.jsx";
import { useSnapshot } from "./useSnapshot.js";

// Why the tab is signed out or a sign-in / log-out failed (pass A §7).
const AUTH_NOTICES = {
  rejected: "Operator token was not accepted. Re-enter the token to sign in.",
  expired: "Signed out: the session expired or the token changed. Sign in again.",
  blocked: "Your browser did not keep the sign-in; allow cookies for this site.",
  failed: "Sign-in failed: Central did not answer. Try again.",
  signOutFailed: "Log out failed: Central did not answer. Try again.",
};

// A write Central refused because it did not come from the signed-in page (403
// request_unmarked / origin_mismatch; pass A §7). A header-stripping proxy reads the same.
const ORIGIN_REFUSED_MESSAGE =
  "Central refused this write because it did not come from the page you signed in on. " +
  "Reload the console from the address you signed in at, or sign in again.";

/**
 * The console root: the session's notices, the navigation shell and, while signed
 * out, the sign-in screen (pass A §7; flow design §6).
 *
 * The shell is keyed on `sessionEpoch`, which Log out bumps, so Log out remounts it
 * and discards every draft. Signed out, the shell is hidden and inert but stays
 * mounted: a session that expires under a draft keeps it (and the last snapshot)
 * behind the sign-in overlay until the operator signs in again. The notices are the
 * same either way, so a sign-in refused for its origin shows the same alert as a
 * write; while signed out they sit inside the sign-in screen, a modal dialog that
 * leaves everything outside it inert.
 */
export default function App() {
  const { auth, authNotice, originRefused, dismissOriginRefused, sessionEpoch } = useSnapshot();
  const signedOut = auth === "signedOut";

  const notices = (
    <>
      {authNotice !== null && (
        <p className="console__auth-error" role="alert">
          {AUTH_NOTICES[authNotice]}
        </p>
      )}
      {originRefused && (
        <div className="console__auth-error" role="alert">
          <p>{ORIGIN_REFUSED_MESSAGE}</p>
          <button type="button" onClick={dismissOriginRefused}>
            Dismiss
          </button>
        </div>
      )}
    </>
  );

  return (
    <div className="console">
      {!signedOut && notices}
      <Shell key={sessionEpoch} hidden={signedOut} />
      <SignInScreen open={signedOut}>{signedOut && notices}</SignInScreen>
    </div>
  );
}
