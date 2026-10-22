/* One email-first company sign-in control for the public pages' existing dialogs.
   Email finds a route; the WorkOS callback, never this lookup, authenticates the person. */
document.addEventListener('DOMContentLoaded', () => {
  const modal = document.querySelector('#signin-scrim .smodal');
  const email = modal?.querySelector('#em-input');
  const emailStep = modal?.querySelector('#em-step1');
  const codeStep = modal?.querySelector('#em-step2');
  const error = modal?.querySelector('#em-err');
  const codeButton = modal?.querySelector('#em-btn1');
  const provider = modal?.querySelector('a[href="/auth/github"], a[href="/auth/google"]');
  if (!email || !emailStep || !codeStep || !error || !codeButton || !provider) return;

  // Put the work email first on every page, while preserving each page's existing
  // email-code, Google, GitHub, captcha and post-login handlers.
  const label = document.createElement('label');
  label.htmlFor = 'em-input';
  label.textContent = 'Email';
  label.style.cssText = 'display:block;text-align:left;font-size:12px;margin-top:14px';
  emailStep.insertBefore(label, email);
  modal.insertBefore(emailStep, provider);
  modal.insertBefore(codeStep, provider);
  modal.insertBefore(error, provider);
  // Use each page's existing primary and secondary button styles. Public pages use
  // different class names, so do not introduce a landing-only class on every page.
  const primaryClass = provider.className;
  provider.className = primaryClass === 'candy'
    ? 'ghostbtn' : primaryClass.replace(/\bdark\b/g, '').trim();
  const divider = modal.querySelector('.div');
  if (divider) {
    divider.textContent = 'or sign in with';
    modal.insertBefore(divider, provider);
  }

  const ssoButton = document.createElement('button');
  ssoButton.type = 'button';
  ssoButton.className = primaryClass;
  ssoButton.textContent = 'Continue with company SSO';
  ssoButton.style.width = '100%';
  emailStep.insertBefore(ssoButton, codeButton);
  const result = document.createElement('div');
  result.setAttribute('role', 'status');
  result.style.cssText = 'text-align:left;font-size:12px;line-height:1.5;margin-top:8px';
  emailStep.append(result);
  email.addEventListener('input', () => { result.replaceChildren(); });

  ssoButton.addEventListener('click', async () => {
    const value = email.value.trim();
    result.replaceChildren();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
      result.textContent = 'Enter your work email first.';
      email.focus();
      return;
    }
    ssoButton.disabled = true;
    ssoButton.textContent = 'Finding company…';
    try {
      const response = await fetch('/auth/sso/discover', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({email: value}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Company sign-in is unavailable.');
      const teams = data.teams || [];
      if (teams.length === 1) {
        location.href = `/auth/sso/${encodeURIComponent(teams[0].slug)}/start`;
        return;
      }
      if (!teams.length) {
        result.textContent = 'This email has no company SSO connection. Use another sign-in method or ask your IT admin for your company link.';
        return;
      }
      result.textContent = 'Choose your company team:';
      for (const team of teams) {
        const choice = document.createElement('button');
        choice.type = 'button';
        choice.className = codeButton.className;
        choice.style.width = '100%';
        choice.textContent = `${team.name} · ${team.slug}`;
        choice.addEventListener('click', () => {
          location.href = `/auth/sso/${encodeURIComponent(team.slug)}/start`;
        });
        result.append(choice);
      }
    } catch (cause) {
      result.textContent = cause.message || 'Company sign-in is temporarily unavailable.';
    } finally {
      ssoButton.disabled = false;
      ssoButton.textContent = 'Continue with company SSO';
    }
  });
});
