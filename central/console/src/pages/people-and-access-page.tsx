import { useCallback, useEffect, useState } from "react";

import { type OperatorAccessView, type OperatorSessionView, ACCESS_NOT_READ_WORDS, peopleAndAccess } from "../domain/operator-access/access";
import { readAccess, readSession } from "../domain/operator-access/access-api";
import { PeopleAndAccess } from "../domain/operator-access/people-and-access";
import { NOT_READ, nextRead, type PageRead, PolledPage } from "../patterns/polled-page";
import { sinceTime } from "../timeWords.js";

type Read = PageRead<{ session: OperatorSessionView; access: OperatorAccessView }>;

export interface PeopleAndAccessPageProps {
  /** Log out of this browser: the session's owner does it (useSnapshot.js `signOut`). */
  onLogOut: () => void;
}

/**
 * Settings › People and access (`#/people-and-access`; decision 0035): who is signed in and the
 * deployment's identity provider with Central's last check of it, read once when the page opens
 * (and again on Try again). Nothing here is edited in Photo Wall.
 */
export function PeopleAndAccessPage({ onLogOut }: PeopleAndAccessPageProps) {
  const [read, setRead] = useState<Read>(NOT_READ);
  const [asked, setAsked] = useState(0);
  useEffect(() => {
    let current = true;
    void Promise.all([readSession(), readAccess()]).then(([session, access]) => {
      if (!current) return;
      setRead((previous) => nextRead(previous, session.ok && access.ok ? { session: session.session, access: access.access } : null));
    });
    return () => {
      current = false;
    };
  }, [asked]);
  const retry = useCallback(() => {
    setRead(NOT_READ);
    setAsked((count) => count + 1);
  }, []);
  return (
    <PolledPage read={read} onRetry={retry} what={ACCESS_NOT_READ_WORDS} stale={ACCESS_NOT_READ_WORDS}>
      {({ session, access }) => (
        <PeopleAndAccess model={peopleAndAccess(session, access, (at) => sinceTime(at))} onLogOut={onLogOut} />
      )}
    </PolledPage>
  );
}
