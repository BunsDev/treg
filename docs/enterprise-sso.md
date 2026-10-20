# Enterprise SSO setup

This integration uses WorkOS for a customer's SAML or OIDC connection. treg remains responsible for
accounts, team roles, browser sessions, and API keys. The team owner purchases a monthly Stripe
subscription and opens WorkOS setup from **Team → SSO**. The link is generated when clicked and
expires after five minutes. The owner can invite an IT admin by email through WorkOS, or open the
short-lived setup link directly. The owner can copy the employee sign-in link from **Team → SSO**;
employees can also select **Sign in with company SSO** and enter a work email to find their team.
Their first successful sign-in joins the existing team as a member.
The owner can change roles in **Members**.
Team display names may repeat; the link name is the unique slug in the employee sign-in URL
(for example, a second team named `Test` receives `test-2`). Employees can use the owner's exact
link if email discovery returns no match or several teams. Email is a routing hint, not access proof.

## Configure the deployment

1. Apply Alembic migrations through `0080` before running the new code.
2. In the WorkOS environment, create an API key and find the client ID. Set
   `TREG_WORKOS_API_KEY` and `TREG_WORKOS_CLIENT_ID` on the server. Set a stable
   `TREG_SECRET_KEY` and `TREG_SESSION_SECRET` for the state and browser cookies.
3. Add `<TREG_PUBLIC_URL>/auth/sso/callback` as an allowed WorkOS redirect URI. The URL passed to
   WorkOS comes from `TREG_PUBLIC_URL`, so it must match exactly.
4. For Directory Sync, register `<TREG_PUBLIC_URL>/auth/sso/workos/webhook` as a WorkOS webhook.
   Subscribe it to `dsync.activated`, `dsync.deleted`, and `dsync.user.created`,
   `dsync.user.updated`, `dsync.user.deleted`. Set its signing secret as
   `TREG_WORKOS_WEBHOOK_SECRET`. Without that secret, the endpoint returns 404.
   Run `treg-worker enterprise reconcile` periodically to repair missed webhook deliveries.
   It reads the complete WorkOS roster before changing access; monitor failures.
5. Configure Stripe's existing `TREG_STRIPE_SECRET_KEY` and `TREG_STRIPE_WEBHOOK_SECRET`. Send
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, and `customer.subscription.deleted` to the existing
   `/billing/stripe/webhook` endpoint. The same Stripe account can handle tool balance top-ups and
   SSO subscriptions; subscription events do not change the prepaid balance.
6. For an IdP app tile, set the WorkOS application's sign-in endpoint to
   `<TREG_PUBLIC_URL>/auth/sso/idp/start`. WorkOS supplies the connection hint and treg begins
   its normal, state-checked SSO redirect. Test the tile with the customer's IdP.

WorkOS staging and Stripe test mode can be used together. Keep staging keys and test keys in the
same test deployment; production credentials need their own configured redirect and webhook.
The built-in WorkOS Test Identity Provider accepts profiles only for the domain verified on its
WorkOS organization. A profile with another email domain is rejected before treg receives a code;
treg returns the employee to its sign-in dialog with an explanation.

## Owner and employee flow

The team owner chooses **SSO** ($125/month) or **SSO + Directory Sync** ($250/month), plus any
whole-dollar `TREG_ENTERPRISE_SSO_MARKUP_USD`. The default
`TREG_ENTERPRISE_SSO_ONLY_ENABLED=true` displays only the bundle; set it to `false` to offer both.
After Stripe reports an active subscription, the owner opens **Set up or manage SSO**. On the
bundle, they can also open **Set up Directory Sync**. The owner should complete a real test sign-in
before clicking **Require SSO for team**. That control only becomes available after a successful
WorkOS sign-in.

The owner may enter an IT admin email to have WorkOS send setup instructions; the recipient does
not need a treg owner account. The Admin Portal button remains for owners doing setup themselves.

An employee who signs in through the team's WorkOS organization gets a team membership with the
`member` role. A regular email or social sign-in does not satisfy SSO enforcement for that team,
even if it uses the same company email address. Existing team roles are never overwritten by an
IdP group. SSO links to an existing treg account only if its email was already verified; an
unverified existing account must prove its email through treg first. Owners retain their normal
sign-in as a recovery path. Team API keys remain separate
credentials; Directory Sync deactivation removes the affected membership and revokes its keys.
On the SSO-only plan, removing someone from the IdP stops future SSO sign-ins, but treg cannot
promise immediate revocation of an existing team session or key. Use Directory Sync for that.
An SSO browser session and any CLI identity token it mints are limited to the company team, even
when that person belongs to other treg teams. Use an ordinary sign-in to access those other teams.
If an admin removes an SSO member in treg, that person stays blocked even if the directory still
lists them as active. The owner can allow SSO again from the SSO tab. Directory deactivation and
later reactivation remain automatic unless a team admin explicitly removed the person.

The owner can schedule subscription cancellation at the end of the current billing period from
the SSO tab. When Stripe reports the subscription ended, SSO-linked non-owner memberships and
their team keys are revoked. An active subscription must end before the team can be deleted.

## Evidence and personal data

Company sign-ins and failures, setup actions, role changes, enforcement changes, and Directory Sync
access removals write a durable, team-scoped audit row. Access changes and their audit rows commit
together; setup requests are recorded after WorkOS responds. Rows contain the event,
internal user IDs, role transition when relevant, and time, not email addresses, profile payloads,
or WorkOS secrets. Team
deletion removes these rows. Schedule `treg-worker enterprise purge-audit` after the agreed
retention period (default 400 days); choose the final period with the SOC 2 auditor and privacy
counsel. Preserve separate evidence for setup approvals, IT verification, periodic access reviews,
sync failure monitoring, and incident response. These operational controls and WorkOS/Stripe
processing terms are necessary for SOC 2 and GDPR; code and tests alone do not establish compliance.

## Verification before production

The repository tests simulate the documented WorkOS profile and directory event payloads and
verify wrong-organization rejection, JIT membership, regular-session denial, webhook signature,
and deprovisioning. After account access is granted, perform a staging sign-in through a real
WorkOS connection and a Stripe test subscription, confirm webhook delivery and cancellation, and
test deactivating a directory user. The simulated tests cannot prove the external dashboard
configuration or provider-specific SAML/OIDC handshake.
