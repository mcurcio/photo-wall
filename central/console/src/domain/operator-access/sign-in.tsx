import { type FormEvent, useState } from "react";

import { Note } from "../../patterns/fact-row";
import { ProblemCard } from "../../patterns/problem-card";
import { Button } from "../../ui/button";
import { Field, TextInput } from "../../ui/field";
import { Section } from "../../ui/section";
import { Stack } from "../../ui/stack";
import {
  KEPT_WORDS, NO_ANSWER, NO_ANSWER_WORDS, NOT_SIGNED_IN, SIGN_IN_LABEL, SIGN_IN_TITLE, type SignInView, TOKEN_LABEL,
  TRY_AGAIN_LABEL,
} from "./access";

export interface SignInProps {
  view: SignInView;
  /** Sign in with the operator token as typed (the token way only). */
  onToken: (token: string) => void;
  /** Go to the provider to sign in (the provider way only). */
  onProvider: () => void;
  /** Ask Central again how to sign in. */
  onRetry: () => void;
}

/** The token way: the token is handed over once and cleared from the field at once; nothing keeps it. */
function TokenForm({ onToken }: { onToken: (token: string) => void }) {
  const [token, setToken] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setToken("");
    onToken(token);
  };
  return (
    <form onSubmit={submit}>
      <Stack>
        <Field label={TOKEN_LABEL}>
          <TextInput type="password" value={token} onChange={setToken} />
        </Field>
        <Button type="submit" variant="primary">{SIGN_IN_LABEL}</Button>
      </Stack>
    </form>
  );
}

/**
 * SignIn: the sign-in screen's card (decision 0035, A2 Sign in). One button to the deployment's
 * identity provider, or the operator token's field where no provider is set; over a console still
 * open it says the unsaved work is kept. The dialog around it is the shell's (SignInScreen.jsx).
 */
export function SignIn({ view, onToken, onProvider, onRetry }: SignInProps) {
  const { body } = view;
  return (
    <Section title={SIGN_IN_TITLE} heading="name">
      <Stack>
        {view.kept && <Note>{KEPT_WORDS}</Note>}
        {view.problem !== null && (
          <ProblemCard scope="action" variant="inline" live verdict={NOT_SIGNED_IN} what={view.problem} doing={null} />
        )}
        {body.kind === "loading" && <Button variant="primary" disabled aria-busy="true">{SIGN_IN_LABEL}</Button>}
        {body.kind === "failed" && (
          <ProblemCard scope="central" variant="inline" verdict={NO_ANSWER} what={NO_ANSWER_WORDS} doing={null}
            action={{ label: TRY_AGAIN_LABEL, onAction: onRetry }} />
        )}
        {body.kind === "token" && <TokenForm onToken={onToken} />}
        {body.kind === "provider" && <Button variant="primary" onClick={onProvider}>{body.label}</Button>}
      </Stack>
    </Section>
  );
}
