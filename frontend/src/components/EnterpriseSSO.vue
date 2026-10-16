<script>
import { useDashboard } from '../state/context'

export default {
  props: { orgId: { type: Number, required: true }, slug: { type: String, required: true }, owner: Boolean },
  setup: useDashboard,
  data: () => ({ sso: null, busy: '', error: '', message: '' }),
  watch: { orgId() { this.loadSSO() } },
  mounted() { this.loadSSO() },
  methods: {
    async loadSSO() {
      this.sso = null
      try { this.sso = await this.api(`/orgs/${this.orgId}/sso`) }
      catch (e) { this.error = e.detail || 'Could not load SSO settings.' }
    },
    async action(kind, path, body) {
      this.busy = kind; this.error = ''; this.message = ''
      try {
        const result = await this.api(`/orgs/${this.orgId}/sso/${path}`, {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body || {}),
        })
        if (result.url) { location.href = result.url; return }
        this.message = kind === 'enforce' ? 'SSO access setting saved.' :
          kind === 'cancel' ? 'Subscription will end after the current billing period.' : 'Saved.'
        await this.loadSSO()
      } catch (e) { this.error = e.detail || `Could not ${kind}.` }
      finally { this.busy = '' }
    },
  },
}
</script>

<template>
  <div class="card" style="padding:16px;margin-top:8px">
    <div class="lbl">Single sign-on</div>
    <p class="sub">Employees sign in through your company's identity provider. Their first successful sign-in joins this team as a member. The team owner can change roles in Members.</p>
    <p><a class="btn sm" :href="`/auth/sso/${encodeURIComponent(slug)}/start`">Sign in with company SSO</a></p>
    <p v-if="error" class="banner">{{error}}</p>
    <p v-if="message" class="tut-notice">{{message}}</p>
    <template v-if="owner && sso">
      <p v-if="!sso.configured || !sso.billing_configured" class="sub">SSO setup is unavailable until WorkOS and Stripe are configured on this deployment.</p>
      <template v-else-if="!['active','trialing'].includes(sso.subscription_status)">
        <button v-if="sso.enforced" class="btn sm" :disabled="!!busy" @click="action('enforce','enforce',{enabled:false})">Allow regular team sign-in</button>
        <div v-for="plan in sso.plans" :key="plan.id" style="display:flex;align-items:center;gap:12px;margin-top:10px">
          <span>{{plan.directory_sync ? 'SSO + Directory Sync' : 'SSO'}} — ${{plan.usd_monthly}}/month</span>
          <button class="btn sm" :disabled="!!busy" @click="action('checkout','checkout',{plan:plan.id})">Choose plan</button>
        </div>
        <p class="sub">Directory Sync removes team access and team keys when a directory user is deactivated or deleted. SSO alone does not provide automatic offboarding.</p>
      </template>
      <template v-else>
        <p class="sub">Plan: {{sso.plan==='sso_directory'?'SSO + Directory Sync':'SSO'}} · Subscription: {{sso.subscription_status}}</p>
        <button class="btn sm" :disabled="!!busy" @click="action('open setup','setup/sso')">Set up or manage SSO</button>
        <button v-if="sso.plan==='sso_directory'" class="btn sm" style="margin-left:8px" :disabled="!!busy" @click="action('open setup','setup/dsync')">Set up Directory Sync</button>
        <p class="sub" style="margin-top:12px">After setup, test one sign-in, then require SSO for this team. The owner can still sign in normally to recover access.</p>
        <button class="btn sm" :disabled="!!busy || !sso.sso_ready" @click="action('enforce','enforce',{enabled:!sso.enforced})">
          {{sso.enforced?'Allow regular team sign-in':'Require SSO for team'}}
        </button>
        <span v-if="!sso.sso_ready" class="sub" style="margin-left:8px">Test sign-in first.</span>
        <p v-if="sso.cancel_at_period_end" class="sub">This subscription will end after the current billing period.</p>
        <button v-else class="btn sm" style="margin-top:12px" :disabled="!!busy" @click="action('cancel','cancel')">Cancel at end of billing period</button>
      </template>
    </template>
  </div>
</template>
