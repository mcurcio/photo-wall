import type { Meta, StoryObj } from "@storybook/react-vite";

import { type OperatorAccessView, type OperatorSessionView, peopleAndAccess } from "./access";
import { PeopleAndAccess } from "./people-and-access";

const OWNER: OperatorSessionView = { method: "provider", name: "Mateus", roles: ["owner"], provider: "Keycloak" };
const KEYCLOAK: OperatorAccessView = {
  method: "provider",
  provider: { name: "Keycloak", issuer_host: "id.example.net", reachable: true, last_reached_at: 1795443600 },
  session_cap_days: 30,
};
const since = () => "Sun 14:20";

const meta = {
  title: "Domain/PeopleAndAccess",
  component: PeopleAndAccess,
  args: { model: peopleAndAccess(OWNER, KEYCLOAK, since), onLogOut: () => {} },
} satisfies Meta<typeof PeopleAndAccess>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Signed in through the deployment's provider, which Central reaches. */
export const Provider: Story = {};
/** No provider set: one shared token, so no accounts to show. */
export const TokenMode: Story = {
  args: {
    model: peopleAndAccess({ method: "token", name: null, roles: [], provider: null },
      { method: "token", provider: null, session_cap_days: 30 }, since),
  },
};
/** Central can't reach the provider: its check can't tell, since when it last could. */
export const ProviderUnreachable: Story = {
  args: { model: peopleAndAccess(OWNER, { ...KEYCLOAK, provider: { ...KEYCLOAK.provider!, reachable: false } }, since) },
};
/** At phone width: the rows stack. */
export const Phone: Story = { parameters: { phone: true } };
