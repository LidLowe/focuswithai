'use strict';

const TARGET = 'http://127.0.0.1:8765/api/activity/browser';
const headers = { 'Content-Type': 'application/json', 'X-Panel': '1', 'X-FocusWithAI-Browser': '1' };
const PRIVATE = /\b(password|passcode|sign[ -]?in|log[ -]?in|bank|banking|payment|wallet|account|inbox|webmail|messages|auth|oauth|checkout)\b/i;
let generation = 0;

async function publish(data) {
  const response = await fetch(TARGET, { method: 'POST', headers, body: JSON.stringify(data), redirect: 'error' });
  if (!response.ok) throw new Error('FocusWithAI is unavailable');
  return response.json();
}

async function sync() {
  const run = ++generation;
  try {
    const { tabId } = await chrome.storage.session.get('tabId');
    let safe = null;
    if (Number.isInteger(tabId)) {
      const { active } = await publish({ check: true });
      if (generation !== run) return;
      if (!active) {
        await chrome.action.setBadgeText({ text: '' });
        return;
      }
      try {
        const tab = await chrome.tabs.get(tabId);
        const window = await chrome.windows.get(tab.windowId);
        if (!tab.incognito && tab.active && window.focused && tab.status === 'complete' && tab.url && tab.title) {
          const url = new URL(tab.url);
          if (/^https?:$/.test(url.protocol) && !url.username && !url.password &&
              !PRIVATE.test([url.hostname, url.pathname, tab.title].join(' '))) {
            const [check] = await chrome.scripting.executeScript({
              target: { tabId },
              func: () => !document.querySelector('input[type="password"],input[autocomplete="one-time-code"],input[autocomplete^="cc-"]'),
            });
            if (check?.result) safe = { domain: url.hostname, title: tab.title };
          }
        }
      } catch {
        // A closed tab, revoked activeTab grant, or inaccessible document clears context.
      }
    }
    if (generation !== run) return;
    await publish(safe || { clear: true });
    await chrome.action.setBadgeText({ text: safe ? 'ON' : '' });
  } catch {
    await chrome.action.setBadgeText({ text: '!' });
  }
}

chrome.action.onClicked.addListener(async (tab) => {
  if (tab.incognito || !Number.isInteger(tab.id)) return;
  const { tabId } = await chrome.storage.session.get('tabId');
  if (tabId === tab.id) {
    await chrome.storage.session.remove('tabId');
    await chrome.alarms.clear('heartbeat');
  } else {
    await chrome.storage.session.set({ tabId: tab.id });
    await chrome.alarms.create('heartbeat', { periodInMinutes: 0.5 });
  }
  await sync();
});
chrome.tabs.onActivated.addListener(sync);
chrome.tabs.onUpdated.addListener((id, change) => {
  if (change.title || change.url || change.status === 'loading') sync();
});
chrome.tabs.onRemoved.addListener(sync);
chrome.windows.onFocusChanged.addListener(sync);
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === 'heartbeat') sync(); });
