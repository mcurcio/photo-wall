import type { Meta, StoryObj } from "@storybook/react-vite";

import { type SignInMethod, signInView } from "./access";
import { SignIn } from "./sign-in";

const KEYCLOAK: SignInMethod = { status: "read", method: "provider", name: "Keycloak" };
const TOKEN: SignInMethod = { status: "read", method: "token", name: null };

const meta = {
  title: "Domain/SignIn",
  component: SignIn,
  args: { view: signInView(KEYCLOAK, null, false), onToken: () => {}, onProvider: () => {}, onRetry: () => {} },
} satisfies Meta<typeof SignIn>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A first load, signed out: one button to the deployment's identity provider. */
export const Provider: Story = {};
/** Signed out while the console is open: the work underneath is kept, and the button opens the
 * provider in a new tab. */
export const ProviderOverlay: Story = { args: { view: signInView(KEYCLOAK, "expired", true) } };
/** No provider set: the operator token's field, as before. */
export const Token: Story = { args: { view: signInView(TOKEN, null, false) } };
/** Signed in at the provider, without the role that lets someone in. */
export const NotAllowed: Story = { args: { view: signInView(KEYCLOAK, "notAllowed", false) } };
/** Central could not reach the provider. */
export const ProviderUnreachable: Story = { args: { view: signInView(KEYCLOAK, "providerUnreachable", false) } };
/** Central has not said how to sign in yet: the button is busy. */
export const Loading: Story = { args: { view: signInView({ status: "loading", method: null, name: null }, null, false) } };
/** Central did not answer how to sign in. */
export const NoAnswer: Story = { args: { view: signInView({ status: "failed", method: null, name: null }, null, false) } };
/** At phone width: one column, the button full width. */
export const Phone: Story = { parameters: { phone: true } };
