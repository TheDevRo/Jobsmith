// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ---- LinkedIn Session ----
let linkedinPollInterval = null;

async function checkLinkedInSession() {
    const statusEl = document.getElementById('linkedin-status');
    const loginBtn = document.getElementById('linkedin-login-btn');
    const logoutBtn = document.getElementById('linkedin-logout-btn');
    const checkBtn = document.getElementById('linkedin-check-btn');
    try {
        const data = await api('/api/linkedin/session');
        const state = data.login_state || {};
        const sessionCheck = data.session_check || {};

        if (data.has_session) {
            // Session file exists — check if validity check says expired
            if (sessionCheck.valid === false) {
                statusEl.textContent = 'Session Expired \u2014 Reconnect to continue using LinkedIn auto-apply';
                statusEl.className = 'ai-status disconnected';
            } else {
                const sinceText = data.logged_in_at ? ` (signed in ${timeAgo(data.logged_in_at)})` : '';
                statusEl.textContent = `Connected \u2014 LinkedIn session active${sinceText}`;
                statusEl.className = 'ai-status connected';
            }
            logoutBtn.style.display = '';
            checkBtn.style.display = '';
            loginBtn.disabled = false;
            loginBtn.innerHTML = 'Sign in to LinkedIn';
            loginBtn.onclick = linkedinLogin;
            stopLinkedinPoll();
        } else if (state.status === 'waiting') {
            statusEl.textContent = 'Browser window opened \u2014 please sign in to LinkedIn there';
            statusEl.className = 'ai-status';
            loginBtn.disabled = false;
            loginBtn.innerHTML = 'Cancel Login';
            loginBtn.onclick = cancelLinkedinLogin;
            logoutBtn.style.display = 'none';
            checkBtn.style.display = 'none';
        } else if (state.status === 'failed') {
            statusEl.textContent = state.message || 'Login failed';
            statusEl.className = 'ai-status disconnected';
            loginBtn.disabled = false;
            loginBtn.innerHTML = 'Sign in to LinkedIn';
            loginBtn.onclick = linkedinLogin;
            logoutBtn.style.display = 'none';
            checkBtn.style.display = 'none';
            stopLinkedinPoll();
        } else {
            statusEl.textContent = 'Not connected \u2014 sign in to enable LinkedIn auto-apply';
            statusEl.className = 'ai-status disconnected';
            logoutBtn.style.display = 'none';
            checkBtn.style.display = 'none';
            loginBtn.disabled = false;
            loginBtn.innerHTML = 'Sign in to LinkedIn';
            loginBtn.onclick = linkedinLogin;
        }
    } catch (e) {
        statusEl.textContent = '';
        statusEl.className = 'ai-status';
    }
}

function startLinkedinPoll() {
    stopLinkedinPoll();
    linkedinPollInterval = setInterval(async () => {
        const data = await api('/api/linkedin/session').catch(() => null);
        if (!data) return;
        const state = data.login_state || {};
        if (state.status !== 'waiting') {
            if (data.has_session) {
                toast('LinkedIn login successful!', 'success');
            } else if (state.status === 'failed') {
                toast(state.message || 'Login failed', 'error');
            }
            checkLinkedInSession();
        }
    }, 2000);
}

function stopLinkedinPoll() {
    if (linkedinPollInterval) {
        clearInterval(linkedinPollInterval);
        linkedinPollInterval = null;
    }
}

async function linkedinLogin() {
    const loginBtn = document.getElementById('linkedin-login-btn');
    const statusEl = document.getElementById('linkedin-status');

    loginBtn.disabled = true;
    loginBtn.innerHTML = '<span class="loading"></span>Opening browser...';
    statusEl.textContent = 'Starting browser...';
    statusEl.className = 'ai-status';

    try {
        await api('/api/linkedin/login', {
            method: 'POST',
            body: JSON.stringify({}),
        });
        startLinkedinPoll();
        checkLinkedInSession();
    } catch (e) {
        toast('Failed to start LinkedIn login', 'error');
        loginBtn.disabled = false;
        loginBtn.innerHTML = 'Sign in to LinkedIn';
    }
}

