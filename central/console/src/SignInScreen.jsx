import React, { useState } from "react";

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
 */
export function SignInScreen() {
  const { signIn, snapshot } = useSnapshot();
  const [tokenInput, setTokenInput] = useState("");

  const submit = (event) => {
    event.preventDefault();
    // The token goes into the one sign-in request and is cleared from the field at
    // once; nothing keeps it (pass A §7).
    const token = tokenInput;
    setTokenInput("");
    signIn(token);
  };

  return (
    <div className="signin">
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
  );
}
