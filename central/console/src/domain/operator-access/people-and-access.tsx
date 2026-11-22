import type * as React from "react";

import { EmptyState } from "../../patterns/entity-page";
import { Note } from "../../patterns/fact-row";
import { HealthBadge } from "../../patterns/health-badge";
import { SettingRow } from "../../patterns/setting-row";
import { Button } from "../../ui/button";
import { useFieldControl } from "../../ui/field";
import { Section } from "../../ui/section";
import { Inline } from "../../ui/stack";
import { ACCOUNTS_LABEL, LASTS_LABEL, LOG_OUT_LABEL, PEOPLE_TITLE, type PeopleAndAccessModel, SIGNED_IN_AS_LABEL } from "./access";

/** A setting nothing here changes, as its row's control: its value in words, named by the row's label. */
function Stated({ children }: { children: React.ReactNode }) {
  const { id, describedBy } = useFieldControl();
  return <output id={id} aria-describedby={describedBy}>{children}</output>;
}

/** A stated value with what belongs beside it (its one action, its check). */
function StatedWith({ value, children }: { value: string; children: React.ReactNode }) {
  return (
    <Inline>
      <Stated>{value}</Stated>
      {children}
    </Inline>
  );
}

export interface PeopleAndAccessProps {
  model: PeopleAndAccessModel;
  /** Log out of this browser (the same Log out as the header's). */
  onLogOut: () => void;
}

/**
 * PeopleAndAccess: Settings › People and access (decision 0035, A2 Owner account). Who is signed
 * in, where the accounts live and how long a sign-in lasts: all read-only, because the accounts are
 * the deployment's identity provider's. With no provider there are no accounts to show.
 */
export function PeopleAndAccess({ model, onLogOut }: PeopleAndAccessProps) {
  if (model.kind === "token") return <EmptyState>{model.empty}</EmptyState>;
  return (
    <Section title={PEOPLE_TITLE}>
      <SettingRow kind="house" state="idle" label={SIGNED_IN_AS_LABEL}
        control={<StatedWith value={model.signedInAs}><Button onClick={onLogOut}>{LOG_OUT_LABEL}</Button></StatedWith>} />
      <SettingRow kind="house" state="idle" label={ACCOUNTS_LABEL} help={model.accountsHelp}
        control={<StatedWith value={model.accounts}><HealthBadge verdict={model.check} /></StatedWith>} />
      {model.checkNote !== null && <Note>{model.checkNote}</Note>}
      <SettingRow kind="house" state="idle" label={LASTS_LABEL} control={<Stated>{model.lasts}</Stated>} />
    </Section>
  );
}