async function linkedinLogout() {
    try {
        await api('/api/linkedin/logout', { method: 'POST' });
        toast('LinkedIn session cleared', 'info');
    } catch (e) {
        toast('Failed to clear session', 'error');
    }
    checkLinkedInSession();
}

async function cancelLinkedinLogin() {
    try {
        await api('/api/linkedin/login/cancel', { method: 'POST' });
        toast('Login cancelled — browser closed', 'info');
    } catch (e) {
        toast('Failed to cancel login', 'error');
    }
    stopLinkedinPoll();
    checkLinkedInSession();
}

async function checkLinkedInSessionValidity() {
    const checkBtn = document.getElementById('linkedin-check-btn');
    const statusEl = document.getElementById('linkedin-status');
    checkBtn.disabled = true;
    checkBtn.innerHTML = '<span class="loading"></span>Checking...';
    statusEl.textContent = 'Checking LinkedIn session\u2026';
    statusEl.className = 'ai-status';
    try {
        await api('/api/linkedin/check-session', { method: 'POST' });
    } catch (e) {
        toast('Session check failed', 'error');
    }
    checkBtn.disabled = false;
    checkBtn.innerHTML = 'Check Connection';
    await checkLinkedInSession();
}

// ---- Indeed Session ----
let indeedPollInterval = null;

async function checkIndeedSession() {
    const statusEl = document.getElementById('indeed-status');
    const loginBtn = document.getElementById('indeed-login-btn');
    const logoutBtn = document.getElementById('indeed-logout-btn');
    try {
        const data = await api('/api/indeed/session');
        const state = data.login_state || {};

        if (data.has_session) {
            statusEl.textContent = 'Connected \u2014 Indeed session active';
            statusEl.className = 'ai-status connected';
            logoutBtn.style.display = '';
            loginBtn.disabled = false;
            loginBtn.innerHTML = 'Connect Indeed Account';
            loginBtn.onclick = indeedLogin;
            stopIndeedPoll();
        } else if (state.status === 'waiting') {
            statusEl.textContent = 'Browser window opened \u2014 please sign in to Indeed there';
            statusEl.className = 'ai-status';
            loginBtn.disabled = false;
            loginBtn.innerHTML = 'Cancel Login';
            loginBtn.onclick = cancelIndeedLogin;
            logoutBtn.style.display = 'none';
        } else if (state.status === 'failed') {
            statusEl.textContent = state.message || 'Login failed';
            statusEl.className = 'ai-status disconnected';
            loginBtn.disabled = false;
            loginBtn.innerHTML = 'Connect Indeed Account';
            loginBtn.onclick = indeedLogin;
            logoutBtn.style.display = 'none';
            stopIndeedPoll();
        } else {
            statusEl.textContent = 'Not connected \u2014 sign in to enable Indeed auto-apply';
            statusEl.className = 'ai-status disconnected';
            logoutBtn.style.display = 'none';
            loginBtn.disabled = false;
            loginBtn.innerHTML = 'Connect Indeed Account';
            loginBtn.onclick = indeedLogin;
        }
    } catch (e) {
        statusEl.textContent = '';
        statusEl.className = 'ai-status';
    }
}

function startIndeedPoll() {
    stopIndeedPoll();
    indeedPollInterval = setInterval(async () => {
        const data = await api('/api/indeed/session').catch(() => null);
        if (!data) return;
        const state = data.login_state || {};
        if (state.status !== 'waiting') {
            if (data.has_session) {
                toast('Indeed login successful!', 'success');
            } else if (state.status === 'failed') {
                toast(state.message || 'Login failed', 'error');
            }
            checkIndeedSession();
        }
    }, 2000);
}

function stopIndeedPoll() {
    if (indeedPollInterval) {
        clearInterval(indeedPollInterval);
        indeedPollInterval = null;
    }
}

async function indeedLogin() {
    const loginBtn = document.getElementById('indeed-login-btn');
    const statusEl = document.getElementById('indeed-status');

    loginBtn.disabled = true;
    loginBtn.innerHTML = '<span class="loading"></span>Opening browser...';
    statusEl.textContent = 'Starting browser...';
    statusEl.className = 'ai-status';

    try {
        await api('/api/indeed/login', {
            method: 'POST',
            body: JSON.stringify({}),
        });
        startIndeedPoll();
        checkIndeedSession();
    } catch (e) {
        toast('Failed to start Indeed login', 'error');
        loginBtn.disabled = false;
        loginBtn.innerHTML = 'Connect Indeed Account';
    }
}

async function indeedLogout() {
    try {
        await api('/api/indeed/logout', { method: 'POST' });
        toast('Indeed session cleared', 'info');
    } catch (e) {
        toast('Failed to clear session', 'error');
    }
    checkIndeedSession();
}

async function cancelIndeedLogin() {
    try {
        await api('/api/indeed/logout', { method: 'POST' });
        toast('Login cancelled', 'info');
    } catch (e) {
        toast('Failed to cancel login', 'error');
    }
    stopIndeedPoll();
    checkIndeedSession();
}


// ---- Cookie import ----
function cookieImportSiteChanged() {
    const site = document.getElementById('cookie-import-site').value;
    document.getElementById('cookie-import-domain').style.display = site === 'other' ? '' : 'none';
}

async function importCookies(input) {
    const file = input.files[0];
    if (!file) return;
    const statusEl = document.getElementById('cookie-import-status');
    const btn = document.getElementById('cookie-import-btn');
    let site = document.getElementById('cookie-import-site').value;
    if (site === 'other') {
        site = document.getElementById('cookie-import-domain').value.trim().toLowerCase();
        if (!site) {
            statusEl.textContent = 'Enter the domain the cookies belong to (e.g. glassdoor.com).';
            statusEl.className = 'ai-status disconnected';
            input.value = '';
            return;
        }
    }

    const fd = new FormData();
    fd.append('file', file);
    fd.append('site', site);
    statusEl.textContent = 'Importing cookies…';
    statusEl.className = 'ai-status';
    btn.disabled = true;
    try {
        const resp = await fetch(API + '/api/sessions/import-cookies', { method: 'POST', body: fd });
        if (!resp.ok) {
            let msg = 'Import failed';
            try { msg = (await resp.json()).detail || msg; } catch (e) {}
            throw new Error(msg);
        }
        const data = await resp.json();
        if (data.site === 'linkedin') {
            statusEl.textContent = `Imported ${data.imported} LinkedIn cookies — verifying the session works…`;
            statusEl.className = 'ai-status';
            toast('LinkedIn cookies imported — checking session', 'info');
            // The backend kicked a background validity check; give it a moment
            // then refresh the LinkedIn card (which shows expired vs connected).
            setTimeout(async () => {
                await checkLinkedInSession();
                statusEl.textContent = `Imported ${data.imported} cookies. See LinkedIn Session status above.`;
                statusEl.className = 'ai-status connected';
            }, 8000);
        } else if (data.site === 'indeed') {
            statusEl.textContent = `Imported ${data.imported} Indeed cookies.`;
            statusEl.className = 'ai-status connected';
            toast('Indeed cookies imported', 'success');
            checkIndeedSession();
        } else {
            statusEl.textContent = `Imported ${data.imported} cookies for ${data.site}.`;
            statusEl.className = 'ai-status connected';
            toast(`Cookies imported for ${data.site}`, 'success');
        }
    } catch (e) {
        statusEl.textContent = e.message || 'Import failed';
        statusEl.className = 'ai-status disconnected';
        toast('Cookie import failed', 'error');
    }
    btn.disabled = false;
    input.value = '';
}
